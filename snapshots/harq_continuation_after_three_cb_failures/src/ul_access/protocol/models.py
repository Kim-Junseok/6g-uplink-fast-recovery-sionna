"""Packet and queue models for slot-based access simulation."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum


class PacketState(str, Enum):
    ACCESS_WAIT = "access_wait"
    HARQ_FEEDBACK_WAIT = "harq_feedback_wait"
    HARQ_RETX_ACCESS_WAIT = "harq_retx_access_wait"
    FINAL_HARQ_FAILURE = "final_harq_failure"
    RLC_RECOVERY_WAIT = "rlc_recovery_wait"
    RLC_RETX_ACCESS_WAIT = "rlc_retx_access_wait"
    RESCUE_REQUEST_WAIT = "rescue_request_wait"
    RESCUE_GRANT_WAIT = "rescue_grant_wait"
    DELIVERED = "delivered"
    DROPPED = "dropped"


@dataclass
class Packet:
    """Minimal packet state tracked from arrival to completion or drop."""

    packet_id: int
    ue_id: int
    arrival_slot: int
    size_bits: int
    attempt_count: int = 0
    harq_attempt_index: int = 0
    physical_attempts_in_recovery_episode: int = 0
    rlc_retransmission_count: int = 0
    completion_slot: int | None = None
    first_tx_slot: int | None = None
    next_eligible_slot: int = 0
    request_slot: int | None = None
    grant_ready_slot: int | None = None
    state: PacketState = PacketState.ACCESS_WAIT
    feedback_due_slot: int | None = None
    feedback_value: int | None = None
    last_tx_slot: int | None = None
    harq_feedback_wait_slots: int = 0
    harq_retransmission_wait_slots: int = 0
    rlc_recovery_wait_slots: int = 0
    access_wait_slots: int = 0
    last_state_slot: int = 0
    drop_reason: str | None = None
    measurement_cohort: bool = True
    measurement_cohort_locked: bool = False
    continuous_arrival_time_slots: float | None = None
    # ``packet_id`` is the higher-layer payload / modeled RLC-PDU identity.
    # A payload can acquire new MAC-TB and HARQ episode IDs when its
    # transmission context is deliberately restarted.
    mac_tb_id: str | None = None
    harq_episode_id: str | None = None
    transmission_context_generation: int = 0
    rescue_triggered: bool = False
    fast_arq_triggered: bool = False
    scheduled_queue_wait_slots: int = 0
    scheduled_transmission_count: int = 0
    harq_termination_slot: int | None = None
    rescue_active: bool = False
    rescue_request_due_slot: int | None = None
    rescue_grant_ready_slot: int | None = None
    last_transmission_was_rescue: bool = False
    last_transmission_access: str | None = None
    last_transmission_mcs: int | None = None
    receiver_accepted: bool = False
    payload_correct: bool = False
    undetected_error: bool = False
    initial_harq_final_failure_slot: int | None = None
    fast_arq_trigger_slot: int | None = None
    scheduled_request_slot: int | None = None
    scheduled_grant_ready_slot: int | None = None
    scheduled_queue_entry_slot: int | None = None
    scheduled_first_tx_slot: int | None = None
    scheduled_completion_slot: int | None = None
    legacy_timer_expiry_slot: int | None = None
    legacy_recovery_eligible_slot: int | None = None
    next_cb_recovery_tx_slot: int | None = None
    first_recovery_harq_termination_slot: int | None = None
    rlc_timer_start_slot: int | None = None
    rlc_timer_expiry_slot: int | None = None
    sr_eligible_slot: int | None = None
    sr_phase: int | None = None
    actual_sr_slot: int | None = None
    grant_processing_complete_slot: int | None = None
    scheduled_target_slot: int | None = None
    scheduled_grant_dci_slot: int | None = None
    current_scheduled_queue_entry_slot: int | None = None
    early_rescue_trigger_slot: int | None = None
    rv2_tx_slot: int | None = None
    rv2_nack_slot: int | None = None
    rv3_tx_slot: int | None = None
    rv3_nack_slot: int | None = None
    rescue_trigger_rv: int | None = None

    @property
    def latency_slots(self) -> int | None:
        """Completion latency, including zero-slot immediate completion."""
        if self.completion_slot is None:
            return None
        return self.completion_slot - self.arrival_slot

    @property
    def higher_layer_payload_id(self) -> int:
        """Identity retained across MAC/HARQ context restarts."""
        return self.packet_id

    @property
    def phy_attempt_index(self) -> int:
        """One-based PHY-attempt index within the current HARQ episode."""
        return self.harq_attempt_index

    def initialize_transmission_context(self) -> None:
        """Create initial deterministic MAC-TB and HARQ episode IDs."""
        if self.mac_tb_id is None:
            self.mac_tb_id = f"tb-{self.packet_id}-{self.transmission_context_generation}"
        if self.harq_episode_id is None:
            self.harq_episode_id = (
                f"harq-{self.packet_id}-{self.transmission_context_generation}"
            )

    def restart_transmission_context(self) -> tuple[str, str]:
        """Start a fresh MAC TB/HARQ episode for the same payload."""
        if self.mac_tb_id is None or self.harq_episode_id is None:
            raise RuntimeError("cannot restart an uninitialized transmission context")
        old = (self.mac_tb_id, self.harq_episode_id)
        self.transmission_context_generation += 1
        self.mac_tb_id = f"tb-{self.packet_id}-{self.transmission_context_generation}"
        self.harq_episode_id = (
            f"harq-{self.packet_id}-{self.transmission_context_generation}"
        )
        self.harq_attempt_index = 0
        return old


class PacketQueue:
    """FIFO queue owned by one UE."""

    def __init__(self, ue_id: int) -> None:
        self.ue_id = int(ue_id)
        self._packets: deque[Packet] = deque()

    def enqueue(self, packet: Packet) -> None:
        if packet.ue_id != self.ue_id:
            raise ValueError("packet UE does not match queue owner")
        self._packets.append(packet)

    def peek(self) -> Packet | None:
        return self._packets[0] if self._packets else None

    def pop(self) -> Packet:
        if not self._packets:
            raise IndexError("cannot pop an empty packet queue")
        return self._packets.popleft()

    def __len__(self) -> int:
        return len(self._packets)

    def snapshot(self) -> tuple[int, ...]:
        """Packet IDs in FIFO order, useful for deterministic tests/traces."""
        return tuple(packet.packet_id for packet in self._packets)

    def packets(self) -> tuple[Packet, ...]:
        """Return queued packets for conservation/cohort accounting."""
        return tuple(self._packets)
