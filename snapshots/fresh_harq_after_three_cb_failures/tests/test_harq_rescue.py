"""V0.4a deterministic identity, timing, and resource-accounting tests."""

from __future__ import annotations

import hashlib
import json
import unittest
from collections.abc import Sequence
from pathlib import Path

from ul_access.phy import PhyBackend, PhyOutcome
from ul_access.protocol import (
    GrantFreeAccess,
    Packet,
    SimulationParameters,
    SlotSimulator,
)
from ul_access.recovery import (
    FastArqPolicy,
    HarqController,
    ScheduledRescueConfig,
    ScheduledRescueMode,
)
from ul_access.resource import ResourceAllocation


class ScriptedPhy(PhyBackend):
    def __init__(self, outcomes: Sequence[bool], *, num_ues: int = 1,
                 num_resources: int = 1):
        self.outcomes = tuple(outcomes)
        self.calls = 0
        self._num_ues = num_ues
        self._num_resources = num_resources
        self.mcs_calls: list[tuple[int, ...]] = []

    @property
    def num_ues(self):
        return self._num_ues

    @property
    def num_subcarriers(self):
        return self._num_resources

    def evaluate(self, allocation: ResourceAllocation, *, tx_power_dbm,
                 mcs_index, realization_seed):
        del tx_power_dbm, realization_seed
        self.mcs_calls.append(tuple(mcs_index))
        success = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        active = allocation.allocated_re_per_ue()[0].tolist()
        feedback = tuple(1 if success and value else (0 if value else -1)
                         for value in active)
        decoded = tuple(24 if value == 1 else 0 for value in feedback)
        optional = tuple(10.0 if value else None for value in active)
        tbler = tuple(0.0 if value else None for value in active)
        return PhyOutcome(optional, tbler, feedback, decoded,
                          tuple((10.0,) if value else () for value in active),
                          tuple(0.001 if value else None for value in active), tbler)


class OnePacket:
    def arrivals(self, slot):
        return (Packet(7, 0, 0, 24),) if slot == 0 else ()


class SimultaneousPackets:
    def __init__(self, count):
        self.count = count

    def arrivals(self, slot):
        return tuple(Packet(100 + ue, ue, 0, 24) for ue in range(self.count)) \
            if slot == 0 else ()


def run_rescue(mode, outcomes=(False, True), *, request_delay=1,
               grant_delay=2, duration=14, max_rlc=0,
               rescue_mcs=None):
    backend = ScriptedPhy(outcomes)
    result = SlotSimulator(
        parameters=SimulationParameters(
            duration, 1, 42, (0.0,), (10,),
            latency_deadlines_slots=(4, 8),
        ),
        traffic=OnePacket(),
        access=GrantFreeAccess(
            resource_pool_size=1,
            opportunity_period_slots=1,
            max_retries=0,
            retry_backoff_slots=0,
        ),
        phy_backend=backend,
        harq=HarqController(max_attempts=2, feedback_delay_slots=1),
        recovery=FastArqPolicy(failure_indication_delay_slots=1),
        max_rlc_retransmissions=max_rlc,
        scheduled_rescue=ScheduledRescueConfig(
            mode=mode,
            request_delay_slots=request_delay,
            scheduled_grant_delay_slots=grant_delay,
            rescue_mcs_index=rescue_mcs,
        ),
    ).run()
    return result, backend


def events(result, name):
    return [event for event in result["event_trace"] if event["event"] == name]


class HarqRescueTest(unittest.TestCase):
    def test_h_preserves_all_context_ids_and_increments_attempt(self):
        result, _ = run_rescue(ScheduledRescueMode.HARQ_PRESERVING)
        tx = events(result, "phy_transmission")
        self.assertEqual([event["slot"] for event in tx], [0, 4])
        self.assertEqual([event["payload_id"] for event in tx], [7, 7])
        self.assertEqual(len({event["tb_id"] for event in tx}), 1)
        self.assertEqual(len({event["harq_episode_id"] for event in tx}), 1)
        self.assertEqual([event["attempt_index"] for event in tx], [1, 2])
        self.assertEqual(tx[1]["transmission_access"], "scheduled_rescue")
        self.assertEqual(tx[1]["resource_semantics"], "non_contention_based")
        self.assertEqual(result["rescue"]["scheduled_harq_rescue_successes"], 1)
        self.assertEqual(sum(values[0] for values in
                             result["latency_components_slots"].values()),
                         result["latency_slots"]["samples"][0])

    def test_f_retains_payload_replaces_context_and_resets_attempt(self):
        result, _ = run_rescue(ScheduledRescueMode.FRESH_TB)
        tx = events(result, "phy_transmission")
        self.assertEqual([event["slot"] for event in tx], [0, 4])
        self.assertEqual([event["payload_id"] for event in tx], [7, 7])
        self.assertNotEqual(tx[0]["tb_id"], tx[1]["tb_id"])
        self.assertNotEqual(tx[0]["harq_episode_id"], tx[1]["harq_episode_id"])
        self.assertEqual([event["attempt_index"] for event in tx], [1, 1])
        abandoned = events(result, "harq_episode_abandoned_for_rescue")
        self.assertEqual(len(abandoned), 1)
        self.assertEqual(abandoned[0]["old_tb_id"], tx[0]["tb_id"])
        self.assertEqual(abandoned[0]["old_harq_episode_id"],
                         tx[0]["harq_episode_id"])
        self.assertEqual(result["rescue"]["abandoned_harq_episodes"], 1)
        self.assertEqual(result["rescue"]["early_payload_requeues"], 1)
        self.assertEqual(result["recovery"]["rlc_retransmissions"], 0)
        self.assertEqual(sum(values[0] for values in
                             result["latency_components_slots"].values()),
                         result["latency_slots"]["samples"][0])

    def test_request_and_grant_delays_prevent_early_phy_rescue(self):
        result, _ = run_rescue(ScheduledRescueMode.HARQ_PRESERVING)
        self.assertEqual(events(result, "harq_nack")[0]["slot"], 1)
        self.assertEqual(events(result, "scheduled_rescue_request")[0]["slot"], 2)
        self.assertEqual(events(result, "scheduled_rescue_grant")[0]["slot"], 4)
        rescue_tx = [event for event in events(result, "phy_transmission")
                     if event["transmission_access"] == "scheduled_rescue"]
        self.assertEqual(rescue_tx[0]["slot"], 4)

    def test_initial_ack_never_triggers_rescue(self):
        result, _ = run_rescue(ScheduledRescueMode.HARQ_PRESERVING,
                               outcomes=(True,))
        self.assertEqual(result["rescue"]["requests"], 0)
        self.assertEqual(result["rescue"]["grants"], 0)
        self.assertEqual(result["rescue"]["transmissions"], 0)
        self.assertFalse(events(result, "scheduled_rescue_triggered"))

    def test_successful_rescue_delivers_payload_once(self):
        for mode in ScheduledRescueMode:
            with self.subTest(mode=mode):
                result, _ = run_rescue(mode)
                self.assertEqual(result["packets"]["completed"], 1)
                self.assertEqual(len(events(result, "harq_ack")), 1)
                self.assertEqual(result["packets"]["arrived"], 1)

    def test_f_old_episode_has_no_later_feedback(self):
        result, _ = run_rescue(ScheduledRescueMode.FRESH_TB)
        abandoned = events(result, "harq_episode_abandoned_for_rescue")[0]
        later_feedback = [event for event in result["event_trace"]
                          if event["slot"] > abandoned["slot"] and
                          event["event"] in {"harq_ack", "harq_nack"}]
        self.assertTrue(later_feedback)
        self.assertTrue(all(event["harq_episode_id"] !=
                            abandoned["old_harq_episode_id"]
                            for event in later_feedback))
        self.assertEqual(result["rescue"]["stale_feedback_discarded"], 0)

    def test_repeated_failure_is_bounded_and_conserves_packet(self):
        for mode in ScheduledRescueMode:
            with self.subTest(mode=mode):
                result, _ = run_rescue(mode, outcomes=(False,), duration=25,
                                       max_rlc=1)
                packets = result["packets"]
                self.assertEqual(packets["arrived"], packets["completed"] +
                                 packets["dropped"] + packets["pending_at_end"])
                self.assertEqual(packets["dropped"], 1)
                self.assertLessEqual(result["recovery"]["total_phy_transmissions"], 5)
                self.assertEqual(len(events(result, "scheduled_rescue_triggered")), 1)

    def test_rescue_uses_same_configured_mcs_for_h_and_f(self):
        calls = []
        for mode in ScheduledRescueMode:
            result, backend = run_rescue(mode, rescue_mcs=4)
            rescue_tx = [event for event in events(result, "phy_transmission")
                         if event["transmission_access"] == "scheduled_rescue"]
            calls.append((rescue_tx[0]["mcs_index"], backend.mcs_calls[-1][0]))
        self.assertEqual(calls, [(4, 4), (4, 4)])

    def test_rescue_is_orthogonal_and_consumes_the_shared_capacity(self):
        backend = ScriptedPhy((False, True), num_ues=4, num_resources=2)
        result = SlotSimulator(
            parameters=SimulationParameters(12, 4, 42, (0.0,) * 4, (10,) * 4),
            traffic=SimultaneousPackets(4),
            access=GrantFreeAccess(resource_pool_size=2,
                opportunity_period_slots=1, max_retries=0,
                retry_backoff_slots=0),
            phy_backend=backend,
            harq=HarqController(max_attempts=2, feedback_delay_slots=1),
            recovery=FastArqPolicy(failure_indication_delay_slots=1),
            scheduled_rescue=ScheduledRescueConfig(
                ScheduledRescueMode.HARQ_PRESERVING, 1, 2),
        ).run()
        rescue_by_slot = {}
        for event in events(result, "phy_transmission"):
            if event["transmission_access"] == "scheduled_rescue":
                rescue_by_slot.setdefault(event["slot"], []).append(event)
        self.assertTrue(rescue_by_slot)
        for transmissions in rescue_by_slot.values():
            self.assertLessEqual(len(transmissions), 2)
            self.assertEqual(len({event["resource_id"] for event in transmissions}),
                             len(transmissions))
            self.assertTrue(all(event["resource_semantics"] ==
                                "non_contention_based" for event in transmissions))
        self.assertEqual(result["resources"]["scheduled_rescue_resource_units"],
                         sum(len(value) for value in rescue_by_slot.values()))
        self.assertLessEqual(result["resources"]["scheduled_rescue_resource_units"],
                             result["resources"]["resource_units_available"])

    def test_deterministic_replay_and_deadline_metric(self):
        first, _ = run_rescue(ScheduledRescueMode.FRESH_TB)
        second, _ = run_rescue(ScheduledRescueMode.FRESH_TB)
        self.assertEqual(first, second)
        self.assertEqual(json.dumps(first, sort_keys=True),
                         json.dumps(second, sort_keys=True))
        self.assertEqual(first["latency_slots"]["deadline_success"]["4"]
                         ["ratio_of_arrivals"], 0.0)
        self.assertEqual(first["latency_slots"]["deadline_success"]["8"]
                         ["ratio_of_arrivals"], 1.0)

    def test_v03c_v03d_v03e_artifacts_are_unchanged(self):
        expected = {
            "configs/access_recovery_factorial.yaml": "68af44fd697bd3a873dd90416d2c07b8baa4c774d6cd15a2c310018da77b91fa",
            "docs/ACCESS_RECOVERY_FACTORIAL.md": "507247e7fefd545307b02c0337a8475f026a54d3b958a776f85043e1a19006e8",
            "results/access_recovery_factorial.md": "c44b316271676fc9a14f2a8762e41fc8c19ccec76e325c4c2a71bb0c58acd9e3",
            "configs/traffic_sensitivity.yaml": "ddcfa764b7bb305dcae3045c099e023be0da71eb63c9291daeb96915c919b1e0",
            "docs/TRAFFIC_SENSITIVITY.md": "88058cc88559b8597561bc5f4a6e8c71590ecf15250291f5d09c29d241ca5fef",
            "results/traffic_sensitivity.md": "09533e1746cee76079373af419207c84ba5c49107daba77f1b425dc86274a55b",
            "configs/traffic_revalidation_v0_3e.yaml": "689f9eb305ed0d417cafe3544e65b50e752df4a0559b0638c5657ddf84cf29db",
            "docs/TRAFFIC_REVALIDATION_V0_3E.md": "87b5d999d4b625a256efb6f3b4230b61d80a96a84e4534274e92baace03bd594",
            "results/traffic_revalidation_v0_3e.md": "4610a2c1b8da2e2aed5f59160bffe4daa5a98b12c2af1894919184bde20c334f",
        }
        for filename, digest in expected.items():
            with self.subTest(filename=filename):
                self.assertEqual(hashlib.sha256(Path(filename).read_bytes()).hexdigest(),
                                 digest)


if __name__ == "__main__":
    unittest.main()
