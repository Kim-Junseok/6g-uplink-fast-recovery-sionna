"""Deterministic V0.3c factorial-design and measurement tests."""

import json
import unittest

from ul_access.protocol import (GrantFreeAccess, Packet, ScheduledAccess,
    SimulationParameters, SlotSimulator, aggregate_paired_effects,
    derive_stream_seed, seed_effects)
from ul_access.phy import PhyBackend, PhyOutcome
from ul_access.recovery import FastArqPolicy, HarqController, LegacyArqPolicy


class TwoUeTraffic:
    def arrivals(self, slot):
        return tuple(Packet(slot * 2 + ue, ue, slot, 24) for ue in range(2)) if slot < 2 else ()


class OutcomePhy(PhyBackend):
    def __init__(self, success=False): self.success = success
    @property
    def num_ues(self): return 2
    @property
    def num_subcarriers(self): return 1
    def evaluate(self, allocation, *, tx_power_dbm, mcs_index, realization_seed):
        del allocation, tx_power_dbm, mcs_index, realization_seed
        feedback = (int(self.success), int(self.success))
        return PhyOutcome((0.0, 0.0), (0.5, 0.5), feedback,
            tuple(24 if x else 0 for x in feedback), ((0.0,), (0.0,)), (1.0, 1.0), (0.5, 0.5))


def run(access, recovery, success=False):
    return SlotSimulator(parameters=SimulationParameters(2, 2, 19, (0.0, 0.0), (10, 10), max_drain_slots=8),
        traffic=TwoUeTraffic(), access=access, phy_backend=OutcomePhy(success),
        harq=HarqController(max_attempts=2, feedback_delay_slots=1), recovery=recovery,
        max_rlc_retransmissions=1).run()


def scheduled():
    return ScheduledAccess(resource_pool_size=1, request_period_slots=1, grant_delay_slots=0,
        max_retries=0, retry_delay_slots=0)


def grant_free():
    return GrantFreeAccess(resource_pool_size=1, opportunity_period_slots=1,
        max_retries=0, retry_backoff_slots=0)


class FactorialTest(unittest.TestCase):
    def test_rng_addresses_do_not_alias_across_consecutive_seeds_and_slots(self):
        access_seeds = {
            derive_stream_seed(seed, slot, "access", bits=64)
            for seed in range(9101, 9109)
            for slot in range(550)
        }
        phy_seeds = {
            derive_stream_seed(seed, slot, "phy", bits=32)
            for seed in range(9101, 9109)
            for slot in range(550)
        }
        self.assertEqual(len(access_seeds), 8 * 550)
        self.assertEqual(len(phy_seeds), 8 * 550)
        self.assertNotEqual(
            derive_stream_seed(9101, 1, "access"),
            derive_stream_seed(9102, 0, "access"),
        )
        self.assertNotEqual(
            derive_stream_seed(9101, 0, "access"),
            derive_stream_seed(9101, 0, "phy"),
        )

    def test_matched_budget_harq_and_only_factor_dimensions_change(self):
        treatments = {"A": run(scheduled(), LegacyArqPolicy(recovery_delay_slots=2), True),
            "B": run(grant_free(), LegacyArqPolicy(recovery_delay_slots=2), True),
            "C": run(scheduled(), FastArqPolicy(failure_indication_delay_slots=1), True),
            "D": run(grant_free(), FastArqPolicy(failure_indication_delay_slots=1), True)}
        self.assertEqual(len({r["resources"]["resource_units_available"] for r in treatments.values()}), 1)
        self.assertEqual({(r["recovery_configuration"]["harq_max_attempts"], r["recovery_configuration"]["harq_feedback_delay_slots"]) for r in treatments.values()}, {(2, 1)})
        self.assertEqual(treatments["A"]["recovery_configuration"]["policy"], treatments["B"]["recovery_configuration"]["policy"])
        self.assertEqual(treatments["C"]["recovery_configuration"]["policy"], treatments["D"]["recovery_configuration"]["policy"])

    def test_traffic_decomposition_and_rho_excludes_recovery(self):
        result = run(grant_free(), FastArqPolicy(failure_indication_delay_slots=1))
        traffic = result["measurement_traffic"]
        self.assertEqual(traffic["transmissions"], traffic["initial_tx"] + traffic["harq_retx"] + traffic["rlc_retx_tx"])
        self.assertEqual(result["packets"]["new_packets_generated"], 4)
        self.assertGreater(traffic["harq_retx"], 0)
        self.assertGreater(traffic["rlc_retx_tx"], 0)
        self.assertEqual(traffic["exogenous_packets_per_measurement_slot"], 2.0)
        diagnostics = result["traffic_diagnostics"]
        self.assertEqual(len(diagnostics["series"]["generated_arrivals"]), 2)
        self.assertEqual(set(diagnostics["series"]), set(diagnostics["summaries"]))
        self.assertEqual(diagnostics["summaries"]["generated_arrivals"]["maximum"], 2)

    def test_recovery_attribution_and_overlap_distinct_from_failure(self):
        result = run(grant_free(), FastArqPolicy(failure_indication_delay_slots=1), success=True)
        self.assertGreater(result["resources"]["overlap_measurement"]["events"], 0)
        self.assertEqual(result["measurement_traffic"]["nacks"], 0)
        failed = run(grant_free(), FastArqPolicy(failure_indication_delay_slots=1))
        self.assertTrue(failed["recovery_pressure"]["recovery_originated_phy_transmissions_per_slot"])
        self.assertIsNotNone(failed["resources"]["overlap_measurement"]["fraction_overlap_events_involving_recovery"])

    def test_warmup_cohort_and_recovery_aware_conservation(self):
        result = SlotSimulator(parameters=SimulationParameters(2, 2, 19, (0.0,0.0), (10,10), max_drain_slots=8, warmup_slots=1),
            traffic=TwoUeTraffic(), access=grant_free(), phy_backend=OutcomePhy(False),
            harq=HarqController(max_attempts=1, feedback_delay_slots=1),
            recovery=FastArqPolicy(failure_indication_delay_slots=1), max_rlc_retransmissions=1).run()
        packets = result["packets"]
        self.assertEqual(packets["arrived"], 2)
        self.assertEqual(packets["arrived"], packets["completed"] + packets["dropped"] + packets["pending_at_end"])

    def test_seed_interaction_and_paired_ci(self):
        base = run(grant_free(), FastArqPolicy(failure_indication_delay_slots=1), True)
        treatments = {key: json.loads(json.dumps(base)) for key in "ABCD"}
        for key, value in zip("ABCD", (10.0, 13.0, 8.0, 9.0)):
            treatments[key]["latency_slots"]["mean"] = value
        effects = seed_effects(treatments)
        self.assertEqual(effects["mean_latency_slots"]["interaction_D_minus_B_minus_C_plus_A"], -2.0)
        records = [{"effects": effects}, {"effects": effects}]
        summary = aggregate_paired_effects(records)["mean_latency_slots"]["interaction_D_minus_B_minus_C_plus_A"]
        self.assertEqual(summary["mean"], -2.0)
        self.assertEqual(summary["confidence_interval_95"]["sample_count"], 2)

    def test_overloaded_summary_suppresses_all_machine_readable_cis(self):
        base = run(grant_free(), FastArqPolicy(failure_indication_delay_slots=1), True)
        treatments = {key: json.loads(json.dumps(base)) for key in "ABCD"}
        effects = seed_effects(treatments)
        aggregate = aggregate_paired_effects(
            [{"effects": effects}, {"effects": effects}],
            report_confidence_intervals=False,
        )
        for metric in aggregate.values():
            for effect in metric.values():
                self.assertIsNotNone(effect["mean"])
                self.assertIsNone(effect["confidence_interval_95"])

    def test_deterministic_serializable_factorial_replay(self):
        first = run(grant_free(), FastArqPolicy(failure_indication_delay_slots=1))
        second = run(grant_free(), FastArqPolicy(failure_indication_delay_slots=1))
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))


if __name__ == "__main__": unittest.main()
