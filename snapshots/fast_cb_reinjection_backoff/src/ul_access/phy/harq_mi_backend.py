"""System-level backend for the validated compact V0.4e MI-HARQ mapping."""

from __future__ import annotations

import hashlib
import random
from typing import Sequence

from ul_access.phy.harq_mi_abstraction import (
    BicmInformationTable, HarqInformationState, HarqMiEpisodeState,
    HarqMiLogisticModel, RateMatchingProfile)
from ul_access.phy.interface import HarqPhyBackend, PhyBackend, PhyOutcome
from ul_access.resource import ResourceAllocation


class MiHarqPhyBackend(HarqPhyBackend):
    """Apply MI-C after one ordinary LMMSE-to-EESM evaluation per attempt."""

    def __init__(self, base: PhyBackend, *, model: HarqMiLogisticModel,
                 table: BicmInformationTable, profile: RateMatchingProfile,
                 mcs_index: int, information_bits: int, coded_bits: int,
                 resource_units_per_transmission: int = 1) -> None:
        self.base = base
        self.model = model
        self.table = table
        self.profile = profile
        self.mcs_index = int(mcs_index)
        self.information_bits = int(information_bits)
        self.coded_bits = int(coded_bits)
        self.resource_units_per_transmission = int(resource_units_per_transmission)

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
        return True

    def new_episode_state(self, *, mac_tb_id: str, harq_episode_id: str,
                          mcs_index: int) -> HarqMiEpisodeState:
        if int(mcs_index) != self.mcs_index:
            raise ValueError("MCS does not match validated MI-HARQ model")
        return HarqMiEpisodeState(
            mac_tb_id=mac_tb_id, harq_episode_id=harq_episode_id,
            mcs_index=int(mcs_index), information_bits=self.information_bits,
            coded_bits=self.coded_bits, information_state=HarqInformationState(),
            resource_units_per_transmission=self.resource_units_per_transmission)

    def rv_for_attempt(self, attempt_index: int) -> int:
        if not 0 <= attempt_index < len(self.model.rv_sequence):
            raise ValueError("attempt index exceeds validated RV sequence")
        return self.model.rv_sequence[attempt_index]

    def evaluate(self, allocation: ResourceAllocation, *, tx_power_dbm,
                 mcs_index, realization_seed) -> PhyOutcome:
        return self.base.evaluate(
            allocation, tx_power_dbm=tx_power_dbm, mcs_index=mcs_index,
            realization_seed=realization_seed)

    def append_episode_state(self, state: HarqMiEpisodeState, *, rv: int,
                             effective_sinr_db: float) -> HarqMiEpisodeState:
        if not isinstance(state, HarqMiEpisodeState):
            raise TypeError("MI backend requires HarqMiEpisodeState")
        return HarqMiEpisodeState(
            mac_tb_id=state.mac_tb_id, harq_episode_id=state.harq_episode_id,
            mcs_index=state.mcs_index, information_bits=state.information_bits,
            coded_bits=state.coded_bits,
            information_state=state.information_state.append(
                rv=rv, sinr_db=effective_sinr_db, table=self.table,
                profile=self.profile),
            resource_units_per_transmission=state.resource_units_per_transmission)

    @staticmethod
    def state_trace(state: HarqMiEpisodeState) -> dict:
        value = state.information_state
        return {"rv_history": list(value.rv_history),
                "harq_information_state": {
                    "new_bit_information": value.new_bit_information,
                    "repeated_bit_information": value.repeated_bit_information,
                    "latest_transmission_information":
                        value.latest_transmission_information,
                    "total_information": value.total_information,
                    "unique_coded_bits": value.unique_coded_bits,
                    "effective_code_rate": value.effective_code_rate(
                        state.information_bits)}}

    @staticmethod
    def _sample(seed: int, episode: str, probability: float) -> bool:
        digest = hashlib.blake2s(
            f"v04e-mi-harq\0{seed}\0{episode}".encode(), digest_size=8).digest()
        return random.Random(int.from_bytes(digest, "big")).random() < probability

    def evaluate_harq_history(self, allocation: ResourceAllocation, *, tx_power_dbm,
                              mcs_index, realization_seed,
                              states: Sequence[HarqMiEpisodeState | None],
                              rvs: Sequence[int | None]) -> PhyOutcome:
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
                raise ValueError("active MI-HARQ state requires current SINR and RV")
            if int(mcs_index[ue_id]) != state.mcs_index:
                raise ValueError("MCS changed within a HARQ episode")
            updated = self.append_episode_state(
                state, rv=int(rv), effective_sinr_db=float(current))
            probability = self.model.probability(
                updated.information_state, self.information_bits)
            success = self._sample(realization_seed,
                f"{state.harq_episode_id}:{len(updated.rv_history)}", probability)
            feedback[ue_id] = int(success)
            decoded[ue_id] = self.information_bits if success else 0
            tbler[ue_id] = bler[ue_id] = 1.0-probability
        return PhyOutcome(
            effective_sinr_db=ordinary.effective_sinr_db, tbler=tuple(tbler),
            feedback=tuple(feedback), decoded_bits=tuple(decoded),
            post_equalization_sinr_active_db=ordinary.post_equalization_sinr_active_db,
            tx_power_per_active_re_w=ordinary.tx_power_per_active_re_w,
            bler=tuple(bler))

    def capture_failed_episode(self, **kwargs) -> HarqMiEpisodeState:
        raise TypeError("MI-HARQ captures every attempt through history evaluation")

    def evaluate_harq(self, *args, **kwargs) -> PhyOutcome:
        raise TypeError("MI-HARQ uses history evaluation for every attempt")
