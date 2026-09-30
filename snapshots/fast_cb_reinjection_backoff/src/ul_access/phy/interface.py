"""Sionna-independent PHY contract used by protocol simulations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Sequence

from ul_access.phy.harq_abstraction import HarqEpisodeState
from ul_access.phy.harq_mi_abstraction import HarqMiEpisodeState

from ul_access.resource import ResourceAllocation


@dataclass(frozen=True)
class PhyOutcome:
    """Scientifically meaningful per-UE outputs from one PHY evaluation."""

    effective_sinr_db: tuple[float | None, ...]
    tbler: tuple[float | None, ...]
    feedback: tuple[int, ...]
    decoded_bits: tuple[int, ...]
    post_equalization_sinr_active_db: tuple[tuple[float, ...], ...]
    tx_power_per_active_re_w: tuple[float | None, ...]
    bler: tuple[float | None, ...]
    crc_pass: tuple[bool | None, ...] = ()
    payload_correct: tuple[bool | None, ...] = ()
    undetected_error: tuple[bool | None, ...] = ()
    payload_hamming_distance: tuple[int | None, ...] = ()

    @property
    def success(self) -> tuple[bool, ...]:
        """Return whether each UE received an ACK."""
        return tuple(value == 1 for value in self.feedback)


class PhyBackend(ABC):
    """Abstract physical-layer evaluator for access simulations."""

    @property
    @abstractmethod
    def num_ues(self) -> int:
        """Number of UE identities supported by this backend."""

    @property
    @abstractmethod
    def num_subcarriers(self) -> int:
        """Number of allocatable frequency resources."""

    @abstractmethod
    def evaluate(
        self,
        allocation: ResourceAllocation,
        *,
        tx_power_dbm: Sequence[float],
        mcs_index: Sequence[int],
        realization_seed: int,
    ) -> PhyOutcome:
        """Evaluate one allocation and return per-UE PHY outcomes."""


class HarqPhyBackend(PhyBackend):
    """Optional extension for a validated compact HARQ-combining backend."""

    @property
    @abstractmethod
    def rescue_rv(self) -> int:
        """Redundancy version used by the calibrated rescue model."""

    @property
    @abstractmethod
    def history_aware(self) -> bool:
        """Whether every transmission uses a length-1--4 history mapping."""

    @abstractmethod
    def new_episode_state(self, *, mac_tb_id: str, harq_episode_id: str,
                          mcs_index: int) -> HarqEpisodeState | HarqMiEpisodeState:
        """Create an empty compact state for a new TB/HARQ episode."""

    @abstractmethod
    def rv_for_attempt(self, attempt_index: int) -> int:
        """Return the calibrated RV for a zero-based episode attempt."""

    @abstractmethod
    def evaluate_harq_history(
        self, allocation: ResourceAllocation, *, tx_power_dbm: Sequence[float],
        mcs_index: Sequence[int], realization_seed: int,
        states: Sequence[HarqEpisodeState | HarqMiEpisodeState | None],
        rvs: Sequence[int | None],
    ) -> PhyOutcome:
        """Evaluate current receiver SINR and the complete compact histories."""

    @abstractmethod
    def append_episode_state(
        self, state: HarqEpisodeState | HarqMiEpisodeState, *, rv: int,
        effective_sinr_db: float,
    ) -> HarqEpisodeState | HarqMiEpisodeState:
        """Accumulate one observation without retaining decoder tensors."""

    @abstractmethod
    def state_trace(self, state: HarqEpisodeState | HarqMiEpisodeState) -> dict:
        """Return serializable diagnostics for one compact state."""

    @abstractmethod
    def capture_failed_episode(
        self, *, mac_tb_id: str, harq_episode_id: str, mcs_index: int,
        outcome: PhyOutcome, ue_id: int,
    ) -> HarqEpisodeState:
        """Capture compact state after a failed first transmission."""

    @abstractmethod
    def evaluate_harq(
        self, allocation: ResourceAllocation, *, tx_power_dbm: Sequence[float],
        mcs_index: Sequence[int], realization_seed: int,
        states: Sequence[HarqEpisodeState | None], rescue_rv: int,
    ) -> PhyOutcome:
        """Evaluate the current links and apply calibrated combining where set."""
