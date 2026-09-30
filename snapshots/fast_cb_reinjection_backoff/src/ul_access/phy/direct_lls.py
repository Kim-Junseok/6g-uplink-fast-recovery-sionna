"""Direct public-Sionna HARQ LLS for the V0.4g controlled PHY category."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import numpy as np
import torch
from sionna.phy import config as sionna_config
from sionna.phy.channel import cir_to_ofdm_channel, subcarrier_frequencies
from sionna.phy.channel.tr38901 import TDL
from sionna.phy.fec.crc import CRCDecoder, CRCEncoder
from sionna.phy.fec.ldpc import LDPC5GDecoder, LDPC5GEncoder
from sionna.phy.mapping import Demapper, Mapper
from sionna.phy.mimo import StreamManagement
from sionna.phy.nr import PUSCHConfig, calculate_tb_size
from sionna.phy.ofdm import EyePrecodedChannel, LMMSEPostEqualizationSINR, ResourceGrid
from sionna.phy.utils import dbm_to_watt, lin_to_db
from sionna.sys import EESM, spread_across_subcarriers

from ul_access.phy.interface import HarqPhyBackend, PhyOutcome
from ul_access.resource import ResourceAllocation


RNG_CONTRACT = "v0.4g-event-addressed-blake2s-v1"


def classify_crc_oracle_outcome(
    crc_pass: bool,
    decoded_payload: torch.Tensor,
    transmitted_payload: torch.Tensor,
) -> tuple[int, bool, bool, int]:
    """Separate receiver CRC feedback from simulator-oracle correctness."""
    payload_correct = bool(torch.equal(decoded_payload, transmitted_payload))
    hamming_distance = int(torch.count_nonzero(
        decoded_payload != transmitted_payload).item())
    undetected_error = bool(crc_pass and not payload_correct)
    return int(crc_pass), payload_correct, undetected_error, hamming_distance


def addressed_seed(run_seed: int, domain: str, *address: object,
                   bits: int = 64) -> int:
    """Return a domain-separated seed whose value is independent of call order."""
    payload = "\0".join((RNG_CONTRACT, str(run_seed), domain,
                         *(str(value) for value in address))).encode()
    return int.from_bytes(hashlib.blake2s(
        payload, digest_size=bits // 8).digest(), "big")


def audit_controlled_pusch_geometry() -> dict[str, Any]:
    """Derive the controlled 160-bit operating point through public NR APIs."""
    cfg = PUSCHConfig()
    cfg.carrier.n_size_grid = 1
    cfg.symbol_allocation = [0, 11]
    cfg.dmrs.config_type = 1
    cfg.dmrs.length = 1
    cfg.dmrs.additional_position = 0
    cfg.dmrs.num_cdm_groups_without_data = 1
    cfg.tb.mcs_table = 1
    cfg.tb.mcs_index = 10
    result = calculate_tb_size(
        modulation_order=4, target_coderate=340 / 1024,
        num_prbs=1, num_ofdm_symbols=11, num_dmrs_per_prb=6)
    tb_size, cb_size, num_cbs, tb_crc, cb_crc, cw_lengths = result
    ldpc = LDPC5GEncoder(176, 504, num_bits_per_symbol=4,
                         precision="single", device="cpu")
    geometry = {
        "allocated_pusch_symbols": 11,
        "prbs_per_attempt": 1,
        "dmrs_config_type": 1,
        "dmrs_symbols": list(cfg.dmrs_symbol_indices),
        "dmrs_re_per_prb": 6,
        "data_re": int(cfg.num_res_per_prb),
        "mcs_table": 1,
        "mcs_index": 10,
        "modulation": "16-QAM",
        "modulation_order": int(cfg.tb.num_bits_per_symbol.item()),
        "target_code_rate_x1024": 340,
        "tbs_bits": int(tb_size.item()),
        "tb_crc_bits": int(tb_crc.item()),
        "ldpc_input_bits": int(cb_size.item()),
        "num_code_blocks": int(num_cbs.item()),
        "cb_crc_bits": int(cb_crc.item()),
        "coded_bits": int(cw_lengths.sum().item()),
        "qam_symbols": int(cw_lengths.sum().item()) // 4,
        "pusch_config_tbs_bits": int(cfg.tb_size),
        "pusch_config_coded_bits": int(cfg.num_coded_bits),
        "ldpc_lifting_size": int(ldpc.k_ldpc // 10),
        "ldpc_systematic_bits": int(ldpc.k_ldpc),
        "ldpc_mother_code_bits": int(ldpc.n_ldpc),
        "ldpc_filler_bits": int(ldpc.k_filler),
        "ldpc_circular_buffer_bits": int(ldpc.n_cb),
        "ldpc_compressed_buffer_bits": int(ldpc.n_cb_comp),
        "effective_code_rate": float(ldpc.coderate),
        "rv_sequence": [0, 2, 3, 1],
    }
    expected = {"data_re": 126, "tbs_bits": 160, "tb_crc_bits": 16,
                "ldpc_input_bits": 176, "coded_bits": 504,
                "qam_symbols": 126, "pusch_config_tbs_bits": 160,
                "pusch_config_coded_bits": 504}
    if any(geometry[key] != value for key, value in expected.items()):
        raise RuntimeError(f"controlled PUSCH geometry gate failed: {geometry}")
    return geometry


@dataclass
class DirectHarqEpisodeState:
    """Bit-level state retained for one TB and HARQ episode."""

    mac_tb_id: str
    harq_episode_id: str
    mcs_index: int
    payload_bits: torch.Tensor = field(repr=False)
    rv_history: tuple[int, ...] = ()
    effective_sinr_history_db: tuple[float, ...] = ()
    llr_history: tuple[torch.Tensor, ...] = field(default=(), repr=False)
    pending_rv: int | None = field(default=None, repr=False)
    pending_sinr_db: float | None = field(default=None, repr=False)
    pending_llr: torch.Tensor | None = field(default=None, repr=False)


class FrequencySelectivePrbReceiver:
    """TDL-C frequency response and known-activity joint-LMMSE receiver."""

    def __init__(self, config: Mapping[str, Any]) -> None:
        self.num_ues = int(config["num_ues"])
        self.num_prbs = int(config.get("num_prbs", 6))
        self.num_rx_ant = int(config.get("num_bs_receive_antennas", 2))
        self.scs_hz = float(config.get("subcarrier_spacing_hz", 15_000.0))
        self.noise_power_w = float(config["noise_power_w"])
        self.carrier_frequency_hz = float(config.get("carrier_frequency_hz", 3.5e9))
        self.delay_spread_s = float(config.get("delay_spread_s", 300e-9))
        self.num_symbols = 11
        self.fft_size = 12 * self.num_prbs
        self.geometry = audit_controlled_pusch_geometry()
        pusch = PUSCHConfig()
        pusch.carrier.n_size_grid = 1
        pusch.symbol_allocation = [0, 11]
        pusch.dmrs.config_type = 1
        pusch.dmrs.length = 1
        pusch.dmrs.additional_position = 0
        pusch.dmrs.num_cdm_groups_without_data = 1
        self._prb_data_mask = torch.tensor(
            (~pusch.dmrs_mask[:, :self.num_symbols].T).copy(), dtype=torch.bool)
        if int(self._prb_data_mask.sum()) != 126:
            raise RuntimeError("PUSCH data mask does not contain 126 RE")
        self._grid = ResourceGrid(
            num_ofdm_symbols=self.num_symbols, fft_size=self.fft_size,
            subcarrier_spacing=self.scs_hz, num_tx=self.num_ues,
            num_streams_per_tx=1)
        self._streams = StreamManagement(
            np.ones((1, self.num_ues), dtype=np.int32), 1)
        self._precoder = EyePrecodedChannel(self._grid, self._streams)
        self._lmmse = LMMSEPostEqualizationSINR(self._grid, self._streams)
        self._eesm = EESM()
        self._frequencies = subcarrier_frequencies(self.fft_size, self.scs_hz)
        self.last_channel: torch.Tensor | None = None
        self.timing_seconds = 0.0

    def _tdl_channel(self, seed: int,
                     ue_identities: Sequence[int] | None = None) -> torch.Tensor:
        identities = tuple(range(self.num_ues)) if ue_identities is None else tuple(
            int(value) for value in ue_identities)
        if len(identities) != self.num_ues:
            raise ValueError("channel identities must match PHY-local UE count")
        links = []
        for ue_identity in identities:
            event_seed = addressed_seed(seed, "channel", ue_identity, bits=32)
            torch.manual_seed(event_seed)
            sionna_config.seed = event_seed
            model = TDL(
                model="C", delay_spread=self.delay_spread_s,
                carrier_frequency=self.carrier_frequency_hz,
                min_speed=0.0, max_speed=0.0, num_rx_ant=self.num_rx_ant,
                num_tx_ant=1, precision="single", device="cpu")
            a, tau = model(batch_size=1, num_time_steps=1,
                           sampling_frequency=self.scs_hz)
            links.append(cir_to_ofdm_channel(
                self._frequencies, a, tau, normalize=False))
        channel = torch.cat(links, dim=3)
        channel = channel.expand(-1, -1, -1, -1, -1,
                                 self.num_symbols, -1).clone()
        self.last_channel = channel.detach().clone()
        return channel

    def _expand(self, allocation: ResourceAllocation) -> ResourceAllocation:
        expected = (1, 1, self.num_prbs, self.num_ues, 1)
        if allocation.shape != expected:
            raise ValueError(f"allocation shape {allocation.shape} does not match {expected}")
        mask = torch.zeros((1, self.num_symbols, self.fft_size,
                            self.num_ues, 1), dtype=torch.bool)
        for ue in range(self.num_ues):
            chosen = torch.nonzero(allocation.mask[0, 0, :, ue, 0],
                                   as_tuple=False).flatten().tolist()
            if len(chosen) > 1:
                raise ValueError("one UE may occupy at most one PRB per attempt")
            if chosen:
                start = 12 * chosen[0]
                mask[0, :, start:start + 12, ue, 0] = self._prb_data_mask
        return ResourceAllocation(mask)

    def evaluate(self, allocation: ResourceAllocation, *, tx_power_dbm: Sequence[float],
                 mcs_index: Sequence[int], realization_seed: int,
                 channel_identities: Sequence[int] | None = None) -> PhyOutcome:
        started = time.perf_counter()
        if len(tx_power_dbm) != self.num_ues or len(mcs_index) != self.num_ues:
            raise ValueError("one power and MCS value is required per UE")
        if any(int(value) != 10 for value in mcs_index):
            raise ValueError("the V0.4g controlled receiver fixes MCS 10")
        expanded = self._expand(allocation)
        tx_power = dbm_to_watt(torch.tensor(tx_power_dbm, dtype=torch.float32))
        per_symbol = tx_power.reshape(1, 1, self.num_ues).expand(
            1, self.num_symbols, self.num_ues)
        power_grid = spread_across_subcarriers(
            per_symbol, expanded.mask, num_tx=self.num_ues)
        effective_channel = self._precoder(
            self._tdl_channel(int(realization_seed), channel_identities), power_grid)
        shape = (1, self.num_symbols, self.fft_size, self.num_ues, 1)
        sinr = self._lmmse(
            effective_channel, no=torch.tensor(self.noise_power_w),
            interference_whitening=True).reshape(shape)
        mcs = torch.tensor([list(mcs_index)], dtype=torch.int32)
        sinr_eff = self._eesm(
            sinr, mcs, mcs_table_index=1, mcs_category=0)
        active = allocation.allocated_re_per_ue()[0] > 0
        per_re, per_power = [], []
        for ue in range(self.num_ues):
            values = sinr[..., ue, :][expanded.mask[..., ue, :]]
            per_re.append(tuple(float(x) for x in lin_to_db(values).tolist()))
            powers = power_grid[:, ue, ...]
            powers = powers[powers > 0]
            per_power.append(float(powers[0]) if powers.numel() else None)
        eff_raw = lin_to_db(sinr_eff)[0].tolist()
        optional = tuple(float(value) if bool(active[ue]) else None
                         for ue, value in enumerate(eff_raw))
        self.timing_seconds += time.perf_counter() - started
        return PhyOutcome(optional, (None,) * self.num_ues,
                          tuple(0 if x else -1 for x in active.tolist()),
                          (0,) * self.num_ues, tuple(per_re), tuple(per_power),
                          (None,) * self.num_ues)


class ActiveCompactedFrequencySelectivePrbReceiver:
    """Map global UE allocations to active-only Sionna receiver tensors."""

    def __init__(self, config: Mapping[str, Any]) -> None:
        self.num_ues = int(config["num_ues"])
        self.num_prbs = int(config.get("num_prbs", 6))
        self._kernel_config = dict(config)
        self._kernel_config.pop("num_ues", None)
        self._kernels: dict[int, FrequencySelectivePrbReceiver] = {}
        self.timing_seconds = 0.0
        self.last_active_global_ue_ids: tuple[int, ...] = ()
        self.last_compact_shape: tuple[int, ...] | None = None

    def _kernel(self, count: int) -> FrequencySelectivePrbReceiver:
        if count not in self._kernels:
            self._kernels[count] = FrequencySelectivePrbReceiver(
                {"num_ues": count, **self._kernel_config})
        return self._kernels[count]

    def evaluate(self, allocation: ResourceAllocation, *, tx_power_dbm: Sequence[float],
                 mcs_index: Sequence[int], realization_seed: int) -> PhyOutcome:
        expected = (1, 1, self.num_prbs, self.num_ues, 1)
        if allocation.shape != expected:
            raise ValueError(f"allocation shape {allocation.shape} does not match {expected}")
        if len(tx_power_dbm) != self.num_ues or len(mcs_index) != self.num_ues:
            raise ValueError("global power and MCS vectors must match configured UEs")
        active = tuple(int(value) for value in torch.nonzero(
            allocation.allocated_re_per_ue()[0] > 0, as_tuple=False).flatten().tolist())
        if not active:
            return PhyOutcome((None,) * self.num_ues, (None,) * self.num_ues,
                (-1,) * self.num_ues, (0,) * self.num_ues,
                ((),) * self.num_ues, (None,) * self.num_ues,
                (None,) * self.num_ues)
        assignments = []
        for local, global_ue in enumerate(active):
            resources = torch.nonzero(
                allocation.mask[0, 0, :, global_ue, 0], as_tuple=False
            ).flatten().tolist()
            if len(resources) != 1:
                raise ValueError("each active UE must occupy exactly one PRB")
            from ul_access.resource import ResourceAssignment
            assignments.append(ResourceAssignment(local, int(resources[0])))
        compact = ResourceAllocation.from_assignments(
            assignments, num_ofdm_symbols=1, num_subcarriers=self.num_prbs,
            num_ues=len(active))
        self.last_active_global_ue_ids = active
        self.last_compact_shape = compact.shape
        kernel = self._kernel(len(active))
        before = kernel.timing_seconds
        local = kernel.evaluate(
            compact, tx_power_dbm=tuple(tx_power_dbm[ue] for ue in active),
            mcs_index=tuple(mcs_index[ue] for ue in active),
            realization_seed=realization_seed, channel_identities=active)
        self.timing_seconds += kernel.timing_seconds - before

        def expand(values: Sequence[Any], inactive: Any) -> tuple[Any, ...]:
            output = [inactive for _ in range(self.num_ues)]
            for local_ue, global_ue in enumerate(active):
                output[global_ue] = values[local_ue]
            return tuple(output)

        return PhyOutcome(
            expand(local.effective_sinr_db, None), expand(local.tbler, None),
            expand(local.feedback, -1), expand(local.decoded_bits, 0),
            expand(local.post_equalization_sinr_active_db, ()),
            expand(local.tx_power_per_active_re_w, None),
            expand(local.bler, None))


class DirectLlsHarqBackend(HarqPhyBackend):
    """Join one receiver SINR evaluation to one bit-level decode per attempt."""

    RV_SEQUENCE = (0, 2, 3, 1)

    def __init__(self, receiver: FrequencySelectivePrbReceiver |
                 ActiveCompactedFrequencySelectivePrbReceiver, *, run_seed: int,
                 decoder_iterations: int = 20,
                 audit_enabled: bool = False) -> None:
        self.receiver = receiver
        self.run_seed = int(run_seed)
        self.geometry = audit_controlled_pusch_geometry()
        self.crc_encoder = CRCEncoder("CRC16", k=160, precision="single", device="cpu")
        self.crc_decoder = CRCDecoder(self.crc_encoder, precision="single", device="cpu")
        self.encoder = LDPC5GEncoder(176, 504, num_bits_per_symbol=4,
                                     precision="single", device="cpu")
        self.decoder = LDPC5GDecoder(
            self.encoder, num_iter=int(decoder_iterations), harq_mode=True,
            precision="single", device="cpu")
        self.mapper = Mapper("qam", 4, precision="single", device="cpu")
        self.demapper = Demapper("app", "qam", 4, precision="single", device="cpu")
        self.timing_seconds = 0.0
        self.audit_enabled = bool(audit_enabled)
        self.audit_records: list[dict[str, Any]] = []

    @property
    def num_ues(self) -> int: return self.receiver.num_ues

    @property
    def num_subcarriers(self) -> int: return self.receiver.num_prbs

    @property
    def rescue_rv(self) -> int: return 3

    @property
    def history_aware(self) -> bool: return True

    def new_episode_state(self, *, mac_tb_id: str, harq_episode_id: str,
                          mcs_index: int) -> DirectHarqEpisodeState:
        if int(mcs_index) != 10:
            raise ValueError("direct LLS fixes MCS 10")
        # Simulator MAC-TB IDs end in a context generation. Removing that
        # suffix keeps the higher-layer payload bits fixed across an F reset.
        payload_identity = mac_tb_id.rsplit("-", 1)[0]
        generator = torch.Generator(device="cpu").manual_seed(addressed_seed(
            self.run_seed, "payload_bits", payload_identity, bits=64))
        payload = torch.randint(0, 2, (1, 160), generator=generator,
                                dtype=torch.float32)
        return DirectHarqEpisodeState(mac_tb_id, harq_episode_id, 10, payload)

    def rv_for_attempt(self, attempt_index: int) -> int:
        if not 0 <= attempt_index < 4:
            raise ValueError("attempt index exceeds RV sequence")
        return self.RV_SEQUENCE[attempt_index]

    def evaluate(self, allocation: ResourceAllocation, **kwargs: Any) -> PhyOutcome:
        return self.receiver.evaluate(allocation, **kwargs)

    def _observe(self, bits: torch.Tensor, sinr_db: float, seed: int) -> torch.Tensor:
        symbols = self.mapper(bits)
        no = torch.tensor(10.0 ** (-sinr_db / 10.0), dtype=torch.float32)
        generator = torch.Generator(device="cpu").manual_seed(seed)
        noise = torch.complex(
            torch.randn(symbols.shape, generator=generator),
            torch.randn(symbols.shape, generator=generator)) * torch.sqrt(no / 2)
        return self.demapper(symbols + noise, no)

    def evaluate_harq_history(self, allocation: ResourceAllocation, *, tx_power_dbm,
                              mcs_index, realization_seed,
                              states: Sequence[DirectHarqEpisodeState | None],
                              rvs: Sequence[int | None]) -> PhyOutcome:
        ordinary = self.receiver.evaluate(
            allocation, tx_power_dbm=tx_power_dbm, mcs_index=mcs_index,
            realization_seed=realization_seed)
        started = time.perf_counter()
        feedback = [-1] * self.num_ues
        decoded_bits = [0] * self.num_ues
        crc_passes: list[bool | None] = [None] * self.num_ues
        payload_correctness: list[bool | None] = [None] * self.num_ues
        undetected_errors: list[bool | None] = [None] * self.num_ues
        payload_hamming_distances: list[int | None] = [None] * self.num_ues
        for ue, state in enumerate(states):
            if state is None:
                continue
            rv, sinr_db = rvs[ue], ordinary.effective_sinr_db[ue]
            if rv is None or sinr_db is None:
                raise ValueError("active direct-LLS state requires RV and SINR")
            if state.pending_llr is not None:
                raise RuntimeError("previous LLR observation was not committed")
            expected_rv = self.rv_for_attempt(len(state.rv_history))
            if int(rv) != expected_rv:
                raise ValueError(f"expected RV {expected_rv}, received {rv}")
            tb_bits = self.crc_encoder(state.payload_bits)
            codeword = self.encoder(tb_bits, rv=[int(rv)])[:, 0, :]
            noise_seed = addressed_seed(
                self.run_seed, "lls_awgn", state.harq_episode_id,
                len(state.rv_history), bits=64)
            current_llr = self._observe(codeword, float(sinr_db), noise_seed)
            history = (*state.llr_history, current_llr)
            decoder_input = history[0] if len(history) == 1 else torch.stack(history, dim=1)
            if self.audit_enabled:
                self.audit_records.append({
                    "mac_tb_id": state.mac_tb_id,
                    "harq_episode_id": state.harq_episode_id,
                    "rv_history": list((*state.rv_history, int(rv))),
                    "decoder_input_shape": list(decoder_input.shape),
                    "llr_observation_shapes": [list(value.shape)
                                               for value in history],
                    "rate_matching_start_positions":
                        self.encoder.get_start_positions_comp(
                            list((*state.rv_history, int(rv)))),
                })
            decoded_tb = self.decoder(
                decoder_input, rv=list((*state.rv_history, int(rv))))
            decoded_payload, crc_ok = self.crc_decoder(decoded_tb)
            crc_pass = bool(crc_ok.reshape(-1)[0].item())
            (feedback[ue], payload_correctness[ue], undetected_errors[ue],
             payload_hamming_distances[ue]) = classify_crc_oracle_outcome(
                 crc_pass, decoded_payload, state.payload_bits)
            crc_passes[ue] = crc_pass
            decoded_bits[ue] = 160 if crc_pass else 0
            state.pending_rv = int(rv)
            state.pending_sinr_db = float(sinr_db)
            state.pending_llr = current_llr.detach().clone()
        self.timing_seconds += time.perf_counter() - started
        return PhyOutcome(
            ordinary.effective_sinr_db, (None,) * self.num_ues,
            tuple(feedback), tuple(decoded_bits),
            ordinary.post_equalization_sinr_active_db,
            ordinary.tx_power_per_active_re_w, (None,) * self.num_ues,
            tuple(crc_passes), tuple(payload_correctness),
            tuple(undetected_errors), tuple(payload_hamming_distances))

    def append_episode_state(self, state: DirectHarqEpisodeState, *, rv: int,
                             effective_sinr_db: float) -> DirectHarqEpisodeState:
        if state.pending_llr is None or state.pending_rv != int(rv):
            raise RuntimeError("direct LLS has no matching pending observation")
        state.rv_history = (*state.rv_history, int(rv))
        state.effective_sinr_history_db = (
            *state.effective_sinr_history_db, float(effective_sinr_db))
        state.llr_history = (*state.llr_history, state.pending_llr)
        state.pending_rv = None
        state.pending_sinr_db = None
        state.pending_llr = None
        return state

    @staticmethod
    def state_trace(state: DirectHarqEpisodeState) -> dict[str, Any]:
        payload_hash = hashlib.sha256(
            state.payload_bits.to(torch.uint8).numpy().tobytes()).hexdigest()
        return {"rv_history": list(state.rv_history),
                "effective_sinr_history_db": list(state.effective_sinr_history_db),
                "harq_information_state": {
                    "representation": "retained_rate-matched_llr_vectors",
                    "llr_vector_count": len(state.llr_history),
                    "llr_vector_length": 504,
                    "payload_sha256": payload_hash}}

    def capture_failed_episode(self, **kwargs: Any) -> DirectHarqEpisodeState:
        raise TypeError("direct LLS captures every attempt through history evaluation")

    def evaluate_harq(self, *args: Any, **kwargs: Any) -> PhyOutcome:
        raise TypeError("direct LLS uses history evaluation for every attempt")
