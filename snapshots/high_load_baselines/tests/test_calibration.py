"""Fast deterministic tests for V0.3a baseline calibration."""

from __future__ import annotations

import json
import random
import unittest
from collections.abc import Sequence

from ul_access.phy import PhyBackend, PhyOutcome
from ul_access.protocol import (
    BernoulliTrafficGenerator,
    CalibratedSimulationParameters,
    CalibratedSlotSimulator,
    GrantFreeAccess,
    Packet,
    PacketQueue,
    ScheduledAccess,
    aggregate_calibration_runs,
    nominal_capacity,
    normalized_offered_load,
    student_t_confidence_interval_95,
)
from ul_access.resource import ResourceAllocation


class AlwaysSuccessfulBackend(PhyBackend):
    """Sionna-free PHY that succeeds for every active UE."""

    def __init__(self, num_ues: int, num_subcarriers: int) -> None:
        self._num_ues = num_ues
        self._num_subcarriers = num_subcarriers

    @property
    def num_ues(self) -> int:
        return self._num_ues

    @property
    def num_subcarriers(self) -> int:
        return self._num_subcarriers

    def evaluate(
        self,
        allocation: ResourceAllocation,
        *,
        tx_power_dbm: Sequence[float],
        mcs_index: Sequence[int],
        realization_seed: int,
    ) -> PhyOutcome:
        del tx_power_dbm, mcs_index, realization_seed
        active = allocation.allocated_re_per_ue()[0].tolist()
        feedback = tuple(1 if count else -1 for count in active)
        return PhyOutcome(
            effective_sinr_db=tuple(10.0 if count else None for count in active),
            tbler=tuple(0.0 if count else None for count in active),
            feedback=feedback,
            decoded_bits=tuple(24 if count else 0 for count in active),
            post_equalization_sinr_active_db=tuple(
                (10.0,) if count else () for count in active
            ),
            tx_power_per_active_re_w=tuple(
                0.001 if count else None for count in active
            ),
            bler=tuple(0.0 if count else None for count in active),
        )


class RecordingTraffic:
    """Return configured arrivals and expose which slots were queried."""

    def __init__(self, arrivals_by_slot: dict[int, tuple[Packet, ...]]) -> None:
        self.arrivals_by_slot = arrivals_by_slot
        self.called_slots: list[int] = []

    def arrivals(self, slot: int) -> tuple[Packet, ...]:
        self.called_slots.append(slot)
        return self.arrivals_by_slot.get(slot, ())


def parameters(
    *,
    warmup: int,
    measurement: int,
    drain: int,
    num_ues: int = 1,
    resources: int = 1,
    seed: int = 17,
    rho: float = 0.5,
) -> CalibratedSimulationParameters:
    return CalibratedSimulationParameters(
        warmup_slots=warmup,
        measurement_slots=measurement,
        max_drain_slots=drain,
        num_ues=num_ues,
        seed=seed,
        traffic_parameter=rho * resources / num_ues,
        normalized_load=rho,
        nominal_capacity=nominal_capacity(
            num_resources_per_slot=resources,
            resource_payload_bits=24,
            packet_size_bits=24,
        ),
        tx_power_dbm=(0.0,) * num_ues,
        mcs_index=(10,) * num_ues,
    )


class CalibrationTest(unittest.TestCase):
    def test_nominal_capacity_and_normalized_system_load(self) -> None:
        capacity = nominal_capacity(
            num_resources_per_slot=4,
            resource_payload_bits=48,
            packet_size_bits=24,
        )
        self.assertEqual(capacity.packets_per_resource, 2)
        self.assertEqual(capacity.nominal_packets_per_slot, 8)
        self.assertAlmostEqual(
            normalized_offered_load(
                num_ues=8,
                arrival_probability_per_ue=0.3,
                nominal_packets_per_slot=4,
            ),
            0.6,
        )

    def test_shared_resource_budget_is_enforced(self) -> None:
        with self.assertRaisesRegex(ValueError, "resource budgets differ"):
            CalibratedSlotSimulator(
                access_mode="scheduled",
                parameters=parameters(
                    warmup=0, measurement=1, drain=0, resources=2
                ),
                traffic=RecordingTraffic({}),
                access=ScheduledAccess(
                    resource_pool_size=1,
                    request_period_slots=1,
                    grant_delay_slots=0,
                    max_retries=0,
                    retry_delay_slots=0,
                ),
                phy_backend=AlwaysSuccessfulBackend(1, 1),
            )

    def test_queue_eligibility_reuses_grant_until_queue_empty(self) -> None:
        queue = PacketQueue(0)
        queue.enqueue(Packet(0, 0, 0, 24))
        queue.enqueue(Packet(1, 0, 0, 24))
        access = ScheduledAccess(
            resource_pool_size=1,
            request_period_slots=4,
            grant_delay_slots=0,
            max_retries=0,
            retry_delay_slots=0,
            eligibility_mode="queue_until_empty",
        )
        first = access.decide(0, [queue], random.Random(1))
        self.assertEqual(first.request_count, 1)
        self.assertEqual(len(first.transmissions), 1)
        queue.pop()
        access.packet_removed(0, queue_empty=False)
        second = access.decide(1, [queue], random.Random(1))
        self.assertEqual(second.request_count, 0)
        self.assertEqual(len(second.transmissions), 1)

    def test_scheduled_and_grant_free_respect_physical_pool(self) -> None:
        arrivals = {
            0: tuple(Packet(ue, ue, 0, 24) for ue in range(3))
        }
        for name, access in (
            (
                "scheduled",
                ScheduledAccess(
                    resource_pool_size=2,
                    request_period_slots=1,
                    grant_delay_slots=0,
                    max_retries=0,
                    retry_delay_slots=0,
                    eligibility_mode="queue_until_empty",
                ),
            ),
            (
                "grant_free",
                GrantFreeAccess(
                    resource_pool_size=2,
                    opportunity_period_slots=1,
                    max_retries=0,
                    retry_backoff_slots=0,
                ),
            ),
        ):
            with self.subTest(access=name):
                result = CalibratedSlotSimulator(
                    access_mode=name,
                    parameters=parameters(
                        warmup=0,
                        measurement=1,
                        drain=2,
                        num_ues=3,
                        resources=2,
                    ),
                    traffic=RecordingTraffic(arrivals),
                    access=access,
                    phy_backend=AlwaysSuccessfulBackend(3, 2),
                ).run()
                trace = result["resource_trace"]
                self.assertLessEqual(
                    max(trace["occupied_physical_resources"]), 2
                )
                self.assertTrue(
                    all(
                        occupied + idle == 2
                        for occupied, idle in zip(
                            trace["occupied_physical_resources"],
                            trace["idle_physical_resources"],
                        )
                    )
                )
                if name == "scheduled":
                    self.assertLessEqual(
                        max(trace["allocated_ue_resource_uses"]), 2
                    )

    def test_phases_stop_traffic_and_drain_measurement_packet(self) -> None:
        traffic = RecordingTraffic(
            {
                0: (Packet(0, 0, 0, 24),),
                1: (Packet(1, 0, 1, 24),),
            }
        )
        result = CalibratedSlotSimulator(
            access_mode="scheduled",
            parameters=parameters(warmup=1, measurement=1, drain=4),
            traffic=traffic,
            access=ScheduledAccess(
                resource_pool_size=1,
                request_period_slots=1,
                grant_delay_slots=2,
                max_retries=0,
                retry_delay_slots=0,
                eligibility_mode="queue_until_empty",
            ),
            phy_backend=AlwaysSuccessfulBackend(1, 1),
        ).run()
        self.assertEqual(traffic.called_slots, [0, 1])
        self.assertEqual(result["phases"]["drain_slots_used"], 2)
        self.assertEqual(result["phase_counters"]["warmup"]["slots"], 1)
        self.assertEqual(result["phase_counters"]["measurement"]["slots"], 1)
        self.assertEqual(result["phase_counters"]["drain"]["slots"], 2)
        self.assertEqual(result["phase_counters"]["drain"]["completion_events"], 2)
        self.assertEqual(result["packets"]["unfinished_at_measurement_end"], 1)
        self.assertEqual(result["packets"]["unfinished_after_drain"], 0)
        self.assertEqual(
            result["packets"]["completed_measurement_population_after_drain"],
            1,
        )
        self.assertEqual(
            result["latency_slots_measurement_arrivals"]["samples"], [2]
        )

    def test_drain_timeout_preserves_unfinished_population(self) -> None:
        traffic = RecordingTraffic({0: (Packet(0, 0, 0, 24),)})
        result = CalibratedSlotSimulator(
            access_mode="scheduled",
            parameters=parameters(warmup=0, measurement=1, drain=0),
            traffic=traffic,
            access=ScheduledAccess(
                resource_pool_size=1,
                request_period_slots=1,
                grant_delay_slots=5,
                max_retries=0,
                retry_delay_slots=0,
                eligibility_mode="queue_until_empty",
            ),
            phy_backend=AlwaysSuccessfulBackend(1, 1),
        ).run()
        self.assertTrue(result["phases"]["drain_timeout_reached"])
        self.assertEqual(result["packets"]["generated_measurement"], 1)
        self.assertEqual(
            result["packets"]["completed_measurement_population_after_drain"],
            0,
        )
        self.assertEqual(result["packets"]["unfinished_after_drain"], 1)
        self.assertEqual(
            result["latency_slots_measurement_arrivals"]["sample_count"], 0
        )
        self.assertEqual(result["stability_status"], "overloaded")
        self.assertGreater(result["queues"]["mean_total_queue_length_measurement"], 0)
        self.assertEqual(result["queues"]["maximum_total_queue_length_measurement"], 1)

    def test_deterministic_multi_seed_replay_and_json_schema(self) -> None:
        def run(seed: int) -> dict:
            return CalibratedSlotSimulator(
                access_mode="grant_free",
                parameters=parameters(
                    warmup=2,
                    measurement=6,
                    drain=4,
                    num_ues=2,
                    resources=2,
                    seed=seed,
                    rho=0.4,
                ),
                traffic=BernoulliTrafficGenerator(
                    num_ues=2,
                    arrival_probability_per_ue=0.4,
                    packet_size_bits=24,
                    seed=seed,
                ),
                access=GrantFreeAccess(
                    resource_pool_size=2,
                    opportunity_period_slots=1,
                    max_retries=0,
                    retry_backoff_slots=0,
                ),
                phy_backend=AlwaysSuccessfulBackend(2, 2),
            ).run()

        first = [run(seed) for seed in (11, 12, 13)]
        second = [run(seed) for seed in (11, 12, 13)]
        self.assertEqual(first, second)
        self.assertEqual({item["stability_status"] for item in first}, {"stable"})
        aggregate = aggregate_calibration_runs(first)
        encoded = json.dumps({"runs": first, "aggregate": aggregate})
        self.assertIsInstance(json.loads(encoded), dict)
        self.assertEqual(
            aggregate["independent_sampling_unit"], "seed-level simulation run"
        )
        overloaded = [dict(item) for item in first]
        overloaded[0]["stability_status"] = "overloaded"
        overloaded_aggregate = aggregate_calibration_runs(overloaded)
        self.assertFalse(overloaded_aggregate["confidence_intervals_reported"])
        self.assertIsNone(
            overloaded_aggregate["primary_metrics"][
                "mean_packet_latency_slots"
            ]["confidence_interval_95"]
        )

    def test_student_t_confidence_interval(self) -> None:
        interval = student_t_confidence_interval_95([1.0, 2.0, 3.0, 4.0])
        self.assertEqual(interval["sample_count"], 4)
        self.assertAlmostEqual(interval["mean"], 2.5)
        self.assertAlmostEqual(interval["half_width"], 2.053972167938667, places=6)
        self.assertAlmostEqual(interval["lower"], 0.446027832061333, places=6)
        self.assertAlmostEqual(interval["upper"], 4.553972167938667, places=6)


if __name__ == "__main__":
    unittest.main()
