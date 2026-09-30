"""Frozen V0.3a phased calibration engine and resource accounting.

This module intentionally retains the accepted V0.3a temporary retry semantics
solely to reproduce historical calibration artifacts. New simulations use the
explicit V0.3b recovery engine in :mod:`ul_access.protocol.simulator`; V0.3a
outputs must not be interpreted as HARQ or RLC ARQ results.
"""

from __future__ import annotations

import random
import statistics
from dataclasses import asdict, dataclass
from typing import Any

from ul_access.phy.interface import PhyBackend
from ul_access.protocol.access import AccessPolicy, Transmission
from ul_access.protocol.capacity import NominalCapacity
from ul_access.protocol.models import Packet, PacketQueue
from ul_access.protocol.simulator import TrafficSource, _percentile, _ratio
from ul_access.resource import ResourceAllocation, ResourceAssignment


@dataclass(frozen=True)
class CalibratedSimulationParameters:
    """Controls for warm-up, measurement, and bounded drain simulation."""

    warmup_slots: int
    measurement_slots: int
    max_drain_slots: int
    num_ues: int
    seed: int
    traffic_parameter: float
    normalized_load: float
    nominal_capacity: NominalCapacity
    tx_power_dbm: tuple[float, ...]
    mcs_index: tuple[int, ...]


def _new_phase_counters() -> dict[str, int]:
    return {
        "slots": 0,
        "arrivals": 0,
        "completion_events": 0,
        "drop_events": 0,
        "available_physical_resources": 0,
        "occupied_physical_resources": 0,
        "idle_physical_resources": 0,
        "overlapped_physical_resources": 0,
        "successful_physical_resources": 0,
        "failed_physical_resources": 0,
        "ue_transmission_attempts": 0,
        "successful_ue_resource_uses": 0,
        "failed_ue_resource_uses": 0,
        "successfully_delivered_bits": 0,
        "abstract_requests": 0,
        "abstract_grants": 0,
    }


def _validate_decision(
    access: AccessPolicy,
    transmissions: tuple[Transmission, ...],
) -> None:
    ue_ids = [transmission.ue_id for transmission in transmissions]
    resource_ids = [transmission.resource_id for transmission in transmissions]
    if len(ue_ids) != len(set(ue_ids)):
        raise ValueError("an access decision may attempt at most one packet per UE")
    if any(
        resource_id < 0 or resource_id >= access.resource_pool_size
        for resource_id in resource_ids
    ):
        raise ValueError("access decision exceeds the physical resource pool")
    if (
        not access.allows_resource_overlap
        and len(resource_ids) != len(set(resource_ids))
    ):
        raise ValueError("orthogonal access assigned a resource more than once")
    if len(set(resource_ids)) > access.resource_pool_size:
        raise ValueError("occupied resources exceed the physical resource pool")


class CalibratedSlotSimulator:
    """Reproduce the accepted V0.3a phased calibration semantics."""

    def __init__(
        self,
        *,
        access_mode: str,
        parameters: CalibratedSimulationParameters,
        traffic: TrafficSource,
        access: AccessPolicy,
        phy_backend: PhyBackend,
    ) -> None:
        if min(
            parameters.warmup_slots,
            parameters.measurement_slots,
            parameters.max_drain_slots,
        ) < 0:
            raise ValueError("phase durations must be non-negative")
        if parameters.measurement_slots <= 0:
            raise ValueError("measurement_slots must be positive")
        if parameters.num_ues != phy_backend.num_ues:
            raise ValueError("simulation and PHY backend UE counts differ")
        if access.resource_pool_size != phy_backend.num_subcarriers:
            raise ValueError("access pool and PHY resource counts differ")
        if (
            access.resource_pool_size
            != parameters.nominal_capacity.num_resources_per_slot
        ):
            raise ValueError("access and nominal-capacity resource budgets differ")
        if parameters.nominal_capacity.packets_per_resource != 1:
            raise ValueError(
                "current slot engine supports exactly one packet per UE-resource use"
            )
        if len(parameters.tx_power_dbm) != parameters.num_ues:
            raise ValueError("one transmit-power value is required per UE")
        if len(parameters.mcs_index) != parameters.num_ues:
            raise ValueError("one MCS value is required per UE")
        self.access_mode = access_mode
        self.parameters = parameters
        self.traffic = traffic
        self.access = access
        self.phy_backend = phy_backend

    def run(self) -> dict[str, Any]:
        queues = [PacketQueue(ue_id) for ue_id in range(self.parameters.num_ues)]
        access_rng = random.Random(self.parameters.seed)
        phase_counters = {
            phase: _new_phase_counters()
            for phase in ("warmup", "measurement", "drain")
        }
        measurement_packet_ids: set[int] = set()
        completed_measurement: list[Packet] = []
        dropped_measurement: list[Packet] = []
        queue_length_trace: list[int] = []
        queue_trace_phase: list[str] = []
        resource_trace = {
            "phase": [],
            "allocated_ue_resource_uses": [],
            "occupied_physical_resources": [],
            "idle_physical_resources": [],
            "overlapped_physical_resources": [],
            "successful_physical_resources": [],
            "failed_physical_resources": [],
        }

        end_warmup = self.parameters.warmup_slots
        end_measurement = end_warmup + self.parameters.measurement_slots
        absolute_slot = 0
        while absolute_slot < end_measurement:
            phase = "warmup" if absolute_slot < end_warmup else "measurement"
            self._run_slot(
                absolute_slot,
                phase,
                generate_traffic=True,
                queues=queues,
                access_rng=access_rng,
                phase_counters=phase_counters,
                measurement_packet_ids=measurement_packet_ids,
                completed_measurement=completed_measurement,
                dropped_measurement=dropped_measurement,
                queue_length_trace=queue_length_trace,
                queue_trace_phase=queue_trace_phase,
                resource_trace=resource_trace,
            )
            absolute_slot += 1

        unfinished_at_measurement_end = self._count_measurement_packets(
            queues, measurement_packet_ids
        )
        drain_slots_used = 0
        while (
            any(len(queue) for queue in queues)
            and drain_slots_used < self.parameters.max_drain_slots
        ):
            self._run_slot(
                absolute_slot,
                "drain",
                generate_traffic=False,
                queues=queues,
                access_rng=access_rng,
                phase_counters=phase_counters,
                measurement_packet_ids=measurement_packet_ids,
                completed_measurement=completed_measurement,
                dropped_measurement=dropped_measurement,
                queue_length_trace=queue_length_trace,
                queue_trace_phase=queue_trace_phase,
                resource_trace=resource_trace,
            )
            absolute_slot += 1
            drain_slots_used += 1

        drain_timeout_reached = any(len(queue) for queue in queues)
        unfinished_after_drain = self._count_measurement_packets(
            queues, measurement_packet_ids
        )
        return self._build_result(
            phase_counters=phase_counters,
            completed=completed_measurement,
            dropped=dropped_measurement,
            unfinished_at_measurement_end=unfinished_at_measurement_end,
            unfinished_after_drain=unfinished_after_drain,
            drain_slots_used=drain_slots_used,
            drain_timeout_reached=drain_timeout_reached,
            queue_length_trace=queue_length_trace,
            queue_trace_phase=queue_trace_phase,
            resource_trace=resource_trace,
        )

    def _run_slot(
        self,
        slot: int,
        phase: str,
        *,
        generate_traffic: bool,
        queues: list[PacketQueue],
        access_rng: random.Random,
        phase_counters: dict[str, dict[str, int]],
        measurement_packet_ids: set[int],
        completed_measurement: list[Packet],
        dropped_measurement: list[Packet],
        queue_length_trace: list[int],
        queue_trace_phase: list[str],
        resource_trace: dict[str, list[Any]],
    ) -> None:
        counters = phase_counters[phase]
        counters["slots"] += 1
        if generate_traffic:
            for packet in self.traffic.arrivals(slot):
                queues[packet.ue_id].enqueue(packet)
                counters["arrivals"] += 1
                if phase == "measurement":
                    measurement_packet_ids.add(packet.packet_id)

        decision = self.access.decide(slot, queues, access_rng)
        _validate_decision(self.access, decision.transmissions)
        counters["abstract_requests"] += decision.request_count
        counters["abstract_grants"] += decision.grant_count
        occupancy = [0] * self.access.resource_pool_size
        for transmission in decision.transmissions:
            occupancy[transmission.resource_id] += 1
        occupied = sum(value > 0 for value in occupancy)
        overlapped = sum(value > 1 for value in occupancy)
        idle = self.access.resource_pool_size - occupied
        if occupied > self.access.resource_pool_size or idle < 0:
            raise RuntimeError("physical resource accounting exceeded the pool")

        successful_by_resource = [0] * self.access.resource_pool_size
        if decision.transmissions:
            allocation = ResourceAllocation.from_assignments(
                (
                    ResourceAssignment(
                        ue_id=transmission.ue_id,
                        subcarrier=transmission.resource_id,
                    )
                    for transmission in decision.transmissions
                ),
                num_ofdm_symbols=1,
                num_subcarriers=self.access.resource_pool_size,
                num_ues=self.parameters.num_ues,
            )
            outcome = self.phy_backend.evaluate(
                allocation,
                tx_power_dbm=self.parameters.tx_power_dbm,
                mcs_index=self.parameters.mcs_index,
                realization_seed=self.parameters.seed + 1_000_003 + slot,
            )
            for transmission in decision.transmissions:
                packet = transmission.packet
                packet.attempt_count += 1
                ue_id = transmission.ue_id
                success = (
                    outcome.feedback[ue_id] == 1
                    and outcome.decoded_bits[ue_id] >= packet.size_bits
                )
                if success:
                    successful_by_resource[transmission.resource_id] += 1
                    counters["successful_ue_resource_uses"] += 1
                    counters["successfully_delivered_bits"] += packet.size_bits
                    counters["completion_events"] += 1
                    packet.completion_slot = slot
                    popped = queues[ue_id].pop()
                    if popped is not packet:
                        raise RuntimeError("access attempted a non-head packet")
                    self.access.packet_removed(
                        ue_id, queue_empty=len(queues[ue_id]) == 0
                    )
                    if packet.packet_id in measurement_packet_ids:
                        completed_measurement.append(packet)
                else:
                    counters["failed_ue_resource_uses"] += 1
                    if packet.attempt_count > self.access.max_retries:
                        counters["drop_events"] += 1
                        popped = queues[ue_id].pop()
                        if popped is not packet:
                            raise RuntimeError("access attempted a non-head packet")
                        self.access.packet_removed(
                            ue_id, queue_empty=len(queues[ue_id]) == 0
                        )
                        if packet.packet_id in measurement_packet_ids:
                            dropped_measurement.append(packet)
                    else:
                        self.access.prepare_retry(packet, slot)

        successful_physical = sum(value > 0 for value in successful_by_resource)
        failed_physical = sum(
            occupancy[resource] > 0 and successful_by_resource[resource] == 0
            for resource in range(self.access.resource_pool_size)
        )
        if successful_physical + failed_physical != occupied:
            raise RuntimeError("successful/failed resources do not sum to occupied")
        counters["available_physical_resources"] += self.access.resource_pool_size
        counters["occupied_physical_resources"] += occupied
        counters["idle_physical_resources"] += idle
        counters["overlapped_physical_resources"] += overlapped
        counters["successful_physical_resources"] += successful_physical
        counters["failed_physical_resources"] += failed_physical
        counters["ue_transmission_attempts"] += len(decision.transmissions)

        resource_trace["phase"].append(phase)
        resource_trace["allocated_ue_resource_uses"].append(
            len(decision.transmissions)
        )
        resource_trace["occupied_physical_resources"].append(occupied)
        resource_trace["idle_physical_resources"].append(idle)
        resource_trace["overlapped_physical_resources"].append(overlapped)
        resource_trace["successful_physical_resources"].append(
            successful_physical
        )
        resource_trace["failed_physical_resources"].append(failed_physical)
        queue_length_trace.append(sum(len(queue) for queue in queues))
        queue_trace_phase.append(phase)

    @staticmethod
    def _count_measurement_packets(
        queues: list[PacketQueue], measurement_packet_ids: set[int]
    ) -> int:
        return sum(
            packet_id in measurement_packet_ids
            for queue in queues
            for packet_id in queue.snapshot()
        )

    def _build_result(
        self,
        *,
        phase_counters: dict[str, dict[str, int]],
        completed: list[Packet],
        dropped: list[Packet],
        unfinished_at_measurement_end: int,
        unfinished_after_drain: int,
        drain_slots_used: int,
        drain_timeout_reached: bool,
        queue_length_trace: list[int],
        queue_trace_phase: list[str],
        resource_trace: dict[str, list[Any]],
    ) -> dict[str, Any]:
        measurement = phase_counters["measurement"]
        generated = measurement["arrivals"]
        accounted = len(completed) + len(dropped) + unfinished_after_drain
        if accounted != generated:
            raise RuntimeError(
                "measurement packet accounting is not conservative: "
                f"generated={generated}, accounted={accounted}"
            )
        latencies = [
            packet.latency_slots
            for packet in completed
            if packet.latency_slots is not None
        ]
        retries = [
            max(packet.attempt_count - 1, 0) for packet in completed + dropped
        ]
        measurement_queue = [
            value
            for value, phase in zip(queue_length_trace, queue_trace_phase)
            if phase == "measurement"
        ]
        quarter = max(1, len(measurement_queue) // 4)
        pending_trend = statistics.fmean(
            measurement_queue[-quarter:]
        ) - statistics.fmean(measurement_queue[:quarter])
        status = self._stability_status(
            drain_timeout_reached=drain_timeout_reached,
            unfinished_after_drain=unfinished_after_drain,
            pending_trend=pending_trend,
            resource_utilization=_ratio(
                measurement["occupied_physical_resources"],
                measurement["available_physical_resources"],
            )
            or 0.0,
        )
        attempts = measurement["ue_transmission_attempts"]
        delivered_bits = measurement["successfully_delivered_bits"]
        occupied = measurement["occupied_physical_resources"]
        available = measurement["available_physical_resources"]
        return {
            "schema_version": 1,
            "access_mode": self.access_mode,
            "traffic_parameter_packets_per_ue_per_slot": (
                self.parameters.traffic_parameter
            ),
            "normalized_offered_load": self.parameters.normalized_load,
            "seed": self.parameters.seed,
            "nominal_capacity": self.parameters.nominal_capacity.as_dict(),
            "phases": {
                "warmup_slots": self.parameters.warmup_slots,
                "measurement_slots": self.parameters.measurement_slots,
                "max_drain_slots": self.parameters.max_drain_slots,
                "drain_slots_used": drain_slots_used,
                "drain_timeout_reached": drain_timeout_reached,
                "traffic_generated_during": ["warmup", "measurement"],
                "primary_metrics_collected_during": "measurement",
            },
            "packets": {
                "generated_measurement": generated,
                "completed_measurement_population_after_drain": len(completed),
                "dropped_measurement_population_after_drain": len(dropped),
                "unfinished_at_measurement_end": unfinished_at_measurement_end,
                "unfinished_after_drain": unfinished_after_drain,
                "delivery_ratio_after_drain": _ratio(len(completed), generated),
            },
            "rates_packets_per_slot": {
                "offered_mean_configured": (
                    self.parameters.num_ues * self.parameters.traffic_parameter
                ),
                "arrival_rate_observed_measurement": (
                    generated / self.parameters.measurement_slots
                ),
                "completion_event_rate_measurement": (
                    measurement["completion_events"]
                    / self.parameters.measurement_slots
                ),
            },
            "latency_slots_measurement_arrivals": {
                "population": "measurement-arrival packets completed by drain end",
                "sample_count": len(latencies),
                "mean": statistics.fmean(latencies) if latencies else None,
                "p50": _percentile(latencies, 50.0),
                "p95": _percentile(latencies, 95.0),
                "p99": _percentile(latencies, 99.0),
                "samples": latencies,
            },
            "reliability_measurement": {
                "ue_transmission_attempts": attempts,
                "successful_ue_resource_uses": measurement[
                    "successful_ue_resource_uses"
                ],
                "failed_ue_resource_uses": measurement[
                    "failed_ue_resource_uses"
                ],
                "transmission_failure_rate": _ratio(
                    measurement["failed_ue_resource_uses"], attempts
                ),
                "mean_protocol_retry_count_terminated": (
                    statistics.fmean(retries) if retries else None
                ),
                "protocol_retry_count_samples": retries,
                "retry_semantics": "temporary protocol retry; not HARQ/RLC/Fast ARQ",
            },
            "resources_measurement": {
                "total_available_physical_resources": available,
                "allocated_physical_resources": occupied,
                "idle_physical_resources": measurement[
                    "idle_physical_resources"
                ],
                "overlapped_physical_resources": measurement[
                    "overlapped_physical_resources"
                ],
                "successful_physical_resources": measurement[
                    "successful_physical_resources"
                ],
                "failed_physical_resources": measurement[
                    "failed_physical_resources"
                ],
                "ue_transmission_attempts": attempts,
                "physical_resource_utilization": _ratio(occupied, available),
                "overlap_rate_per_occupied_physical_resource": _ratio(
                    measurement["overlapped_physical_resources"], occupied
                ),
                "delivered_bits_per_total_available_physical_resource": _ratio(
                    delivered_bits, available
                ),
                "delivered_bits_per_occupied_physical_resource": _ratio(
                    delivered_bits, occupied
                ),
                "delivered_bits_per_ue_transmission_attempt": _ratio(
                    delivered_bits, attempts
                ),
            },
            "abstract_dynamic_control_transactions_measurement": {
                "requests": measurement["abstract_requests"],
                "grants": measurement["abstract_grants"],
                "total": measurement["abstract_requests"]
                + measurement["abstract_grants"],
                "unit": "modeled events, not bits or exact NR channel uses",
            },
            "queues": {
                "mean_total_queue_length_measurement": statistics.fmean(
                    measurement_queue
                ),
                "maximum_total_queue_length_measurement": max(measurement_queue),
                "pending_packet_trend_last_minus_first_quarter": pending_trend,
                "total_queue_length_trace": queue_length_trace,
                "trace_phase": queue_trace_phase,
            },
            "phase_counters": phase_counters,
            "resource_trace": resource_trace,
            "stability_status": status,
        }

    def _stability_status(
        self,
        *,
        drain_timeout_reached: bool,
        unfinished_after_drain: int,
        pending_trend: float,
        resource_utilization: float,
    ) -> str:
        rho = self.parameters.normalized_load
        if (
            rho >= 1.0
            or drain_timeout_reached
            or (unfinished_after_drain > 0 and pending_trend > 0)
        ):
            return "overloaded"
        if rho >= 0.8 or resource_utilization >= 0.85 or pending_trend > 1.0:
            return "near_saturation"
        return "stable"
