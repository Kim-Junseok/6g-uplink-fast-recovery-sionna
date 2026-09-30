"""Deterministic correctness tests for the V0.3b recovery state machine."""

import json
import unittest
from collections.abc import Sequence

from ul_access.phy import PhyBackend, PhyOutcome
from ul_access.protocol import (GrantFreeAccess, Packet, ScheduledAccess,
                                SimulationParameters, SlotSimulator)
from ul_access.recovery import FastArqPolicy, HarqController, LegacyArqPolicy
from ul_access.resource import ResourceAllocation


class TracePhy(PhyBackend):
    def __init__(self, outcomes: Sequence[bool]):
        self.outcomes = tuple(outcomes); self.calls = 0
    @property
    def num_ues(self): return 1
    @property
    def num_subcarriers(self): return 1
    def evaluate(self, allocation: ResourceAllocation, *, tx_power_dbm,
                 mcs_index, realization_seed):
        del allocation, tx_power_dbm, mcs_index, realization_seed
        success = self.outcomes[min(self.calls, len(self.outcomes)-1)]; self.calls += 1
        return PhyOutcome((10.0,), (0.0,), (1 if success else 0,),
                          (24 if success else 0,), ((10.0,),), (0.001,), (0.0,))


class OnePacket:
    def arrivals(self, slot):
        return (Packet(7, 0, 0, 24),) if slot == 0 else ()


def run(policy, outcomes=(False, False, True), duration=20, max_rlc=1):
    return SlotSimulator(
        parameters=SimulationParameters(duration, 1, 42, (0.0,), (10,)),
        traffic=OnePacket(),
        access=GrantFreeAccess(resource_pool_size=1, opportunity_period_slots=1,
                               max_retries=99, retry_backoff_slots=99),
        phy_backend=TracePhy(outcomes),
        harq=HarqController(max_attempts=2, feedback_delay_slots=1),
        recovery=policy, max_rlc_retransmissions=max_rlc).run()


class RecoveryTest(unittest.TestCase):
    def test_negative_drain_duration_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "max_drain_slots"):
            SlotSimulator(
                parameters=SimulationParameters(
                    1, 1, 42, (0.0,), (10,), max_drain_slots=-1
                ),
                traffic=OnePacket(),
                access=GrantFreeAccess(
                    resource_pool_size=1,
                    opportunity_period_slots=1,
                    max_retries=0,
                    retry_backoff_slots=0,
                ),
                phy_backend=TracePhy((True,)),
            )

    def test_unresolved_feedback_is_censored_from_reliability_rates(self):
        for outcome in (True, False):
            with self.subTest(outcome=outcome):
                result = SlotSimulator(
                    parameters=SimulationParameters(1, 1, 42, (0.0,), (10,)),
                    traffic=OnePacket(),
                    access=GrantFreeAccess(
                        resource_pool_size=1,
                        opportunity_period_slots=1,
                        max_retries=0,
                        retry_backoff_slots=0,
                    ),
                    phy_backend=TracePhy((outcome,)),
                    harq=HarqController(max_attempts=1, feedback_delay_slots=2),
                    recovery=FastArqPolicy(failure_indication_delay_slots=1),
                    max_rlc_retransmissions=0,
                ).run()
                reliability = result["reliability"]
                self.assertEqual(reliability["transmission_attempts"], 1)
                self.assertEqual(reliability["resolved_feedback_attempts"], 0)
                self.assertEqual(reliability["unresolved_feedback_attempts"], 1)
                self.assertIsNone(reliability["transmission_success_probability"])
                self.assertIsNone(reliability["transmission_failure_probability"])
                self.assertEqual(result["packets"]["pending_at_end"], 1)
                self.assertEqual(result["recovery"]["final_harq_failures"], 0)

    def test_all_access_recovery_combinations_are_constructible(self):
        for access in (
            ScheduledAccess(resource_pool_size=1, request_period_slots=1,
                            grant_delay_slots=0, max_retries=0,
                            retry_delay_slots=0),
            GrantFreeAccess(resource_pool_size=1, opportunity_period_slots=1,
                            max_retries=0, retry_backoff_slots=0),
        ):
            for policy in (LegacyArqPolicy(recovery_delay_slots=1),
                           FastArqPolicy(failure_indication_delay_slots=1)):
                with self.subTest(access=type(access).__name__, recovery=policy.name):
                    result = SlotSimulator(
                        parameters=SimulationParameters(6, 1, 4, (0.0,), (10,)),
                        traffic=OnePacket(), access=access,
                        phy_backend=TracePhy((False, True)),
                        harq=HarqController(max_attempts=1, feedback_delay_slots=1),
                        recovery=policy, max_rlc_retransmissions=1).run()
                    self.assertEqual(result["packets"]["completed"], 1)

    def test_harq_then_new_episode_and_access_reentry(self):
        result = run(FastArqPolicy(failure_indication_delay_slots=1))
        tx = [e for e in result["event_trace"] if e["event"] == "phy_transmission"]
        self.assertEqual([(e["slot"], e["harq_attempt"], e["rlc_retransmissions"]) for e in tx],
                         [(0, 1, 0), (1, 2, 0), (3, 1, 1)])
        self.assertEqual(result["recovery"]["harq_retransmissions"], 1)
        self.assertEqual(result["recovery"]["rlc_retransmissions"], 1)
        self.assertEqual(result["packets"]["arrived"], 1)
        self.assertEqual(result["packets"]["completed"], 1)
        components = result["latency_components_slots"]
        self.assertEqual(sum(values[0] for values in components.values()),
                         result["latency_slots"]["samples"][0])

    def test_feedback_delay_and_no_early_arq(self):
        trace = run(FastArqPolicy(failure_indication_delay_slots=1))["event_trace"]
        self.assertEqual([e["slot"] for e in trace if e["event"] == "harq_nack"], [1, 2])
        self.assertEqual([e["slot"] for e in trace if e["event"] == "final_harq_failure"], [2])
        self.assertEqual([e["slot"] for e in trace if "failure_indication" in e["event"]], [2])

    def test_legacy_and_fast_only_change_post_failure_eligibility(self):
        legacy = run(LegacyArqPolicy(recovery_delay_slots=8), duration=20)
        fast = run(FastArqPolicy(failure_indication_delay_slots=1), duration=20)
        def event(result, name): return next(e for e in result["event_trace"] if e["event"] == name)
        self.assertEqual(event(legacy, "final_harq_failure")["slot"], 2)
        self.assertEqual(event(fast, "final_harq_failure")["slot"], 2)
        self.assertEqual(event(legacy, "legacy_arq_recovery_trigger")["eligible_slot"], 10)
        self.assertEqual(event(fast, "fast_arq_failure_indication")["eligible_slot"], 3)
        self.assertEqual(event(legacy, "rlc_retransmission_eligible")["slot"] -
                         event(fast, "rlc_retransmission_eligible")["slot"], 7)

    def test_rlc_limit_drop_conservation_and_separate_counters(self):
        result = run(LegacyArqPolicy(recovery_delay_slots=1), outcomes=(False,),
                     duration=10, max_rlc=1)
        self.assertEqual(result["packets"]["arrived"],
                         result["packets"]["completed"] + result["packets"]["dropped"] + result["packets"]["pending_at_end"])
        self.assertEqual(result["recovery"]["final_harq_failures"], 2)
        self.assertEqual(result["recovery"]["harq_retransmissions"], 2)
        self.assertEqual(result["recovery"]["rlc_retransmissions"], 1)
        self.assertEqual(result["recovery"]["packets_dropped_rlc_limit"], 1)

    def test_deterministic_serializable_replay(self):
        first = run(FastArqPolicy(failure_indication_delay_slots=1))
        second = run(FastArqPolicy(failure_indication_delay_slots=1))
        self.assertEqual(first, second)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))

    def test_nonzero_delay_does_not_retransmit_in_same_slot(self):
        result = run(FastArqPolicy(failure_indication_delay_slots=1))
        final = next(e["slot"] for e in result["event_trace"] if e["event"] == "final_harq_failure")
        eligible = next(e["slot"] for e in result["event_trace"] if e["event"] == "rlc_retransmission_eligible")
        self.assertGreater(eligible, final)

    def test_drain_processes_feedback_arq_timer_and_rlc_retransmission(self):
        result = SlotSimulator(
            parameters=SimulationParameters(1, 1, 42, (0.0,), (10,), max_drain_slots=8),
            traffic=OnePacket(),
            access=GrantFreeAccess(resource_pool_size=1, opportunity_period_slots=1,
                                   max_retries=0, retry_backoff_slots=0),
            phy_backend=TracePhy((False, True)),
            harq=HarqController(max_attempts=1, feedback_delay_slots=2),
            recovery=FastArqPolicy(failure_indication_delay_slots=2),
            max_rlc_retransmissions=1).run()
        self.assertEqual(result["packets"]["completed"], 1)
        self.assertFalse(result["phases"]["drain_timeout_reached"])
        self.assertEqual(result["phases"]["drain_slots_used"], 6)

    def test_slot_diagnostics_include_warmup_cohort_rlc_entry(self):
        result = SlotSimulator(
            parameters=SimulationParameters(6, 1, 42, (0.0,), (10,),
                                            warmup_slots=2),
            traffic=OnePacket(),
            access=GrantFreeAccess(resource_pool_size=1,
                                   opportunity_period_slots=1,
                                   max_retries=0, retry_backoff_slots=0),
            phy_backend=TracePhy((False, True)),
            harq=HarqController(max_attempts=1, feedback_delay_slots=1),
            recovery=FastArqPolicy(failure_indication_delay_slots=2),
            max_rlc_retransmissions=1).run()

        # The packet arrived in warm-up, so cohort metrics exclude it, while
        # the slot series records its RLC entry at measurement slot 3.
        self.assertEqual(result["measurement_traffic"]["rlc_eligible"], 0)
        self.assertEqual(
            result["traffic_diagnostics"]["series"]
                  ["rlc_retransmission_entries"],
            [0, 1, 0, 0],
        )


if __name__ == "__main__": unittest.main()
