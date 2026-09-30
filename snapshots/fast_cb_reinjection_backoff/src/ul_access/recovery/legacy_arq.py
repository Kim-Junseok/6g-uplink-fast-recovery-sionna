"""Delayed conventional RLC-recovery abstractions."""

import hashlib

from ul_access.protocol.models import Packet
from ul_access.recovery.interface import RecoveryDecision, RecoveryPolicy


class LegacyArqPolicy(RecoveryPolicy):
    def __init__(self, *, recovery_delay_slots: int) -> None:
        if recovery_delay_slots < 0:
            raise ValueError("recovery_delay_slots must be non-negative")
        self.recovery_delay_slots = int(recovery_delay_slots)

    @property
    def name(self) -> str:
        return "legacy_arq"

    def on_final_harq_failure(
        self, pdu: Packet, current_slot: int
    ) -> RecoveryDecision:
        del pdu
        return RecoveryDecision(
            current_slot + self.recovery_delay_slots,
            "legacy_arq_recovery_trigger",
        )


class PollRetransmitPolicy(RecoveryPolicy):
    """Controlled t-PollRetransmit eligibility measured from first RLC TX."""

    def __init__(self, *, poll_retransmit_slots: int = 20) -> None:
        if poll_retransmit_slots < 0:
            raise ValueError("poll_retransmit_slots must be non-negative")
        self.poll_retransmit_slots = int(poll_retransmit_slots)

    @property
    def name(self) -> str:
        return "legacy_arq"

    def on_final_harq_failure(self, pdu: Packet,
                              current_slot: int) -> RecoveryDecision:
        if pdu.first_tx_slot is None:
            raise RuntimeError("legacy timer requires a first transmission slot")
        timer_expiry = pdu.first_tx_slot + self.poll_retransmit_slots
        eligible = max(timer_expiry, current_slot) + 1
        return RecoveryDecision(
            eligible, "legacy_poll_retransmit_expiry", timer_expiry)


class LegacyRlcScheduledPolicy(RecoveryPolicy):
    """Timer-driven RLC recovery through periodic SR and scheduled PUSCH."""

    def __init__(self, *, poll_retransmit_slots: int,
                 sr_period_slots: int = 10,
                 sr_control_processing_slots: int = 1,
                 scheduled_pusch_k2_slots: int = 1,
                 sr_phase_domain: str = "v0.5-sr-phase") -> None:
        if poll_retransmit_slots <= 0 or sr_period_slots <= 0:
            raise ValueError("timer and SR period must be positive")
        if sr_control_processing_slots < 0 or scheduled_pusch_k2_slots < 0:
            raise ValueError("control processing and K2 must be non-negative")
        self.poll_retransmit_slots = int(poll_retransmit_slots)
        self.sr_period_slots = int(sr_period_slots)
        self.sr_control_processing_slots = int(sr_control_processing_slots)
        self.scheduled_pusch_k2_slots = int(scheduled_pusch_k2_slots)
        self.sr_phase_domain = str(sr_phase_domain)

    @property
    def name(self) -> str:
        return "legacy_rlc_scheduled"

    def on_final_harq_failure(self, pdu: Packet,
                              current_slot: int) -> RecoveryDecision:
        """Expose timer state; the simulator advances it independently."""
        del current_slot
        if pdu.rlc_timer_expiry_slot is None:
            raise RuntimeError("Legacy RLC timer has not started")
        return RecoveryDecision(
            pdu.rlc_timer_expiry_slot, "legacy_rlc_timer_pending",
            pdu.rlc_timer_expiry_slot)

    def timer_expiry_slot(self, timer_start_slot: int) -> int:
        return int(timer_start_slot) + self.poll_retransmit_slots

    def sr_phase(self, global_ue_id: int) -> int:
        raw = f"{self.sr_phase_domain}\0{int(global_ue_id)}".encode()
        value = int.from_bytes(
            hashlib.blake2s(raw, digest_size=8).digest(), "big")
        return value % self.sr_period_slots

    def next_sr_slot(self, eligible_slot: int, global_ue_id: int) -> int:
        phase = self.sr_phase(global_ue_id)
        wait = (phase - int(eligible_slot)) % self.sr_period_slots
        return int(eligible_slot) + wait

    def earliest_scheduled_pusch_slot(self, sr_slot: int) -> int:
        return (int(sr_slot) + self.sr_control_processing_slots +
                self.scheduled_pusch_k2_slots)
