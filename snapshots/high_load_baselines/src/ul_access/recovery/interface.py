"""Research-owned post-HARQ recovery policy interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ul_access.protocol.models import Packet


@dataclass(frozen=True)
class RecoveryDecision:
    """Slot at which an exhausted PDU may re-enter access."""

    eligible_slot: int
    event_name: str
    timer_expiry_slot: int | None = None


class RecoveryPolicy(ABC):
    """Determine when RLC retransmission becomes eligible after HARQ exhaustion."""

    @abstractmethod
    def on_final_harq_failure(
        self, pdu: Packet, current_slot: int
    ) -> RecoveryDecision:
        """Return a deterministic post-final-HARQ recovery decision."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable policy name used in serialized results."""
