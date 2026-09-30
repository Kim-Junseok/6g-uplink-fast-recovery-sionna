"""Event-addressed post-termination Fast-CB reinjection timing."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from ul_access.protocol.models import Packet
from ul_access.recovery.fast_arq import FastArqPolicy
from ul_access.recovery.interface import RecoveryDecision


BACKOFF_RNG_DOMAIN = "v0.8-fast-cb-reinjection-opportunities-v1"


@dataclass(frozen=True)
class ReinjectionDecision:
    payload_id: int
    recovery_index: int
    failure_slot: int
    original_eligible_slot: int
    original_first_opportunity: int
    additional_opportunities_skipped: int
    target_opportunity: int


class FastCbReinjectionBackoffPolicy(FastArqPolicy):
    """Delay a fresh RLC episode by B additional eligible CB opportunities."""

    def __init__(self, *, failure_indication_delay_slots: int, seed: int,
                 opportunity_period_slots: int = 1,
                 forced_backoff: int | None = None) -> None:
        super().__init__(
            failure_indication_delay_slots=failure_indication_delay_slots)
        if opportunity_period_slots <= 0:
            raise ValueError("opportunity_period_slots must be positive")
        if forced_backoff is not None and forced_backoff not in range(4):
            raise ValueError("forced_backoff must be 0, 1, 2, or 3")
        self.seed = int(seed)
        self.opportunity_period_slots = int(opportunity_period_slots)
        self.forced_backoff = forced_backoff
        self.decisions: list[ReinjectionDecision] = []

    @staticmethod
    def first_opportunity(eligible_slot: int, period: int) -> int:
        return ((eligible_slot + period - 1) // period) * period

    def draw(self, payload_id: int, recovery_index: int) -> int:
        if self.forced_backoff is not None:
            return self.forced_backoff
        raw = (f"{BACKOFF_RNG_DOMAIN}\0{self.seed}\0{payload_id}\0"
               f"{recovery_index}").encode()
        return int.from_bytes(
            hashlib.blake2s(raw, digest_size=8).digest(), "big") % 4

    def on_final_harq_failure(
        self, pdu: Packet, current_slot: int
    ) -> RecoveryDecision:
        original = super().on_final_harq_failure(pdu, current_slot)
        first = self.first_opportunity(
            original.eligible_slot, self.opportunity_period_slots)
        recovery_index = pdu.rlc_retransmission_count + 1
        backoff = self.draw(pdu.higher_layer_payload_id, recovery_index)
        target = first + backoff * self.opportunity_period_slots
        self.decisions.append(ReinjectionDecision(
            payload_id=pdu.higher_layer_payload_id,
            recovery_index=recovery_index,
            failure_slot=current_slot,
            original_eligible_slot=original.eligible_slot,
            original_first_opportunity=first,
            additional_opportunities_skipped=backoff,
            target_opportunity=target))
        # B=0 retains the original eligibility slot and wait accounting.
        return RecoveryDecision(
            original.eligible_slot if backoff == 0 else target,
            original.event_name)
