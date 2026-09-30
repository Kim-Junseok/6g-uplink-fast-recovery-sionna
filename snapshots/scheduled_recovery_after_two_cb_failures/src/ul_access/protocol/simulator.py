"""Deterministic slot engine with explicit HARQ and post-HARQ recovery."""

from __future__ import annotations

import hashlib
import random
import statistics
from dataclasses import asdict, dataclass
from typing import Any, Protocol, Sequence

from ul_access.phy.harq_abstraction import HarqEpisodeState
from ul_access.phy.harq_mi_abstraction import HarqMiEpisodeState
from ul_access.phy.interface import HarqPhyBackend, PhyBackend, PhyOutcome
from ul_access.protocol.access import AccessDecision, AccessPolicy, Transmission
from ul_access.protocol.models import Packet, PacketQueue, PacketState
from ul_access.recovery import (
    HarqController,
    LegacyArqPolicy,
    LegacyRlcScheduledPolicy,
    RecoveryPolicy,
    ScheduledRescueConfig,
    ScheduledRescueMode,
)
from ul_access.resource import (
    ResourceAllocation,
    ResourceAssignment,
    ScheduledResourcePolicy,
    shared_uncapped_policy,
)


class TrafficSource(Protocol):
    def arrivals(self, slot: int) -> tuple[Packet, ...]: ...


@dataclass(frozen=True)
class SimulationParameters:
    duration_slots: int
    num_ues: int
    seed: int
    tx_power_dbm: tuple[float, ...]
    mcs_index: tuple[int, ...]
    max_drain_slots: int = 0
    warmup_slots: int = 0
    latency_deadlines_slots: tuple[int, ...] = ()


@dataclass(frozen=True)
class PendingHarqFeedback:
    """Feedback bound to the exact transmission context that created it."""

    packet: Packet
    mac_tb_id: str
    harq_episode_id: str
    phy_attempt_index: int
    feedback_value: int
    payload_correct: bool
    undetected_error: bool


def _percentile(samples: Sequence[int], percentile: float) -> float | None:
    if not samples:
        return None
    ordered = sorted(samples)
    position = (len(ordered) - 1) * percentile / 100.0
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + fraction * (ordered[upper] - ordered[lower])


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _series_summary(samples: Sequence[int]) -> dict[str, float | int | None]:
    return {"mean": statistics.fmean(samples) if samples else None,
            "p95": _percentile(samples, 95), "p99": _percentile(samples, 99),
            "maximum": max(samples) if samples else None}


def derive_stream_seed(
    run_seed: int, slot: int, stream_id: str, *, bits: int = 64
) -> int:
    """Hash a conceptual RNG address without additive cross-run aliases."""
    if bits not in {32, 64}:
        raise ValueError("stream seed width must be 32 or 64 bits")
    if slot < 0:
        raise ValueError("slot must be non-negative")
    payload = f"ul_access_rng_v1\0{int(run_seed)}\0{int(slot)}\0{stream_id}".encode()
    digest = hashlib.blake2s(payload, digest_size=bits // 8).digest()
    return int.from_bytes(digest, byteorder="big", signed=False)


class SlotSimulator:
    """Advance traffic, access, PHY, delayed feedback, and RLC recovery by slot."""

    def __init__(self, *, parameters: SimulationParameters, traffic: TrafficSource,
                 access: AccessPolicy, phy_backend: PhyBackend,
                 harq: HarqController | None = None,
                 recovery: RecoveryPolicy | None = None,
                 max_rlc_retransmissions: int = 0,
                 scheduled_rescue: ScheduledRescueConfig | None = None,
                 scheduled_rlc_recovery: bool = False,
                 extended_outputs: bool = False,
                 event_addressed_scheduler_ties: bool = False,
                 uniform_available_cb_selection: bool = False,
                 scheduled_rlc_uses_request_grant: bool = False,
                 direct_scheduled_k2_slots: int | None = None,
                 scheduled_resource_policy: ScheduledResourcePolicy | None = None,
                 retain_per_re_trace: bool = True,
                 enhanced_attempt_trace: bool = False) -> None:
        if parameters.duration_slots <= 0:
            raise ValueError("duration_slots must be positive")
        if parameters.max_drain_slots < 0:
            raise ValueError("max_drain_slots must be non-negative")
        if parameters.num_ues != phy_backend.num_ues:
            raise ValueError("simulation and PHY backend UE counts differ")
        if access.resource_pool_size != phy_backend.num_subcarriers:
            raise ValueError("access pool and PHY resource counts differ")
        if max_rlc_retransmissions < 0:
            raise ValueError("max_rlc_retransmissions must be non-negative")
        if any(value < 0 for value in parameters.latency_deadlines_slots):
            raise ValueError("latency deadlines must be non-negative")
        if scheduled_rescue is not None and not access.allows_resource_overlap:
            raise ValueError("V0.4a scheduled rescue requires Grant-Free base access")
        self.parameters, self.traffic, self.access = parameters, traffic, access
        self.phy_backend = phy_backend
        self.harq = harq or HarqController(max_attempts=2, feedback_delay_slots=0)
        self.recovery = recovery or LegacyArqPolicy(recovery_delay_slots=0)
        self.max_rlc_retransmissions = int(max_rlc_retransmissions)
        self.scheduled_rescue = scheduled_rescue
        self.scheduled_rlc_recovery = bool(scheduled_rlc_recovery)
        self.extended_outputs = bool(extended_outputs)
        self.event_addressed_scheduler_ties = bool(event_addressed_scheduler_ties)
        self.uniform_available_cb_selection = bool(uniform_available_cb_selection)
        self.scheduled_rlc_uses_request_grant = bool(
            scheduled_rlc_uses_request_grant)
        if direct_scheduled_k2_slots is not None and direct_scheduled_k2_slots < 0:
            raise ValueError("direct scheduled K2 must be non-negative")
        self.direct_scheduled_k2_slots = (
            int(direct_scheduled_k2_slots)
            if direct_scheduled_k2_slots is not None else None)
        self.scheduled_resource_policy = (
            scheduled_resource_policy or
            shared_uncapped_policy(self.access.resource_pool_size)
        )
        self.retain_per_re_trace = bool(retain_per_re_trace)
        self.enhanced_attempt_trace = bool(enhanced_attempt_trace)
        if self.scheduled_rlc_recovery and self.scheduled_rescue is None:
            raise ValueError("scheduled RLC recovery requires scheduled resource config")
        if self.scheduled_resource_policy.base_cb_prbs != self.access.resource_pool_size:
            raise ValueError("resource policy base pool differs from access pool")
        if self.scheduled_resource_policy.dedicated and self.scheduled_rescue is None:
            raise ValueError("dedicated scheduled resources require scheduled recovery")

    def run(self) -> dict[str, Any]:
        queues = [PacketQueue(i) for i in range(self.parameters.num_ues)]
        completed: list[Packet] = []
        dropped: list[Packet] = []
        pending_feedback: dict[int, list[PendingHarqFeedback]] = {}
        harq_combining_state: dict[
            str, HarqEpisodeState | HarqMiEpisodeState] = {}
        legacy_timing_records: dict[tuple[int, int], dict[str, Any]] = {}
        trace: list[dict[str, Any]] = []
        counts = {k: 0 for k in (
            "arrived", "transmissions", "nacks", "acks", "harq_retx",
            "final_failures", "rlc_retx", "legacy_events", "fast_events",
            "feedback_events", "rlc_limit_drops", "requests", "grants",
            "occupied", "overlap_events", "overlapped_transmissions", "bits",
            "gf_transmissions",
            "rescue_requests", "rescue_grants", "rescue_transmissions",
            "rescue_successes", "abandoned_harq_episodes",
            "early_payload_requeues", "stale_feedback_discarded",
            "receiver_acceptances", "correct_deliveries",
            "undetected_errors")}
        measurement_counts = {k: 0 for k in (
            "arrived", "initial_tx", "harq_retx", "rlc_retx_tx", "transmissions",
            "nacks", "acks", "final_failures", "rlc_eligible", "bits",
            "gf_transmissions", "rescue_requests", "rescue_grants",
            "rescue_transmissions", "rescue_successes",
            "abandoned_harq_episodes", "early_payload_requeues",
            "receiver_acceptances", "correct_deliveries",
            "undetected_errors")}
        occupancy_samples: list[int] = []
        slot_resource_rows: list[dict[str, int | bool]] = []
        recovery_tx_per_slot: list[int] = []
        rlc_eligible_per_slot: list[int] = []
        overlap = {"occupied": 0, "events": 0, "recovery_events": 0,
                   "overlapping_resources": 0, "ues_involved": 0}
        diagnostic_names = ("generated_arrivals", "queued_packets", "phy_attempts",
            "grant_free_attempts", "overlapping_ues", "nacks", "fast_arq_triggers",
            "rlc_retransmission_entries", "scheduled_rescue_requests",
            "scheduled_rescue_grants", "scheduled_rescue_attempts",
            "successful_completions")
        diagnostic_series = {name: [] for name in diagnostic_names}
        slot_diagnostics = {name: 0 for name in diagnostic_names}

        def identity(packet: Packet) -> dict[str, Any]:
            return {
                "payload_id": packet.higher_layer_payload_id,
                "tb_id": packet.mac_tb_id,
                "harq_episode_id": packet.harq_episode_id,
                "attempt_index": packet.phy_attempt_index,
            }

        def record(packet: Packet, slot: int, event: str, **values: Any) -> None:
            trace.append({"slot": slot, "packet_id": packet.packet_id,
                          "event": event, **identity(packet), **values})

        def clear_combining_state(packet: Packet, slot: int, reason: str,
                                  episode_id: str | None = None) -> None:
            key = episode_id if episode_id is not None else packet.harq_episode_id
            if key is not None and harq_combining_state.pop(key, None) is not None:
                record(packet, slot, "harq_combining_state_cleared",
                       cleared_harq_episode_id=key, reason=reason)

        def schedule_rescue(packet: Packet, slot: int, *, initial: bool) -> None:
            if self.scheduled_rescue is None:
                raise RuntimeError("scheduled rescue is not configured")
            packet.rescue_triggered = True
            packet.rescue_active = True
            if initial:
                record(packet, slot, "scheduled_rescue_triggered",
                       rescue_mode=self.scheduled_rescue.name,
                       ndi_semantics=("logically_unchanged"
                           if self.scheduled_rescue.mode == ScheduledRescueMode.HARQ_PRESERVING
                           else "logically_toggled"))
            if self.direct_scheduled_k2_slots is not None:
                target = max(
                    slot + self.direct_scheduled_k2_slots,
                    self.harq.next_pusch_slot(packet.last_tx_slot)
                    if packet.last_tx_slot is not None else slot)
                packet.state = PacketState.RESCUE_GRANT_WAIT
                packet.rescue_grant_ready_slot = target
                packet.current_scheduled_queue_entry_slot = slot
                if packet.scheduled_queue_entry_slot is None:
                    packet.scheduled_queue_entry_slot = slot
                packet.last_state_slot = slot
                record(packet, slot, "scheduled_recovery_scheduler_ready",
                       earliest_pusch_slot=target)
                return
            packet.rescue_request_due_slot = (
                slot + self.scheduled_rescue.request_delay_slots
            )
            packet.rescue_grant_ready_slot = None
            packet.state = PacketState.RESCUE_REQUEST_WAIT
            packet.last_state_slot = slot

        def restart_rlc_episode(packet: Packet, slot: int) -> None:
            """Start the next RLC retransmission episode exactly once."""
            packet.rlc_recovery_wait_slots += slot - packet.last_state_slot
            packet.rlc_retransmission_count += 1
            old_episode = packet.harq_episode_id
            clear_combining_state(packet, slot, "rlc_restart", old_episode)
            old_tb_id, old_harq_id = packet.restart_transmission_context()
            packet.physical_attempts_in_recovery_episode = 0
            packet.rlc_timer_start_slot = None
            packet.rlc_timer_expiry_slot = None
            if (self.scheduled_rescue is not None and
                    self.scheduled_rescue.reset_rescue_on_rlc_restart):
                packet.rescue_triggered = False
                packet.rescue_active = False
            packet.state = PacketState.RLC_RETX_ACCESS_WAIT
            packet.last_state_slot = slot
            counts["rlc_retx"] += 1
            if packet.measurement_cohort:
                measurement_counts["rlc_eligible"] += 1
            if self.parameters.warmup_slots <= slot < self.parameters.duration_slots:
                slot_diagnostics["rlc_retransmission_entries"] += 1
            self.access.reset_packet_access(packet, slot)
            record(packet, slot, "rlc_retransmission_eligible",
                   rlc_retx_index=packet.rlc_retransmission_count,
                   old_tb_id=old_tb_id, old_harq_episode_id=old_harq_id)

        def remove(packet: Packet, state: PacketState, slot: int) -> None:
            popped = queues[packet.ue_id].pop()
            if popped is not packet:
                raise RuntimeError("recovery attempted a non-head packet")
            packet.state, packet.completion_slot = state, slot
            if self.extended_outputs:
                record(packet, slot, "packet_delivered" if state == PacketState.DELIVERED
                       else "packet_dropped", drop_reason=packet.drop_reason)
            self.access.packet_removed(packet.ue_id, queue_empty=not len(queues[packet.ue_id]))
            (completed if state == PacketState.DELIVERED else dropped).append(packet)

        def feedback(pending: PendingHarqFeedback, slot: int) -> None:
            packet = pending.packet
            if (packet.mac_tb_id != pending.mac_tb_id or
                    packet.harq_episode_id != pending.harq_episode_id or
                    packet.phy_attempt_index != pending.phy_attempt_index):
                counts["stale_feedback_discarded"] += 1
                return
            counts["feedback_events"] += 1
            packet.harq_feedback_wait_slots += slot - packet.last_state_slot
            ack = pending.feedback_value == 1
            record(packet, slot, "harq_ack" if ack else "harq_nack",
                   harq_attempt=packet.harq_attempt_index,
                   rlc_retransmissions=packet.rlc_retransmission_count,
                   transmission_access=packet.last_transmission_access,
                   payload_correct=pending.payload_correct,
                   undetected_error=pending.undetected_error)
            if ack:
                if (packet.rlc_retransmission_count > 0 and
                        packet.first_recovery_harq_termination_slot is None):
                    packet.first_recovery_harq_termination_slot = slot
                counts["acks"] += 1
                counts["bits"] += packet.size_bits
                counts["receiver_acceptances"] += 1
                counts["correct_deliveries"] += int(pending.payload_correct)
                counts["undetected_errors"] += int(pending.undetected_error)
                packet.receiver_accepted = True
                packet.payload_correct = pending.payload_correct
                packet.undetected_error = pending.undetected_error
                if packet.last_transmission_was_rescue:
                    packet.scheduled_completion_slot = slot
                if packet.last_transmission_was_rescue:
                    counts["rescue_successes"] += 1
                if packet.measurement_cohort:
                    measurement_counts["acks"] += 1
                    measurement_counts["bits"] += packet.size_bits
                    measurement_counts["receiver_acceptances"] += 1
                    measurement_counts["correct_deliveries"] += int(
                        pending.payload_correct)
                    measurement_counts["undetected_errors"] += int(
                        pending.undetected_error)
                    if packet.last_transmission_was_rescue:
                        measurement_counts["rescue_successes"] += 1
                clear_combining_state(packet, slot, "ack")
                remove(packet, PacketState.DELIVERED, slot)
                if self.parameters.warmup_slots <= slot < self.parameters.duration_slots:
                    slot_diagnostics["successful_completions"] += 1
            else:
                counts["nacks"] += 1
                if self.parameters.warmup_slots <= slot < self.parameters.duration_slots:
                    slot_diagnostics["nacks"] += 1
                if packet.measurement_cohort: measurement_counts["nacks"] += 1

                physical_limit = (
                    self.scheduled_rescue.maximum_physical_attempts_per_recovery_episode
                    if self.scheduled_rescue is not None and
                    self.scheduled_rescue.maximum_physical_attempts_per_recovery_episode is not None
                    else self.harq.max_attempts)
                physical_budget_remains = (
                    packet.physical_attempts_in_recovery_episode < physical_limit)

                first_gf_rescue = (
                    self.scheduled_rescue is not None
                    and packet.last_transmission_access == "grant_free"
                    and not packet.rescue_triggered
                    and packet.harq_attempt_index >= self.scheduled_rescue.trigger_after_attempts
                    and physical_budget_remains
                    and (self.scheduled_rescue.mode == ScheduledRescueMode.FRESH_TB
                         or packet.harq_attempt_index < self.harq.max_attempts)
                )
                if first_gf_rescue:
                    packet.early_rescue_trigger_slot = slot
                    packet.rv2_nack_slot = slot
                    if self.scheduled_rescue.mode == ScheduledRescueMode.FRESH_TB:
                        old_episode = packet.harq_episode_id
                        clear_combining_state(packet, slot, "episode_abandonment",
                                              old_episode)
                        old_tb_id, old_harq_id = packet.restart_transmission_context()
                        counts["abandoned_harq_episodes"] += 1
                        counts["early_payload_requeues"] += 1
                        if packet.measurement_cohort:
                            measurement_counts["abandoned_harq_episodes"] += 1
                            measurement_counts["early_payload_requeues"] += 1
                        record(packet, slot, "harq_episode_abandoned_for_rescue",
                               old_tb_id=old_tb_id,
                               old_harq_episode_id=old_harq_id,
                               ndi_semantics="logically_toggled")
                        record(packet, slot, "early_payload_requeue",
                               retained_payload_id=packet.higher_layer_payload_id)
                    schedule_rescue(packet, slot, initial=True)
                elif (packet.rescue_active and physical_budget_remains and
                      (self.scheduled_rescue.mode == ScheduledRescueMode.FRESH_TB
                       or packet.harq_attempt_index < self.harq.max_attempts)):
                    schedule_rescue(packet, slot, initial=False)
                elif (physical_budget_remains and
                      packet.harq_attempt_index < self.harq.max_attempts):
                    packet.state = PacketState.HARQ_RETX_ACCESS_WAIT
                    packet.last_state_slot = slot
                    earliest = self.harq.next_pusch_slot(
                        packet.last_tx_slot if packet.last_tx_slot is not None
                        else slot)
                    self.access.reset_packet_access(packet, max(slot, earliest))
                else:
                    packet.rescue_active = False
                    if (packet.rlc_retransmission_count > 0 and
                            packet.first_recovery_harq_termination_slot is None):
                        packet.first_recovery_harq_termination_slot = slot
                    clear_combining_state(packet, slot, "final_failure")
                    counts["final_failures"] += 1
                    if packet.measurement_cohort:
                        measurement_counts["final_failures"] += 1
                    record(packet, slot, "final_harq_failure")
                    packet.state = PacketState.FINAL_HARQ_FAILURE
                    packet.harq_termination_slot = slot
                    if packet.initial_harq_final_failure_slot is None:
                        packet.initial_harq_final_failure_slot = slot
                    timer_driven_legacy = isinstance(
                        self.recovery, LegacyRlcScheduledPolicy)
                    if (not timer_driven_legacy and
                            packet.rlc_retransmission_count >=
                            self.max_rlc_retransmissions):
                        counts["rlc_limit_drops"] += 1
                        packet.drop_reason = "rlc_retransmission_limit"
                        remove(packet, PacketState.DROPPED, slot)
                    elif timer_driven_legacy:
                        record(packet, slot, "legacy_harq_terminated_timer_pending",
                               timer_expiry_slot=packet.rlc_timer_expiry_slot)
                    else:
                        decision = self.recovery.on_final_harq_failure(packet, slot)
                        counts["legacy_events" if self.recovery.name == "legacy_arq" else "fast_events"] += 1
                        packet.fast_arq_triggered = self.recovery.name == "fast_arq"
                        if (packet.fast_arq_triggered and
                                packet.fast_arq_trigger_slot is None):
                            packet.fast_arq_trigger_slot = slot
                        if (self.recovery.name == "fast_arq" and
                                self.parameters.warmup_slots <= slot < self.parameters.duration_slots):
                            slot_diagnostics["fast_arq_triggers"] += 1
                        packet.state = PacketState.RLC_RECOVERY_WAIT
                        packet.next_eligible_slot = decision.eligible_slot
                        if (self.recovery.name == "legacy_arq" and
                                packet.legacy_recovery_eligible_slot is None):
                            packet.legacy_timer_expiry_slot = (
                                decision.timer_expiry_slot)
                            packet.legacy_recovery_eligible_slot = (
                                decision.eligible_slot)
                        packet.last_state_slot = slot
                        record(packet, slot, decision.event_name,
                               eligible_slot=decision.eligible_slot)
                        if (self.scheduled_rlc_recovery and
                                self.direct_scheduled_k2_slots is not None):
                            restart_rlc_episode(packet, slot)
                            schedule_rescue(packet, slot, initial=True)

        drain_slots_used = 0
        for slot in range(
            self.parameters.duration_slots + self.parameters.max_drain_slots
        ):
            slot_diagnostics = {name: 0 for name in diagnostic_names}
            scheduled_requests_in_slot = 0
            if slot >= self.parameters.duration_slots:
                if not any(len(queue) for queue in queues):
                    break
                drain_slots_used += 1
            for packet in pending_feedback.pop(slot, []):
                feedback(packet, slot)
            eligible_this_slot = 0
            if isinstance(self.recovery, LegacyRlcScheduledPolicy):
                for queue in queues:
                    packet = queue.peek()
                    if (packet is None or packet.rlc_timer_expiry_slot is None or
                            slot < packet.rlc_timer_expiry_slot):
                        continue
                    timer_start = packet.rlc_timer_start_slot
                    timer_expiry = packet.rlc_timer_expiry_slot
                    record(packet, slot, "legacy_rlc_timer_expired",
                           rlc_episode_index=packet.rlc_retransmission_count,
                           timer_start_slot=timer_start,
                           timer_expiry_slot=timer_expiry)
                    counts["legacy_events"] += 1
                    if (packet.rlc_retransmission_count >=
                            self.max_rlc_retransmissions):
                        clear_combining_state(packet, slot, "rlc_limit")
                        counts["rlc_limit_drops"] += 1
                        packet.drop_reason = "rlc_retransmission_limit"
                        remove(packet, PacketState.DROPPED, slot)
                        continue
                    restart_rlc_episode(packet, slot)
                    eligible_this_slot += int(packet.measurement_cohort)
                    packet.sr_eligible_slot = slot
                    packet.sr_phase = self.recovery.sr_phase(packet.ue_id)
                    packet.actual_sr_slot = self.recovery.next_sr_slot(
                        slot, packet.ue_id)
                    packet.state = PacketState.RESCUE_REQUEST_WAIT
                    packet.rescue_active = True
                    packet.rescue_request_due_slot = packet.actual_sr_slot
                    packet.current_scheduled_queue_entry_slot = None
                    legacy_timing_records[(
                        packet.packet_id, packet.rlc_retransmission_count)] = {
                            "payload_id": packet.packet_id,
                            "ue_id": packet.ue_id,
                            "rlc_retx_index": packet.rlc_retransmission_count,
                            "rlc_timer_start_slot": timer_start,
                            "rlc_timer_expiry_slot": timer_expiry,
                            "sr_eligible_slot": slot,
                            "sr_phase": packet.sr_phase,
                            "actual_sr_slot": packet.actual_sr_slot,
                            "sr_wait_slots": packet.actual_sr_slot - slot,
                            "grant_ready_slot": None,
                            "scheduled_request_queue_entry": None,
                            "scheduled_grant_dci_slot": None,
                            "scheduled_first_tx_slot": None,
                            "sr_to_scheduled_tx_slots": None,
                            "scheduled_queue_wait_slots": None,
                        }
                    record(packet, slot, "legacy_sr_pending",
                           sr_phase=packet.sr_phase,
                           actual_sr_slot=packet.actual_sr_slot)
            for queue in queues:
                packet = queue.peek()
                if packet and packet.state == PacketState.RLC_RECOVERY_WAIT and slot >= packet.next_eligible_slot:
                    restart_rlc_episode(packet, slot)
                    eligible_this_slot += int(packet.measurement_cohort)
                    if self.scheduled_rlc_recovery and self.scheduled_rlc_uses_request_grant:
                        if self.scheduled_rescue is None:
                            raise RuntimeError("scheduled recovery configuration is missing")
                        packet.rescue_active = True
                        packet.state = PacketState.RESCUE_REQUEST_WAIT
                        packet.rescue_request_due_slot = (
                            slot + self.scheduled_rescue.request_delay_slots)
                        record(packet, slot, "scheduled_rlc_recovery_ready",
                               request_due_slot=packet.rescue_request_due_slot)
                    elif self.scheduled_rlc_recovery:
                        packet.rescue_active = True
                        packet.state = PacketState.RESCUE_GRANT_WAIT
                        packet.rescue_grant_ready_slot = slot
                        record(packet, slot, "scheduled_rlc_recovery_ready",
                               grant_ready_slot=slot)
            if slot < self.parameters.duration_slots:
                arrivals = self.traffic.arrivals(slot)
                for packet in arrivals:
                    packet.initialize_transmission_context()
                    if not packet.measurement_cohort_locked:
                        packet.measurement_cohort = slot >= self.parameters.warmup_slots
                    packet.last_state_slot = slot
                    queues[packet.ue_id].enqueue(packet); counts["arrived"] += 1
                    if packet.measurement_cohort: measurement_counts["arrived"] += 1
                    record(packet, slot, "arrival", ue_id=packet.ue_id,
                           continuous_time_slots=packet.continuous_arrival_time_slots)
                if slot >= self.parameters.warmup_slots:
                    slot_diagnostics["generated_arrivals"] = len(arrivals)
            if self.parameters.warmup_slots <= slot < self.parameters.duration_slots:
                slot_diagnostics["queued_packets"] = sum(map(len, queues))

            # Advance the abstract SR-like control path. Request and grant
            # events are distinct, and a grant is counted only when one of the
            # shared physical resources is actually assigned.
            if self.scheduled_rescue is not None:
                for queue in queues:
                    packet = queue.peek()
                    if (packet is not None and
                            packet.state == PacketState.RESCUE_REQUEST_WAIT and
                            packet.rescue_request_due_slot is not None and
                            slot >= packet.rescue_request_due_slot):
                        packet.state = PacketState.RESCUE_GRANT_WAIT
                        if isinstance(self.recovery, LegacyRlcScheduledPolicy):
                            packet.actual_sr_slot = slot
                            packet.grant_processing_complete_slot = (
                                slot + self.recovery.sr_control_processing_slots)
                            packet.scheduled_target_slot = (
                                self.recovery.earliest_scheduled_pusch_slot(slot))
                            packet.rescue_grant_ready_slot = (
                                packet.scheduled_target_slot)
                            record(packet, slot, "legacy_sr_transmitted",
                                   sr_phase=packet.sr_phase,
                                   grant_processing_complete_slot=(
                                       packet.grant_processing_complete_slot),
                                   earliest_scheduled_pusch_slot=(
                                       packet.scheduled_target_slot))
                        else:
                            packet.rescue_grant_ready_slot = (
                                slot + self.scheduled_rescue.scheduled_grant_delay_slots
                            )
                        if packet.scheduled_request_slot is None:
                            packet.scheduled_request_slot = slot
                        if packet.scheduled_grant_ready_slot is None:
                            packet.scheduled_grant_ready_slot = (
                                packet.rescue_grant_ready_slot)
                        if packet.scheduled_queue_entry_slot is None:
                            packet.scheduled_queue_entry_slot = (
                                packet.rescue_grant_ready_slot)
                        packet.current_scheduled_queue_entry_slot = (
                            packet.rescue_grant_ready_slot)
                        if isinstance(self.recovery, LegacyRlcScheduledPolicy):
                            timing = legacy_timing_records[(
                                packet.packet_id,
                                packet.rlc_retransmission_count)]
                            timing["actual_sr_slot"] = slot
                            timing["sr_wait_slots"] = (
                                slot - timing["sr_eligible_slot"])
                            timing["grant_ready_slot"] = (
                                packet.grant_processing_complete_slot)
                            timing["scheduled_request_queue_entry"] = (
                                packet.rescue_grant_ready_slot)
                        counts["rescue_requests"] += 1
                        counts["requests"] += 1
                        scheduled_requests_in_slot += 1
                        if packet.measurement_cohort:
                            measurement_counts["rescue_requests"] += 1
                        if self.parameters.warmup_slots <= slot < self.parameters.duration_slots:
                            slot_diagnostics["scheduled_rescue_requests"] += 1
                        record(packet, slot, "scheduled_rescue_request",
                               grant_ready_slot=packet.rescue_grant_ready_slot,
                               signaling=("collision_free_periodic_sr"
                                   if isinstance(
                                       self.recovery,
                                       LegacyRlcScheduledPolicy)
                                   else "gnb_fast_recovery"))

            rescue_ready_all = sorted(
                (queue.peek() for queue in queues),
                key=lambda packet: (
                    packet.rescue_grant_ready_slot
                    if packet is not None and packet.rescue_grant_ready_slot is not None
                    else 2**63,
                    packet.arrival_slot if packet is not None else 2**63,
                    derive_stream_seed(
                        self.parameters.seed, slot,
                        f"scheduler_tie:{packet.ue_id}", bits=64)
                    if packet is not None and self.event_addressed_scheduler_ties
                    else packet.ue_id if packet is not None else 2**64,
                ),
            )
            rescue_ready_all = [
                packet for packet in rescue_ready_all
                if packet is not None
                and packet.state == PacketState.RESCUE_GRANT_WAIT
                and packet.rescue_grant_ready_slot is not None
                and slot >= packet.rescue_grant_ready_slot
            ]
            scheduled_ready_demand = len(rescue_ready_all)
            rescue_ready = rescue_ready_all[
                :self.scheduled_resource_policy.scheduled_capacity]

            # A slot-addressed stream prevents treatment-dependent control flow in
            # one slot from shifting Grant-Free choices in later slots.
            access_rng = random.Random(
                derive_stream_seed(self.parameters.seed, slot, "access", bits=64)
            )
            base_decision = self.access.decide(slot, queues, access_rng)
            if rescue_ready:
                free_resources = self.scheduled_resource_policy.cb_resources(
                    len(rescue_ready))
                if self.scheduled_resource_policy.dedicated:
                    normal = base_decision.transmissions
                elif (self.uniform_available_cb_selection and
                        hasattr(self.access, "assign_available_resources")):
                    normal = self.access.assign_available_resources(
                        slot, base_decision.transmissions, free_resources)
                else:
                    normal = tuple(
                        Transmission(tx.ue_id,
                                     free_resources[tx.resource_id % len(free_resources)],
                                     tx.packet)
                        for tx in base_decision.transmissions
                    ) if free_resources else ()
                rescue_tx = []
                for service_index, packet in enumerate(rescue_ready):
                    domain, resource_id, phy_resource_id = (
                        self.scheduled_resource_policy.scheduled_resource(
                            service_index))
                    rescue_tx.append(Transmission(
                        packet.ue_id, resource_id, packet, True,
                        resource_domain=domain,
                        phy_resource_id=phy_resource_id))
                decision = AccessDecision(
                    tuple(rescue_tx) + normal,
                    request_count=base_decision.request_count,
                    grant_count=base_decision.grant_count,
                )
                for packet in rescue_ready:
                    k2 = (self.recovery.scheduled_pusch_k2_slots
                          if isinstance(self.recovery,
                                        LegacyRlcScheduledPolicy)
                          else self.direct_scheduled_k2_slots)
                    if k2 is not None:
                        packet.scheduled_grant_dci_slot = slot - k2
                    counts["rescue_grants"] += 1
                    counts["grants"] += 1
                    if packet.measurement_cohort:
                        measurement_counts["rescue_grants"] += 1
                    if self.parameters.warmup_slots <= slot < self.parameters.duration_slots:
                        slot_diagnostics["scheduled_rescue_grants"] += 1
                    record(packet, slot, "scheduled_rescue_grant",
                           resource_semantics="non_contention_based",
                           resource_policy=self.scheduled_resource_policy.identifier,
                           scheduled_grant_dci_slot=(
                               packet.scheduled_grant_dci_slot),
                           target_pusch_slot=slot)
            else:
                decision = base_decision
            self._validate_decision(decision.transmissions)
            counts["requests"] += decision.request_count; counts["grants"] += decision.grant_count
            base_occupancy = [0] * self.access.resource_pool_size
            dedicated_occupancy = [0] * (
                self.scheduled_resource_policy.scheduled_capacity
                if self.scheduled_resource_policy.dedicated else 0)
            attempt_occupancy = {}
            for tx in decision.transmissions:
                if tx.resource_domain == "base":
                    base_occupancy[tx.resource_id] += 1
                else:
                    dedicated_occupancy[tx.phy_resource_id] += 1
            for tx in decision.transmissions:
                local = (base_occupancy[tx.resource_id]
                         if tx.resource_domain == "base" else
                         dedicated_occupancy[tx.phy_resource_id])
                attempt_occupancy[(tx.resource_domain, tx.resource_id)] = local
            occupancy_samples.extend(base_occupancy + dedicated_occupancy)
            counts["occupied"] += sum(x > 0 for x in base_occupancy + dedicated_occupancy)
            counts["overlap_events"] += sum(x > 1 for x in base_occupancy + dedicated_occupancy)
            counts["overlapped_transmissions"] += sum(
                attempt_occupancy[(x.resource_domain, x.resource_id)] > 1
                for x in decision.transmissions)
            scheduled_in_slot = sum(tx.is_scheduled_rescue
                                    for tx in decision.transmissions)
            cb_attempts_in_slot = sum(not tx.is_scheduled_rescue
                                      for tx in decision.transmissions)
            shared_scheduled_in_slot = sum(
                tx.is_scheduled_rescue and tx.resource_domain == "base"
                for tx in decision.transmissions)
            dedicated_scheduled_in_slot = sum(
                tx.is_scheduled_rescue and tx.resource_domain == "dedicated_sb"
                for tx in decision.transmissions)
            cb_resources = {
                tx.resource_id for tx in decision.transmissions
                if not tx.is_scheduled_rescue}
            cb_available_resources = self.scheduled_resource_policy.cb_resources(
                scheduled_in_slot)
            cb_available = len(cb_available_resources)
            pool_invariant = (
                shared_scheduled_in_slot + len(cb_resources) <=
                self.access.resource_pool_size and
                dedicated_scheduled_in_slot <=
                (self.scheduled_resource_policy.scheduled_capacity
                 if self.scheduled_resource_policy.dedicated else 0))
            if not pool_invariant:
                raise RuntimeError("resource-policy accounting invariant failed")
            cb_occupancies = [
                sum(not tx.is_scheduled_rescue and tx.resource_id == resource
                    for tx in decision.transmissions)
                for resource in cb_available_resources]
            if self.extended_outputs:
                slot_resource_rows.append({
                    "slot": slot,
                    "resource_policy": self.scheduled_resource_policy.identifier,
                    "scheduled_ready_demand": scheduled_ready_demand,
                    "scheduled_requests_in_slot": scheduled_requests_in_slot,
                    "scheduled_queue_length_before_service": scheduled_ready_demand,
                    "scheduled_service_capacity": self.scheduled_resource_policy.scheduled_capacity,
                    "scheduled_service_count": scheduled_in_slot,
                    "scheduled_attempts_admitted": scheduled_in_slot,
                    "scheduled_queue_length": scheduled_ready_demand,
                    "scheduled_prbs_used": shared_scheduled_in_slot,
                    "dedicated_sb_resources_used": dedicated_scheduled_in_slot,
                    "cb_prbs_available": cb_available,
                    "cb_prbs_occupied": len(cb_resources),
                    "cb_attempt_count": cb_attempts_in_slot,
                    "cb_occupancy_0": sum(value == 0 for value in cb_occupancies),
                    "cb_occupancy_1": sum(value == 1 for value in cb_occupancies),
                    "cb_occupancy_2": sum(value == 2 for value in cb_occupancies),
                    "cb_occupancy_3plus": sum(value >= 3 for value in cb_occupancies),
                    "zero_cb_prbs": cb_available == 0,
                    "total_base_prbs_occupied": shared_scheduled_in_slot + len(cb_resources),
                    "total_physical_resources_occupied": (
                        shared_scheduled_in_slot + dedicated_scheduled_in_slot +
                        len(cb_resources)),
                    "total_prbs_occupied": (
                        shared_scheduled_in_slot + dedicated_scheduled_in_slot +
                        len(cb_resources)),
                    "pool_invariant": pool_invariant,
                })
            if slot >= self.parameters.warmup_slots:
                all_occupancy = base_occupancy + dedicated_occupancy
                overlap["occupied"] += sum(x > 0 for x in all_occupancy)
                overlap["events"] += sum(x > 1 for x in all_occupancy)
                overlap["overlapping_resources"] += sum(x > 1 for x in all_occupancy)
                overlap["ues_involved"] += sum(x for x in all_occupancy if x > 1)
                by_resource = {}
                for tx in decision.transmissions:
                    by_resource.setdefault(
                        (tx.resource_domain, tx.resource_id), []).append(tx.packet)
                overlap["recovery_events"] += sum(
                    len(packets) > 1 and any(p.harq_attempt_index > 0 or p.rlc_retransmission_count > 0 for p in packets)
                    for packets in by_resource.values())
                if slot < self.parameters.duration_slots:
                    slot_diagnostics["phy_attempts"] = len(decision.transmissions)
                    slot_diagnostics["grant_free_attempts"] = sum(
                        not tx.is_scheduled_rescue for tx in decision.transmissions
                    ) if self.access.allows_resource_overlap else 0
                    slot_diagnostics["scheduled_rescue_attempts"] = sum(
                        tx.is_scheduled_rescue for tx in decision.transmissions
                    )
                    slot_diagnostics["overlapping_ues"] = sum(
                        x for x in all_occupancy if x > 1)
            recovery_this_slot = 0
            if not decision.transmissions:
                if slot >= self.parameters.warmup_slots:
                    recovery_tx_per_slot.append(0)
                    rlc_eligible_per_slot.append(eligible_this_slot)
                    if slot < self.parameters.duration_slots:
                        for name in diagnostic_names:
                            diagnostic_series[name].append(slot_diagnostics[name])
                continue
            slot_mcs = list(self.parameters.mcs_index)
            if self.scheduled_rescue is not None:
                for tx in decision.transmissions:
                    if (tx.is_scheduled_rescue and
                            self.scheduled_rescue.rescue_mcs_index is not None):
                        slot_mcs[tx.ue_id] = self.scheduled_rescue.rescue_mcs_index
            combining_backend = (
                self.phy_backend if isinstance(self.phy_backend, HarqPhyBackend)
                else None)
            h_states: list[HarqEpisodeState | HarqMiEpisodeState | None] = [
                None] * self.parameters.num_ues
            rvs: list[int | None] = [None] * self.parameters.num_ues
            history_aware = bool(combining_backend is not None and
                                 getattr(combining_backend, "history_aware", False))
            if history_aware:
                for tx in decision.transmissions:
                    packet = tx.packet
                    if packet.mac_tb_id is None or packet.harq_episode_id is None:
                        raise RuntimeError("transmission context is not initialized")
                    state = harq_combining_state.get(packet.harq_episode_id)
                    if state is None:
                        state = combining_backend.new_episode_state(
                            mac_tb_id=packet.mac_tb_id,
                            harq_episode_id=packet.harq_episode_id,
                            mcs_index=slot_mcs[tx.ue_id])
                    h_states[tx.ue_id] = state
                    rvs[tx.ue_id] = combining_backend.rv_for_attempt(
                        len(state.rv_history))
            elif (combining_backend is not None and self.scheduled_rescue is not None
                    and self.scheduled_rescue.mode == ScheduledRescueMode.HARQ_PRESERVING):
                for tx in decision.transmissions:
                    if tx.is_scheduled_rescue:
                        episode = tx.packet.harq_episode_id
                        if episode is None or episode not in harq_combining_state:
                            raise RuntimeError("H rescue has no retained combining state")
                        h_states[tx.ue_id] = harq_combining_state[episode]
            def evaluate_group(group: Sequence[Any], domain: str) -> PhyOutcome:
                allocation = ResourceAllocation.from_assignments(
                    (ResourceAssignment(
                        ue_id=x.ue_id,
                        subcarrier=(x.phy_resource_id
                                    if x.phy_resource_id is not None
                                    else x.resource_id))
                     for x in group),
                    num_ofdm_symbols=1,
                    num_subcarriers=self.access.resource_pool_size,
                    num_ues=self.parameters.num_ues)
                phy_seed = derive_stream_seed(
                    self.parameters.seed, slot,
                    "phy" if domain == "base" else "phy:dedicated_sb",
                    bits=32)
                active_ids = {tx.ue_id for tx in group}
                group_states = [state if ue in active_ids else None
                                for ue, state in enumerate(h_states)]
                group_rvs = [rv if ue in active_ids else None
                             for ue, rv in enumerate(rvs)]
                if history_aware:
                    return combining_backend.evaluate_harq_history(
                        allocation, tx_power_dbm=self.parameters.tx_power_dbm,
                        mcs_index=slot_mcs, realization_seed=phy_seed,
                        states=group_states, rvs=group_rvs)
                if (combining_backend is not None and
                        any(state is not None for state in group_states)):
                    return combining_backend.evaluate_harq(
                        allocation, tx_power_dbm=self.parameters.tx_power_dbm,
                        mcs_index=slot_mcs, realization_seed=phy_seed,
                        states=group_states, rescue_rv=combining_backend.rescue_rv)
                return self.phy_backend.evaluate(
                    allocation, tx_power_dbm=self.parameters.tx_power_dbm,
                    mcs_index=slot_mcs, realization_seed=phy_seed)

            grouped = {
                domain: tuple(tx for tx in decision.transmissions
                              if tx.resource_domain == domain)
                for domain in {tx.resource_domain for tx in decision.transmissions}
            }
            outcomes = {domain: evaluate_group(group, domain)
                        for domain, group in sorted(grouped.items())}
            outcome_fields = {
                "effective_sinr_db": [None] * self.parameters.num_ues,
                "tbler": [None] * self.parameters.num_ues,
                "feedback": [-1] * self.parameters.num_ues,
                "decoded_bits": [0] * self.parameters.num_ues,
                "post_equalization_sinr_active_db": [()] * self.parameters.num_ues,
                "tx_power_per_active_re_w": [None] * self.parameters.num_ues,
                "bler": [None] * self.parameters.num_ues,
                "crc_pass": [None] * self.parameters.num_ues,
                "payload_correct": [None] * self.parameters.num_ues,
                "undetected_error": [None] * self.parameters.num_ues,
                "payload_hamming_distance": [None] * self.parameters.num_ues,
            }
            for domain, group in grouped.items():
                group_outcome = outcomes[domain]
                for tx in group:
                    for field, values in outcome_fields.items():
                        source = getattr(group_outcome, field)
                        if source:
                            values[tx.ue_id] = source[tx.ue_id]
            outcome = PhyOutcome(**{
                field: tuple(values) for field, values in outcome_fields.items()
            })
            immediate: list[PendingHarqFeedback] = []
            for tx in decision.transmissions:
                packet = tx.packet
                is_harq_retx = packet.harq_attempt_index > 0
                if is_harq_retx:
                    counts["harq_retx"] += 1
                    packet.harq_retransmission_wait_slots += slot - packet.last_state_slot
                else:
                    packet.access_wait_slots += max(0, slot - packet.last_state_slot)
                if (isinstance(self.recovery, LegacyRlcScheduledPolicy) and
                        packet.harq_attempt_index == 0 and
                        packet.rlc_timer_start_slot is None):
                    packet.rlc_timer_start_slot = slot
                    packet.rlc_timer_expiry_slot = (
                        self.recovery.timer_expiry_slot(slot))
                    record(packet, slot, "legacy_rlc_timer_started",
                           rlc_episode_index=packet.rlc_retransmission_count,
                           timer_start_slot=slot,
                           timer_expiry_slot=packet.rlc_timer_expiry_slot)
                packet.harq_attempt_index += 1
                packet.physical_attempts_in_recovery_episode += 1
                packet.attempt_count += 1; counts["transmissions"] += 1
                if packet.first_tx_slot is None:
                    packet.first_tx_slot = slot
                if tx.is_scheduled_rescue:
                    counts["rescue_transmissions"] += 1
                    packet.scheduled_transmission_count += 1
                    if packet.scheduled_first_tx_slot is None:
                        packet.scheduled_first_tx_slot = slot
                    if packet.rescue_grant_ready_slot is not None:
                        packet.scheduled_queue_wait_slots += max(
                            0, slot - packet.rescue_grant_ready_slot)
                    if isinstance(self.recovery, LegacyRlcScheduledPolicy):
                        timing = legacy_timing_records.get((
                            packet.packet_id,
                            packet.rlc_retransmission_count))
                        if timing is not None and timing[
                                "scheduled_first_tx_slot"] is None:
                            timing["scheduled_grant_dci_slot"] = (
                                packet.scheduled_grant_dci_slot)
                            timing["scheduled_first_tx_slot"] = slot
                            timing["sr_to_scheduled_tx_slots"] = (
                                slot - int(timing["actual_sr_slot"]))
                            timing["scheduled_queue_wait_slots"] = max(
                                0, slot - int(
                                    timing["scheduled_request_queue_entry"]))
                    packet.rescue_request_due_slot = None
                    packet.rescue_grant_ready_slot = None
                else:
                    counts["gf_transmissions"] += int(self.access.allows_resource_overlap)
                    if (packet.rlc_retransmission_count > 0 and
                            packet.next_cb_recovery_tx_slot is None):
                        packet.next_cb_recovery_tx_slot = slot
                if packet.measurement_cohort:
                    measurement_counts["transmissions"] += 1
                    if tx.is_scheduled_rescue:
                        measurement_counts["rescue_transmissions"] += 1
                    elif self.access.allows_resource_overlap:
                        measurement_counts["gf_transmissions"] += 1
                    if packet.harq_attempt_index > 1:
                        measurement_counts["harq_retx"] += 1
                    elif packet.rlc_retransmission_count > 0:
                        measurement_counts["rlc_retx_tx"] += 1
                        recovery_this_slot += 1
                    else:
                        measurement_counts["initial_tx"] += 1
                process = self.harq.transmitted(packet.harq_attempt_index, slot)
                packet.last_tx_slot = slot
                if (not tx.is_scheduled_rescue and rvs[tx.ue_id] == 2 and
                        packet.early_rescue_trigger_slot is None):
                    packet.rv2_tx_slot = slot
                packet.state = PacketState.HARQ_FEEDBACK_WAIT
                packet.feedback_due_slot = process.feedback_due_slot
                packet.feedback_value = int(outcome.feedback[tx.ue_id] == 1 and outcome.decoded_bits[tx.ue_id] >= packet.size_bits)
                crc_pass = (
                    outcome.crc_pass[tx.ue_id]
                    if len(outcome.crc_pass) == self.parameters.num_ues
                    else None)
                payload_correct = (
                    outcome.payload_correct[tx.ue_id]
                    if len(outcome.payload_correct) == self.parameters.num_ues
                    else None)
                undetected_error = (
                    outcome.undetected_error[tx.ue_id]
                    if len(outcome.undetected_error) == self.parameters.num_ues
                    else None)
                payload_hamming_distance = (
                    outcome.payload_hamming_distance[tx.ue_id]
                    if len(outcome.payload_hamming_distance) == self.parameters.num_ues
                    else None)
                if crc_pass is None:
                    crc_pass = packet.feedback_value == 1
                if payload_correct is None:
                    payload_correct = packet.feedback_value == 1
                if undetected_error is None:
                    undetected_error = False
                if bool(crc_pass) != (outcome.feedback[tx.ue_id] == 1):
                    raise RuntimeError("CRC diagnostic disagrees with PHY feedback")
                if bool(undetected_error) != (
                        bool(crc_pass) and not bool(payload_correct)):
                    raise RuntimeError("undetected-error diagnostic is inconsistent")
                packet.last_transmission_was_rescue = tx.is_scheduled_rescue
                packet.last_transmission_access = (
                    "scheduled_rescue" if tx.is_scheduled_rescue else
                    "grant_free" if self.access.allows_resource_overlap else "scheduled"
                )
                packet.last_transmission_mcs = slot_mcs[tx.ue_id]
                packet.last_state_slot = slot
                transmission_rv = rvs[tx.ue_id] if history_aware else None
                transmission_rv_history = None
                transmission_sinr_history_db = None
                transmission_harq_information_state = None
                if history_aware:
                    state = h_states[tx.ue_id]
                    current = outcome.effective_sinr_db[tx.ue_id]
                    if state is None or current is None or transmission_rv is None:
                        raise RuntimeError("history-aware transmission is missing state")
                    state = combining_backend.append_episode_state(
                        state, rv=transmission_rv, effective_sinr_db=current)
                    harq_combining_state[packet.harq_episode_id] = state
                    state_diagnostics = combining_backend.state_trace(state)
                    transmission_rv_history = state_diagnostics["rv_history"]
                    transmission_sinr_history_db = state_diagnostics.get(
                        "effective_sinr_history_db")
                    transmission_harq_information_state = state_diagnostics.get(
                        "harq_information_state")
                    record(packet, slot, ("harq_combining_state_stored"
                           if len(state.rv_history) == 1
                           else "harq_combining_state_extended"),
                           **state_diagnostics)
                elif (combining_backend is not None and self.scheduled_rescue is not None
                        and self.scheduled_rescue.mode == ScheduledRescueMode.HARQ_PRESERVING):
                    if (packet.harq_attempt_index == 1 and
                            packet.feedback_value == 0):
                        if packet.mac_tb_id is None or packet.harq_episode_id is None:
                            raise RuntimeError("transmission context is not initialized")
                        state = combining_backend.capture_failed_episode(
                            mac_tb_id=packet.mac_tb_id,
                            harq_episode_id=packet.harq_episode_id,
                            mcs_index=slot_mcs[tx.ue_id], outcome=outcome,
                            ue_id=tx.ue_id)
                        harq_combining_state[packet.harq_episode_id] = state
                        record(packet, slot, "harq_combining_state_stored",
                               rv_history=list(state.rv_history),
                               effective_sinr_history_db=list(
                                   state.effective_sinr_history_db))
                    elif tx.is_scheduled_rescue:
                        state = harq_combining_state[packet.harq_episode_id]
                        current = outcome.effective_sinr_db[tx.ue_id]
                        if current is None:
                            raise RuntimeError("rescue outcome is missing SINR")
                        state = state.append(
                            rv=combining_backend.rescue_rv,
                            effective_sinr_db=current)
                        harq_combining_state[packet.harq_episode_id] = state
                        record(packet, slot, "harq_combining_state_extended",
                               rv_history=list(state.rv_history),
                               effective_sinr_history_db=list(
                                   state.effective_sinr_history_db))
                per_re_values = outcome.post_equalization_sinr_active_db[tx.ue_id]
                enhanced_trace = ({
                    "same_prb_occupancy": attempt_occupancy[
                        (tx.resource_domain, tx.resource_id)],
                    "post_equalization_sinr_min_db": (min(per_re_values)
                        if per_re_values else None),
                    "post_equalization_sinr_mean_db": (statistics.fmean(per_re_values)
                        if per_re_values else None),
                    "post_equalization_sinr_max_db": (max(per_re_values)
                        if per_re_values else None),
                } if self.enhanced_attempt_trace else {})
                record(packet, slot, "phy_transmission",
                       ue_id=packet.ue_id,
                       harq_attempt=packet.harq_attempt_index,
                       recovery_episode_physical_attempt=(
                           packet.physical_attempts_in_recovery_episode),
                       rlc_retransmissions=packet.rlc_retransmission_count,
                       rlc_retx_index=packet.rlc_retransmission_count,
                       actual_tx_slot=slot,
                       feedback_available_slot=process.feedback_due_slot,
                       scheduled_queue_wait=(
                           max(0, slot - packet.current_scheduled_queue_entry_slot)
                           if tx.is_scheduled_rescue and
                           packet.current_scheduled_queue_entry_slot is not None
                           else None),
                       scheduled_grant_dci_slot=(
                           packet.scheduled_grant_dci_slot
                           if tx.is_scheduled_rescue else None),
                       transmission_access=packet.last_transmission_access,
                       resource_id=tx.resource_id,
                       resource_domain=tx.resource_domain,
                       phy_resource_id=(tx.phy_resource_id
                                        if tx.phy_resource_id is not None
                                        else tx.resource_id),
                       resource_semantics=("non_contention_based" if tx.is_scheduled_rescue
                                           else "contention_based" if self.access.allows_resource_overlap
                                           else "non_contention_based"),
                       mcs_index=slot_mcs[tx.ue_id],
                       redundancy_version=transmission_rv,
                       rv_history=transmission_rv_history,
                       effective_sinr_history_db=transmission_sinr_history_db,
                       harq_information_state=transmission_harq_information_state,
                       effective_sinr_db=outcome.effective_sinr_db[tx.ue_id],
                       post_equalization_sinr_active_db=(list(per_re_values)
                           if self.retain_per_re_trace else None),
                       modeled_tbler=outcome.tbler[tx.ue_id],
                       phy_feedback=outcome.feedback[tx.ue_id],
                       crc_pass=bool(crc_pass),
                       payload_correct=bool(payload_correct),
                       undetected_error=bool(undetected_error),
                       payload_hamming_distance=payload_hamming_distance,
                       **enhanced_trace)
                pending = PendingHarqFeedback(
                    packet=packet,
                    mac_tb_id=packet.mac_tb_id,
                    harq_episode_id=packet.harq_episode_id,
                    phy_attempt_index=packet.phy_attempt_index,
                    feedback_value=packet.feedback_value,
                    payload_correct=bool(payload_correct),
                    undetected_error=bool(undetected_error),
                )
                if process.feedback_due_slot == slot: immediate.append(pending)
                else: pending_feedback.setdefault(process.feedback_due_slot, []).append(pending)
            for pending in immediate: feedback(pending, slot)
            if slot >= self.parameters.warmup_slots:
                recovery_tx_per_slot.append(recovery_this_slot)
                rlc_eligible_per_slot.append(eligible_this_slot)
                if slot < self.parameters.duration_slots:
                    for name in diagnostic_names:
                        diagnostic_series[name].append(slot_diagnostics[name])

        cohort_completed = [p for p in completed if p.measurement_cohort]
        cohort_dropped = [p for p in dropped if p.measurement_cohort]
        cohort_pending = sum(p.measurement_cohort for q in queues for p in q.packets())
        cohort_correct = [p for p in cohort_completed if p.payload_correct]
        latencies = [p.latency_slots for p in cohort_correct
                     if p.latency_slots is not None]
        receiver_accepted_latencies = [p.latency_slots for p in cohort_completed
                                       if p.latency_slots is not None]
        terminated = completed + dropped
        measurement_slots = self.parameters.duration_slots - self.parameters.warmup_slots
        # The calibrated denominator is the fixed measurement-window budget.
        # Drain work is still exposed as endogenous attempts, not extra rho.
        base_total_ru = measurement_slots * self.access.resource_pool_size
        dedicated_total_ru = (
            measurement_slots * self.scheduled_resource_policy.scheduled_capacity
            if self.scheduled_resource_policy.dedicated else 0)
        total_ru = base_total_ru + dedicated_total_ru
        resolved_feedback = counts["acks"] + counts["nacks"]
        unresolved_feedback = counts["transmissions"] - resolved_feedback
        if unresolved_feedback < 0:
            raise RuntimeError("resolved HARQ feedback exceeds PHY transmissions")
        deadline_success = {
            str(deadline): {
                "successful_packets": sum(
                    p.latency_slots is not None and p.latency_slots <= deadline
                    for p in cohort_correct
                ),
                "ratio_of_arrivals": _ratio(sum(
                    p.latency_slots is not None and p.latency_slots <= deadline
                    for p in cohort_correct
                ), measurement_counts["arrived"]),
            }
            for deadline in self.parameters.latency_deadlines_slots
        }
        rescue_mode = self.scheduled_rescue.name if self.scheduled_rescue else "disabled"
        h_rescue = rescue_mode == ScheduledRescueMode.HARQ_PRESERVING.value
        f_rescue = rescue_mode == ScheduledRescueMode.FRESH_TB.value
        packet_rows = [{
            "ue_id": p.ue_id, "payload_id": p.higher_layer_payload_id,
            "generation_slot": p.arrival_slot, "first_tx_slot": p.first_tx_slot,
            "completion_slot": p.completion_slot,
            "harq_termination_slot": p.harq_termination_slot,
            "final_observation_slot": p.completion_slot,
            "completion_latency_slots": p.latency_slots,
            "delivered": p.state == PacketState.DELIVERED,
            "drop_reason": p.drop_reason,
            "number_of_phy_attempts": p.attempt_count,
            "number_of_harq_episodes": p.transmission_context_generation + 1,
            "number_of_rlc_retransmissions": p.rlc_retransmission_count,
            "terminal_reason": (
                p.drop_reason if p.drop_reason is not None else
                "receiver_accepted" if p.receiver_accepted else "unfinished"),
            "t_poll_slots": (
                self.recovery.poll_retransmit_slots
                if isinstance(self.recovery, LegacyRlcScheduledPolicy)
                else None),
            "sr_period_slots": (
                self.recovery.sr_period_slots
                if isinstance(self.recovery, LegacyRlcScheduledPolicy)
                else None),
            "sr_phase": (
                self.recovery.sr_phase(p.ue_id)
                if isinstance(self.recovery, LegacyRlcScheduledPolicy)
                else None),
            "fast_arq_triggered": p.fast_arq_triggered,
            "fast_rescue_triggered": p.rescue_triggered,
            "scheduled_queue_delay_slots": p.scheduled_queue_wait_slots,
            "scheduled_transmission_count": p.scheduled_transmission_count,
            "initial_harq_final_failure_slot": p.initial_harq_final_failure_slot,
            "fast_arq_trigger_slot": p.fast_arq_trigger_slot,
            "scheduled_request_slot": p.scheduled_request_slot,
            "grant_ready_slot": p.scheduled_grant_ready_slot,
            "scheduled_queue_entry_slot": p.scheduled_queue_entry_slot,
            "scheduled_first_tx_slot": p.scheduled_first_tx_slot,
            "scheduled_completion_slot": p.scheduled_completion_slot,
            "scheduled_queue_wait_slots": p.scheduled_queue_wait_slots,
            "scheduled_harq_attempts": p.scheduled_transmission_count,
            "scheduled_grant_dci_slot": p.scheduled_grant_dci_slot,
            "early_rescue_triggered": p.early_rescue_trigger_slot is not None,
            "early_rescue_trigger_slot": p.early_rescue_trigger_slot,
            "rv2_tx_slot": p.rv2_tx_slot,
            "rv2_nack_slot": p.rv2_nack_slot,
            "rescue_trigger_to_tx_delay": (
                p.scheduled_first_tx_slot - p.early_rescue_trigger_slot
                if p.scheduled_first_tx_slot is not None and
                p.early_rescue_trigger_slot is not None else None),
            "legacy_timer_expiry_slot": p.legacy_timer_expiry_slot,
            "legacy_recovery_eligible_slot": p.legacy_recovery_eligible_slot,
            "next_cb_recovery_tx_slot": p.next_cb_recovery_tx_slot,
            "effective_final_harq_to_recovery_gap": (
                p.next_cb_recovery_tx_slot - p.initial_harq_final_failure_slot
                if p.next_cb_recovery_tx_slot is not None and
                p.initial_harq_final_failure_slot is not None else None),
            "legacy_timer_wait_slots": (
                p.legacy_recovery_eligible_slot -
                p.initial_harq_final_failure_slot
                if p.legacy_recovery_eligible_slot is not None and
                p.initial_harq_final_failure_slot is not None else None),
            "cb_resource_wait_slots": (
                p.next_cb_recovery_tx_slot - p.legacy_recovery_eligible_slot
                if p.next_cb_recovery_tx_slot is not None and
                p.legacy_recovery_eligible_slot is not None else None),
            "first_recovery_harq_duration_slots": (
                p.first_recovery_harq_termination_slot -
                p.next_cb_recovery_tx_slot
                if p.first_recovery_harq_termination_slot is not None and
                p.next_cb_recovery_tx_slot is not None else None),
            "recovery_phase_latency_slots": (
                p.completion_slot - p.initial_harq_final_failure_slot
                if p.receiver_accepted and p.completion_slot is not None and
                p.initial_harq_final_failure_slot is not None else None),
            "correct_recovery_phase_latency_slots": (
                p.completion_slot - p.initial_harq_final_failure_slot
                if p.receiver_accepted and p.payload_correct and
                p.completion_slot is not None and
                p.initial_harq_final_failure_slot is not None else None),
            "receiver_accepted": p.receiver_accepted,
            "payload_correct": p.payload_correct,
            "undetected_error": p.undetected_error,
            "delivered_correctly": p.receiver_accepted and p.payload_correct,
            "terminal_status": (
                "accepted_undetected_error" if p.undetected_error else
                "accepted_correct" if p.receiver_accepted else
                "protocol_drop" if p.state == PacketState.DROPPED else
                "unfinished"),
        } for p in cohort_completed + cohort_dropped +
            [p for q in queues for p in q.packets() if p.measurement_cohort]]
        cb_prb_use = len({(e["slot"], e["resource_id"]) for e in trace
                          if e["event"] == "phy_transmission" and
                          e["transmission_access"] == "grant_free" and
                          self.parameters.warmup_slots <= e["slot"] <
                          self.parameters.duration_slots})
        scheduled_prb_use = sum(
            e["event"] == "phy_transmission" and
            e["transmission_access"] == "scheduled_rescue" and
            e.get("resource_domain", "base") == "base" and
            self.parameters.warmup_slots <= e["slot"] < self.parameters.duration_slots
            for e in trace)
        dedicated_sb_use = sum(
            e["event"] == "phy_transmission" and
            e["transmission_access"] == "scheduled_rescue" and
            e.get("resource_domain") == "dedicated_sb" and
            self.parameters.warmup_slots <= e["slot"] < self.parameters.duration_slots
            for e in trace)
        result = {"schema_version": 4, "parameters": asdict(self.parameters),
            "recovery_configuration": {"harq_max_attempts": self.harq.max_attempts,
                "harq_feedback_delay_slots": self.harq.feedback_delay_slots,
                "harq_pusch_spacing_slots": self.harq.pusch_spacing_slots,
                "harq_error_model": ("history_aware_conditional_success"
                    if isinstance(self.phy_backend, HarqPhyBackend)
                    and self.phy_backend.history_aware else "memoryless_or_v04b_rescue_only"),
                "policy": self.recovery.name, "max_rlc_retransmissions": self.max_rlc_retransmissions,
                "direct_scheduled_k2_slots": self.direct_scheduled_k2_slots,
                "scheduled_resource_policy": {
                    "identifier": self.scheduled_resource_policy.identifier,
                    "mode": self.scheduled_resource_policy.mode.value,
                    "base_cb_prbs": self.scheduled_resource_policy.base_cb_prbs,
                    "scheduled_capacity": self.scheduled_resource_policy.scheduled_capacity,
                },
                "scheduled_rescue": ({"mode": self.scheduled_rescue.name,
                    "request_delay_slots": self.scheduled_rescue.request_delay_slots,
                    "scheduled_grant_delay_slots": self.scheduled_rescue.scheduled_grant_delay_slots,
                    "rescue_mcs_index": self.scheduled_rescue.rescue_mcs_index,
                    "phy_combining": (
                        "calibrated_conditional_harq_ir"
                        if isinstance(self.phy_backend, HarqPhyBackend)
                        and self.scheduled_rescue.mode == ScheduledRescueMode.HARQ_PRESERVING
                        else "none")} if self.scheduled_rescue else None)},
            "phases": {"measurement_slots": measurement_slots,
                "warmup_slots": self.parameters.warmup_slots,
                "max_drain_slots": self.parameters.max_drain_slots,
                "drain_slots_used": drain_slots_used,
                "drain_timeout_reached": bool(sum(map(len, queues)))},
            "packets": {"arrived": measurement_counts["arrived"], "new_packets_generated": measurement_counts["arrived"],
                "completed": len(cohort_completed), "dropped": len(cohort_dropped),
                "pending_at_end": cohort_pending, "unfinished_after_drain": cohort_pending,
                "completion_fraction_of_arrivals": _ratio(len(cohort_completed), measurement_counts["arrived"]),
                "delivery_ratio_after_drain": _ratio(len(cohort_completed), measurement_counts["arrived"]),
                "drop_ratio_after_drain": _ratio(len(cohort_dropped), measurement_counts["arrived"]),
                "unfinished_ratio_after_drain": _ratio(cohort_pending, measurement_counts["arrived"]),
                "packet_success_probability_terminated": _ratio(len(cohort_completed), len(cohort_completed) + len(cohort_dropped))},
            "oracle_reliability": {
                "receiver_acceptances": measurement_counts["receiver_acceptances"],
                "correct_deliveries": measurement_counts["correct_deliveries"],
                "undetected_errors": measurement_counts["undetected_errors"],
                "protocol_drops": len(cohort_dropped),
                "receiver_acceptance_probability": _ratio(
                    measurement_counts["receiver_acceptances"],
                    measurement_counts["arrived"]),
                "correct_delivery_probability": _ratio(
                    measurement_counts["correct_deliveries"],
                    measurement_counts["arrived"]),
                "undetected_error_probability": _ratio(
                    measurement_counts["undetected_errors"],
                    measurement_counts["arrived"]),
                "protocol_drop_probability": _ratio(
                    len(cohort_dropped), measurement_counts["arrived"]),
                "receiver_observable": "CRC status",
                "simulator_oracle": "decoded payload equals transmitted payload"},
            "latency_slots": {"mean": statistics.fmean(latencies) if latencies else None,
                "median": statistics.median(latencies) if latencies else None,
                "p50": _percentile(latencies, 50), "p95": _percentile(latencies, 95),
                "p99": _percentile(latencies, 99), "samples": latencies,
                "population": "correctly delivered measurement-cohort packets",
                "deadline_success": deadline_success},
            "receiver_accepted_latency_slots": {
                "mean": statistics.fmean(receiver_accepted_latencies)
                    if receiver_accepted_latencies else None,
                "p95": _percentile(receiver_accepted_latencies, 95),
                "p99": _percentile(receiver_accepted_latencies, 99),
                "samples": receiver_accepted_latencies,
                "population": "receiver-accepted measurement-cohort packets"},
            "latency_components_slots": {"queue_and_access_wait": [p.access_wait_slots for p in cohort_correct],
                "harq_feedback_wait": [p.harq_feedback_wait_slots for p in cohort_correct],
                "harq_retransmission_wait": [p.harq_retransmission_wait_slots for p in cohort_correct],
                "rlc_recovery_wait": [p.rlc_recovery_wait_slots for p in cohort_correct]},
            "reliability": {"transmission_attempts": counts["transmissions"],
                "resolved_feedback_attempts": resolved_feedback,
                "unresolved_feedback_attempts": unresolved_feedback,
                "successful_attempts": counts["acks"],
                "failed_attempts": counts["nacks"],
                "transmission_success_probability": _ratio(counts["acks"], resolved_feedback),
                "transmission_failure_probability": _ratio(counts["nacks"], resolved_feedback),
                "probability_population": "PHY transmissions with resolved HARQ feedback",
                "mean_retry_count_terminated": statistics.fmean([max(p.attempt_count-1,0) for p in terminated]) if terminated else None,
                "retry_count_samples": [max(p.attempt_count-1,0) for p in terminated]},
            "recovery": {"total_phy_transmissions": counts["transmissions"], "phy_nack_count": counts["nacks"],
                "harq_retransmissions": counts["harq_retx"], "final_harq_failures": counts["final_failures"],
                "abandoned_harq_episodes": counts["abandoned_harq_episodes"],
                "rlc_retransmissions": counts["rlc_retx"], "legacy_arq_recovery_events": counts["legacy_events"],
                "fast_arq_recovery_events": counts["fast_events"], "packets_dropped_rlc_limit": counts["rlc_limit_drops"],
                "mean_harq_attempts_per_delivered_packet": statistics.fmean([p.attempt_count for p in completed]) if completed else None,
                "mean_rlc_retransmissions_per_terminated_packet": statistics.fmean([p.rlc_retransmission_count for p in terminated]) if terminated else None,
                "harq_feedback_wait_slots": sum(p.harq_feedback_wait_slots for p in completed + dropped),
                "rlc_recovery_wait_slots": sum(p.rlc_recovery_wait_slots for p in completed + dropped)},
            "rescue": {"mode": rescue_mode,
                "requests": measurement_counts["rescue_requests"],
                "grants": measurement_counts["rescue_grants"],
                "transmissions": measurement_counts["rescue_transmissions"],
                "successes": measurement_counts["rescue_successes"],
                "success_rate_per_rescue_transmission": _ratio(
                    measurement_counts["rescue_successes"],
                    measurement_counts["rescue_transmissions"]),
                "fraction_of_packets_rescued": _ratio(
                    measurement_counts["rescue_successes"], measurement_counts["arrived"]),
                "scheduled_harq_rescue_requests": measurement_counts["rescue_requests"] if h_rescue else 0,
                "scheduled_harq_rescue_grants": measurement_counts["rescue_grants"] if h_rescue else 0,
                "scheduled_harq_rescue_transmissions": measurement_counts["rescue_transmissions"] if h_rescue else 0,
                "scheduled_harq_rescue_successes": measurement_counts["rescue_successes"] if h_rescue else 0,
                "fresh_tb_rescue_requests": measurement_counts["rescue_requests"] if f_rescue else 0,
                "fresh_tb_rescue_grants": measurement_counts["rescue_grants"] if f_rescue else 0,
                "fresh_tb_rescue_transmissions": measurement_counts["rescue_transmissions"] if f_rescue else 0,
                "fresh_tb_rescue_successes": measurement_counts["rescue_successes"] if f_rescue else 0,
                "abandoned_harq_episodes": measurement_counts["abandoned_harq_episodes"],
                "early_payload_requeues": measurement_counts["early_payload_requeues"],
                "stale_feedback_discarded": counts["stale_feedback_discarded"]},
            "measurement_traffic": {**measurement_counts,
                "exogenous_packets_per_measurement_slot": _ratio(measurement_counts["arrived"], measurement_slots),
                "harq_retransmissions_per_measurement_slot": _ratio(measurement_counts["harq_retx"], measurement_slots),
                "rlc_retransmissions_per_measurement_slot": _ratio(measurement_counts["rlc_retx_tx"], measurement_slots),
                "grant_free_attempts_per_measurement_slot": _ratio(measurement_counts["gf_transmissions"], measurement_slots),
                "scheduled_rescue_attempts_per_measurement_slot": _ratio(measurement_counts["rescue_transmissions"], measurement_slots),
                "total_phy_attempts_per_measurement_slot": _ratio(measurement_counts["transmissions"], measurement_slots)},
            "recovery_pressure": {"rlc_eligible_per_slot": rlc_eligible_per_slot,
                "recovery_originated_phy_transmissions_per_slot": recovery_tx_per_slot,
                "peak_recovery_originated_transmissions_per_slot": max(recovery_tx_per_slot, default=0),
                "p95_recovery_originated_transmissions_per_slot": _percentile(recovery_tx_per_slot, 95),
                "fraction_slots_with_recovery_traffic": _ratio(sum(x > 0 for x in recovery_tx_per_slot), len(recovery_tx_per_slot))},
            "traffic_diagnostics": {"measurement_slot_origin": self.parameters.warmup_slots,
                "series": diagnostic_series,
                "summaries": {name: _series_summary(values)
                    for name, values in diagnostic_series.items()}},
            "resources": {"resource_units_available": total_ru,
                "base_resource_units_available": base_total_ru,
                "additional_dedicated_resource_units_available": dedicated_total_ru,
                "combined_physical_resource_units_available": total_ru,
                "ue_resource_allocations": measurement_counts["transmissions"],
                "scheduled_rescue_resource_units": sum(diagnostic_series["scheduled_rescue_attempts"]),
                "grant_free_ue_attempts": measurement_counts["gf_transmissions"],
                "common_pool_accounting": "scheduled rescue reserves non-CB units from the same per-slot physical pool; GF uses only the remainder",
                "occupied_resource_units": overlap["occupied"], "resource_utilization": _ratio(overlap["occupied"], total_ru),
                "overlap_resource_events": counts["overlap_events"], "overlap_probability_per_occupied_resource": _ratio(counts["overlap_events"], counts["occupied"]),
                "overlapped_transmissions": counts["overlapped_transmissions"], "overlapped_transmission_probability": _ratio(counts["overlapped_transmissions"], counts["transmissions"]),
                "overlap_measurement": {**overlap, "fraction_overlap_events_involving_recovery": _ratio(overlap["recovery_events"], overlap["events"])},
                "successful_bits_per_total_available_resource": _ratio(measurement_counts["bits"], total_ru),
                "successful_bits_per_occupied_resource": _ratio(measurement_counts["bits"], overlap["occupied"]),
                "successful_bits_per_ue_attempt": _ratio(measurement_counts["bits"], measurement_counts["transmissions"]),
                "successful_bits_per_allocated_resource": _ratio(counts["bits"], counts["transmissions"]), "occupancy_samples": occupancy_samples},
            "signaling": {"abstract_requests": counts["requests"], "abstract_grants": counts["grants"],
                "scheduled_rescue_requests": counts["rescue_requests"],
                "scheduled_rescue_grants": counts["rescue_grants"],
                "harq_feedback_events": counts["feedback_events"], "legacy_arq_recovery_trigger_events": counts["legacy_events"],
                "fast_arq_failure_indication_events": counts["fast_events"]},
            "event_trace": trace}
        if self.extended_outputs:
            result["schema_version"] = 4
            result["recovery_configuration"]["scheduled_rlc_recovery"] = (
                self.scheduled_rlc_recovery)
            result["resources"].update({
                "cb_prb_use": cb_prb_use,
                "scheduled_prb_use": scheduled_prb_use,
                "dedicated_sb_resource_use": dedicated_sb_use,
                "combined_physical_resource_use": (
                    cb_prb_use + scheduled_prb_use + dedicated_sb_use),
                "prbs_unavailable_to_cb_due_to_scheduled_use": scheduled_prb_use,
                "scheduled_queue_delay_slots": [
                    row["scheduled_queue_delay_slots"] for row in packet_rows
                    if row["scheduled_transmission_count"] > 0],
            })
            result["packet_rows"] = packet_rows
            result["slot_resource_rows"] = slot_resource_rows
            result["legacy_timing_rows"] = [
                legacy_timing_records[key]
                for key in sorted(legacy_timing_records)]
        return result

    def _validate_decision(self, transmissions: Sequence[Any]) -> None:
        ue_ids = [x.ue_id for x in transmissions]
        if len(ue_ids) != len(set(ue_ids)): raise ValueError("an access decision may attempt at most one packet per UE")
        identities = []
        for tx in transmissions:
            if tx.resource_domain == "base":
                if tx.resource_id < 0 or tx.resource_id >= self.access.resource_pool_size:
                    raise ValueError("access decision exceeds the base physical resource pool")
            elif tx.resource_domain == "dedicated_sb":
                if (not tx.is_scheduled_rescue or
                        tx.phy_resource_id is None or
                        tx.phy_resource_id < 0 or
                        tx.phy_resource_id >= self.scheduled_resource_policy.scheduled_capacity):
                    raise ValueError("invalid dedicated scheduled-resource assignment")
            else:
                raise ValueError(f"unknown resource domain: {tx.resource_domain}")
            identities.append((tx.resource_domain, tx.resource_id))
        if (not self.access.allows_resource_overlap and
                len(identities) != len(set(identities))):
            raise ValueError("orthogonal access assigned a resource more than once")
