"""System-level backend decorator for validated compact HARQ combining."""

from __future__ import annotations

import hashlib
import random
from typing import Sequence

from ul_access.phy.harq_abstraction import (
    CalibratedLogisticHarqModel,
    HarqEpisodeState,
    HarqHistoryLogisticModel,
)
from ul_access.phy.interface import HarqPhyBackend, PhyBackend, PhyOutcome
from ul_access.resource import ResourceAllocation


class CalibratedHarqPhyBackend(HarqPhyBackend):
    """Wrap an ordinary backend and replace only eligible H-rescue outcomes."""

    def __init__(self, base: PhyBackend,
                 model: CalibratedLogisticHarqModel | HarqHistoryLogisticModel) -> None:
        self.base = base
        self.model = model

    @property
    def num_ues(self) -> int:
        return self.base.num_ues

    @property
    def num_subcarriers(self) -> int:
        return self.base.num_subcarriers

    @property
    def rescue_rv(self) -> int:
        return self.model.rv_sequence[-1]

    @property
    def history_aware(self) -> bool:
        return isinstance(self.model, HarqHistoryLogisticModel)

    def new_episode_state(self, *, mac_tb_id: str, harq_episode_id: str,
                          mcs_index: int) -> HarqEpisodeState:
        if not self.history_aware:
            raise TypeError("empty episode state is available only for V0.4c")
        if mcs_index != self.model.mcs_index:
            raise ValueError("MCS does not match calibrated HARQ model")
        return HarqEpisodeState(
            mac_tb_id=mac_tb_id, harq_episode_id=harq_episode_id,
            mcs_index=mcs_index, information_bits=self.model.information_bits,
            coded_bits=self.model.coded_bits, rv_history=(),
            effective_sinr_history_db=(),
            resource_units_per_transmission=self.model.resource_units_per_transmission)

    def rv_for_attempt(self, attempt_index: int) -> int:
        if not 0 <= attempt_index < len(self.model.rv_sequence):
            raise ValueError("attempt index exceeds calibrated RV sequence")
        return self.model.rv_sequence[attempt_index]

    def evaluate(self, allocation: ResourceAllocation, *, tx_power_dbm,
                 mcs_index, realization_seed) -> PhyOutcome:
        return self.base.evaluate(
            allocation, tx_power_dbm=tx_power_dbm, mcs_index=mcs_index,
            realization_seed=realization_seed)

    def capture_failed_episode(self, *, mac_tb_id: str, harq_episode_id: str,
                               mcs_index: int, outcome: PhyOutcome,
                               ue_id: int) -> HarqEpisodeState:
        sinr = outcome.effective_sinr_db[ue_id]
        if sinr is None:
            raise ValueError("cannot retain HARQ state for an inactive UE")
        if mcs_index != self.model.mcs_index:
            raise ValueError("MCS does not match calibrated HARQ model")
        return HarqEpisodeState(
            mac_tb_id=mac_tb_id, harq_episode_id=harq_episode_id,
            mcs_index=mcs_index, information_bits=self.model.information_bits,
            coded_bits=self.model.coded_bits,
            rv_history=(self.model.rv_sequence[0],),
            effective_sinr_history_db=(float(sinr),))

    @staticmethod
    def _sample(seed: int, episode: str, probability: float) -> bool:
        digest = hashlib.blake2s(
            f"v04b-harq\0{seed}\0{episode}".encode(), digest_size=8).digest()
        return random.Random(int.from_bytes(digest, "big")).random() < probability

    def evaluate_harq_history(self, allocation: ResourceAllocation, *, tx_power_dbm,
                              mcs_index, realization_seed,
                              states: Sequence[HarqEpisodeState | None],
                              rvs: Sequence[int | None]) -> PhyOutcome:
        if not self.history_aware:
            raise TypeError("history evaluation requires the V0.4c model")
        if len(states) != self.num_ues or len(rvs) != self.num_ues:
            raise ValueError("states and rvs must contain one entry per UE")
        ordinary = self.evaluate(
            allocation, tx_power_dbm=tx_power_dbm, mcs_index=mcs_index,
            realization_seed=realization_seed)
        feedback, decoded = list(ordinary.feedback), list(ordinary.decoded_bits)
        tbler, bler = list(ordinary.tbler), list(ordinary.bler)
        for ue_id, state in enumerate(states):
            if state is None:
                continue
            current, rv = ordinary.effective_sinr_db[ue_id], rvs[ue_id]
            if current is None or rv is None:
                raise ValueError("active HARQ state requires current SINR and RV")
            if int(mcs_index[ue_id]) != state.mcs_index:
                raise ValueError("MCS changed within a HARQ episode")
            probability = self.model.probability(
                state, rv=rv, effective_sinr_db=current)
            success = self._sample(
                realization_seed, f"{state.harq_episode_id}:{len(state.rv_history)+1}",
                probability)
            feedback[ue_id] = int(success)
            decoded[ue_id] = self.model.information_bits if success else 0
            tbler[ue_id] = bler[ue_id] = 1.0 - probability
        return PhyOutcome(
            effective_sinr_db=ordinary.effective_sinr_db,
            tbler=tuple(tbler), feedback=tuple(feedback), decoded_bits=tuple(decoded),
            post_equalization_sinr_active_db=ordinary.post_equalization_sinr_active_db,
            tx_power_per_active_re_w=ordinary.tx_power_per_active_re_w,
            bler=tuple(bler))

    def append_episode_state(self, state: HarqEpisodeState, *, rv: int,
                             effective_sinr_db: float) -> HarqEpisodeState:
        return state.append(rv=rv, effective_sinr_db=effective_sinr_db)

    @staticmethod
    def state_trace(state: HarqEpisodeState) -> dict:
        return {"rv_history": list(state.rv_history),
                "effective_sinr_history_db": list(
                    state.effective_sinr_history_db)}

    def evaluate_harq(self, allocation: ResourceAllocation, *, tx_power_dbm,
                      mcs_index, realization_seed,
                      states: Sequence[HarqEpisodeState | None],
                      rescue_rv: int) -> PhyOutcome:
        if len(states) != self.num_ues:
            raise ValueError("states must contain one entry per UE")
        ordinary = self.evaluate(
            allocation, tx_power_dbm=tx_power_dbm, mcs_index=mcs_index,
            realization_seed=realization_seed)
        feedback = list(ordinary.feedback)
        decoded = list(ordinary.decoded_bits)
        tbler = list(ordinary.tbler)
        bler = list(ordinary.bler)
        for ue_id, state in enumerate(states):
            if state is None:
                continue
            current = ordinary.effective_sinr_db[ue_id]
            if current is None:
                raise ValueError("HARQ state was supplied for an inactive UE")
            probability = self.model.probability(
                state, rescue_rv=rescue_rv,
                rescue_effective_sinr_db=current)
            success = self._sample(realization_seed, state.harq_episode_id, probability)
            feedback[ue_id] = int(success)
            decoded[ue_id] = self.model.information_bits if success else 0
            tbler[ue_id] = 1.0 - probability
            bler[ue_id] = 1.0 - probability
        return PhyOutcome(
            effective_sinr_db=ordinary.effective_sinr_db,
            tbler=tuple(tbler), feedback=tuple(feedback),
            decoded_bits=tuple(decoded),
            post_equalization_sinr_active_db=ordinary.post_equalization_sinr_active_db,
            tx_power_per_active_re_w=ordinary.tx_power_per_active_re_w,
            bler=tuple(bler))
