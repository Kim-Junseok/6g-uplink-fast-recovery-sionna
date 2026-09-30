"""Fast deterministic tests for packet and access simulation behavior."""

from __future__ import annotations

import random
import unittest
from collections.abc import Sequence

from ul_access.phy import PhyBackend, PhyOutcome
from ul_access.protocol import (
    BernoulliTrafficGenerator,
    GrantFreeAccess,
    Packet,
    PacketQueue,
    ScheduledAccess,
    SimulationParameters,
    SlotSimulator,
)
from ul_access.resource import ResourceAllocation


class ScriptedPhyBackend(PhyBackend):
    """Small Sionna-free backend used to isolate protocol unit tests."""

    def __init__(
        self,
        *,
        num_ues: int,
        num_subcarriers: int,
        success_by_call: Sequence[bool] = (True,),
    ) -> None:
        self._num_ues = num_ues
        self._num_subcarriers = num_subcarriers
        self._success_by_call = tuple(success_by_call)
        self._calls = 0

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
        call_index = min(self._calls, len(self._success_by_call) - 1)
        call_success = self._success_by_call[call_index]
        self._calls += 1
        active = allocation.allocated_re_per_ue()[0].tolist()
        feedback = tuple(
            (1 if call_success else 0) if count else -1 for count in active
        )
        return PhyOutcome(
            effective_sinr_db=tuple(10.0 if count else None for count in active),
            tbler=tuple(0.0 if count else None for count in active),
            feedback=feedback,
            decoded_bits=tuple(24 if value == 1 else 0 for value in feedback),
            post_equalization_sinr_active_db=tuple(
                (10.0,) if count else () for count in active
            ),
            tx_power_per_active_re_w=tuple(
                0.001 if count else None for count in active
            ),
            bler=tuple(0.0 if count else None for count in active),
        )


class UndetectedErrorPhyBackend(ScriptedPhyBackend):
    """Return a receiver ACK with an oracle-incorrect payload."""

    def evaluate(self, allocation, *, tx_power_dbm, mcs_index,
                 realization_seed):
        outcome = super().evaluate(
            allocation, tx_power_dbm=tx_power_dbm, mcs_index=mcs_index,
            realization_seed=realization_seed)
        return PhyOutcome(
            outcome.effective_sinr_db, outcome.tbler, outcome.feedback,
            outcome.decoded_bits, outcome.post_equalization_sinr_active_db,
            outcome.tx_power_per_active_re_w, outcome.bler,
            crc_pass=(True,), payload_correct=(False,),
            undetected_error=(True,), payload_hamming_distance=(1,))


class OnePacketTraffic:
    def arrivals(self, slot: int) -> tuple[Packet, ...]:
        if slot != 0:
            return ()
        return (Packet(0, 0, 0, 24),)


class PacketAndAccessTest(unittest.TestCase):
    def test_crc_undetected_error_acks_without_recovery(self) -> None:
        result = SlotSimulator(
            parameters=SimulationParameters(
                duration_slots=2, num_ues=1, seed=1,
                tx_power_dbm=(0.0,), mcs_index=(10,), max_drain_slots=2),
            traffic=OnePacketTraffic(),
            access=GrantFreeAccess(
                resource_pool_size=1, opportunity_period_slots=1,
                max_retries=1, retry_backoff_slots=0),
            phy_backend=UndetectedErrorPhyBackend(
                num_ues=1, num_subcarriers=1),
            extended_outputs=True).run()
        packet = result["packet_rows"][0]
        self.assertTrue(packet["receiver_accepted"])
        self.assertFalse(packet["payload_correct"])
        self.assertTrue(packet["undetected_error"])
        self.assertFalse(packet["delivered_correctly"])
        self.assertEqual(packet["terminal_status"],
                         "accepted_undetected_error")
        self.assertEqual(result["oracle_reliability"]["undetected_errors"], 1)
        self.assertEqual(
            result["oracle_reliability"]["receiver_acceptance_probability"]
            + result["oracle_reliability"]["protocol_drop_probability"], 1.0)
        self.assertEqual(
            result["oracle_reliability"]["correct_delivery_probability"]
            + result["oracle_reliability"]["undetected_error_probability"]
            + result["oracle_reliability"]["protocol_drop_probability"], 1.0)
        self.assertEqual(result["latency_slots"]["samples"], [])
        self.assertEqual(result["receiver_accepted_latency_slots"]["samples"],
                         [0])
        self.assertEqual(result["recovery"]["final_harq_failures"], 0)
        self.assertEqual(result["recovery"]["total_phy_transmissions"], 1)

    def test_packet_queue_is_fifo_and_checks_ownership(self) -> None:
        queue = PacketQueue(0)
        first = Packet(1, 0, 0, 24)
        second = Packet(2, 0, 1, 24)
        queue.enqueue(first)
        queue.enqueue(second)
        self.assertEqual(queue.snapshot(), (1, 2))
        self.assertIs(queue.pop(), first)
        self.assertIs(queue.peek(), second)
        with self.assertRaises(ValueError):
            queue.enqueue(Packet(3, 1, 2, 24))

    def test_scheduled_request_and_grant_timing(self) -> None:
        queue = PacketQueue(0)
        queue.enqueue(Packet(0, 0, 0, 24))
        access = ScheduledAccess(
            resource_pool_size=1,
            request_period_slots=4,
            grant_delay_slots=2,
            max_retries=1,
            retry_delay_slots=1,
        )
        first = access.decide(0, [queue], random.Random(1))
        second = access.decide(1, [queue], random.Random(1))
        third = access.decide(2, [queue], random.Random(1))
        self.assertEqual(first.request_count, 1)
        self.assertEqual(first.transmissions, ())
        self.assertEqual(second.transmissions, ())
        self.assertEqual(len(third.transmissions), 1)
        self.assertEqual(third.grant_count, 1)

    def test_grant_free_selection_can_overlap(self) -> None:
        queues = [PacketQueue(0), PacketQueue(1)]
        queues[0].enqueue(Packet(0, 0, 0, 24))
        queues[1].enqueue(Packet(1, 1, 0, 24))
        access = GrantFreeAccess(
            resource_pool_size=2,
            opportunity_period_slots=1,
            max_retries=1,
            retry_backoff_slots=1,
        )
        decision = access.decide(0, queues, random.Random(1))
        self.assertEqual(
            [item.resource_id for item in decision.transmissions], [0, 0]
        )

    def test_retry_and_latency_are_separate_from_overlap(self) -> None:
        parameters = SimulationParameters(3, 1, 9, (0.0,), (10,))
        access = GrantFreeAccess(
            resource_pool_size=1,
            opportunity_period_slots=1,
            max_retries=1,
            retry_backoff_slots=1,
        )
        result = SlotSimulator(
            parameters=parameters,
            traffic=OnePacketTraffic(),
            access=access,
            phy_backend=ScriptedPhyBackend(
                num_ues=1, num_subcarriers=1, success_by_call=(False, True)
            ),
        ).run()
        self.assertEqual(result["reliability"]["transmission_attempts"], 2)
        self.assertEqual(result["reliability"]["failed_attempts"], 1)
        self.assertEqual(result["reliability"]["retry_count_samples"], [1])
        self.assertEqual(result["latency_slots"]["samples"], [1])
        self.assertEqual(result["resources"]["overlap_resource_events"], 0)

    def test_phy_backend_interface_and_deterministic_replay(self) -> None:
        self.assertIsInstance(
            ScriptedPhyBackend(num_ues=2, num_subcarriers=2), PhyBackend
        )

        def simulate() -> dict:
            parameters = SimulationParameters(20, 2, 77, (0.0, 0.0), (10, 10))
            return SlotSimulator(
                parameters=parameters,
                traffic=BernoulliTrafficGenerator(
                    num_ues=2,
                    arrival_probability_per_ue=0.4,
                    packet_size_bits=24,
                    seed=77,
                ),
                access=GrantFreeAccess(
                    resource_pool_size=2,
                    opportunity_period_slots=1,
                    max_retries=1,
                    retry_backoff_slots=1,
                ),
                phy_backend=ScriptedPhyBackend(num_ues=2, num_subcarriers=2),
            ).run()

        self.assertEqual(simulate(), simulate())


if __name__ == "__main__":
    unittest.main()
