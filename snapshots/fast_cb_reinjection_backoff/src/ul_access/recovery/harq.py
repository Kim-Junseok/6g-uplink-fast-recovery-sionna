"""Explicit stop-and-wait HARQ timing abstraction."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HarqProcess:
    """State associated with one packet's current HARQ episode."""

    attempt_index: int
    maximum_attempts: int
    feedback_due_slot: int

    @property
    def attempts_remain(self) -> bool:
        return self.attempt_index < self.maximum_attempts


class HarqController:
    """Create and classify HARQ attempts; attempt one is the initial attempt."""

    def __init__(self, *, max_attempts: int, feedback_delay_slots: int,
                 pusch_spacing_slots: int | None = None) -> None:
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if feedback_delay_slots < 0:
            raise ValueError("feedback_delay_slots must be non-negative")
        if pusch_spacing_slots is not None and pusch_spacing_slots < 0:
            raise ValueError("PUSCH spacing must be non-negative")
        self.max_attempts = int(max_attempts)
        self.feedback_delay_slots = int(feedback_delay_slots)
        self.pusch_spacing_slots = (
            int(pusch_spacing_slots) if pusch_spacing_slots is not None
            else self.feedback_delay_slots)

    def transmitted(self, attempt_index: int, slot: int) -> HarqProcess:
        if not 1 <= attempt_index <= self.max_attempts:
            raise ValueError("HARQ attempt index is outside the configured episode")
        return HarqProcess(
            attempt_index=attempt_index,
            maximum_attempts=self.max_attempts,
            feedback_due_slot=slot + self.feedback_delay_slots,
        )

    def next_pusch_slot(self, previous_tx_slot: int) -> int:
        """Return the earliest next PUSCH slot after an actual transmission."""
        return int(previous_tx_slot) + self.pusch_spacing_slots
