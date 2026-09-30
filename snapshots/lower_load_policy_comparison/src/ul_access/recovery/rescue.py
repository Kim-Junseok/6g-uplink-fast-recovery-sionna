"""Deterministic first-NACK scheduled-rescue configuration."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ScheduledRescueMode(str, Enum):
    """MAC/HARQ context semantics used by scheduled rescue."""

    HARQ_PRESERVING = "harq_preserving"
    FRESH_TB = "fresh_tb"


@dataclass(frozen=True)
class ScheduledRescueConfig:
    """Abstract SR-like request and non-CB grant timing.

    Rescue is activated by the first eligible NACK following a Grant-Free
    transmission. Any remaining retransmission in that rescue episode also
    uses scheduled resources. This is a research control abstraction rather
    than a bit-exact NR Scheduling Request procedure.
    """

    mode: ScheduledRescueMode
    request_delay_slots: int
    scheduled_grant_delay_slots: int
    rescue_mcs_index: int | None = None
    trigger_after_attempts: int = 1
    maximum_physical_attempts_per_recovery_episode: int | None = None
    reset_rescue_on_rlc_restart: bool = False

    def __post_init__(self) -> None:
        if self.request_delay_slots < 0:
            raise ValueError("request_delay_slots must be non-negative")
        if self.scheduled_grant_delay_slots < 0:
            raise ValueError("scheduled_grant_delay_slots must be non-negative")
        if self.rescue_mcs_index is not None and self.rescue_mcs_index < 0:
            raise ValueError("rescue_mcs_index must be non-negative")
        if self.trigger_after_attempts <= 0:
            raise ValueError("trigger_after_attempts must be positive")
        if (self.maximum_physical_attempts_per_recovery_episode is not None and
                self.maximum_physical_attempts_per_recovery_episode <= 0):
            raise ValueError("maximum physical attempts must be positive")

    @property
    def name(self) -> str:
        return self.mode.value
