"""V0.4f exact actual-SLS validation and support-accounting tests."""

from __future__ import annotations

import inspect
import json
import math
import unittest
from pathlib import Path

from ul_access.analysis.harq_mi_actual_sls_validation import (
    SequentialHarqAwgn, binomial_metric, deterministic_hash,
    flatten_population, mi_prefix_probabilities,
    noise_variance_from_sinr_db, select_representatives, validate_history,
    wilson)


class ActualSlsValidationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mapping = json.loads(Path(
            "results/harq_mi_validation_v0_4e.json").read_text())
        cls.result = json.loads(Path(
            "results/harq_mi_actual_sls_validation_v0_4f.json").read_text())

    def test_exact_history_extraction_replay_and_rounding_rejection(self):
        row = {"attempt": 2, "physical_attempt": 2, "access": "CB", "rv": 2,
            "effective_sinr_db": 30.0,
            "diagnostic_sinr_history_db": [-2.4080867767333984, 30.0],
            "support_class": "OUTSIDE_SUPPORT", "harq_information_state": {
                "new_bit_information": 82.30548861016308,
                "repeated_bit_information": 0.0,
                "latest_transmission_information": 72.0,
                "unique_coded_bits": 144}}
        artifact = {"points": [{"traffic_scenario": "fixed_q_tight",
            "seed": 9103, "populations": {"F": [row]}}]}
        extracted = flatten_population(artifact, self.mapping)
        self.assertEqual(extracted[0]["diagnostic_sinr_history_db"],
                         row["diagnostic_sinr_history_db"])
        modified = json.loads(json.dumps(artifact))
        modified["points"][0]["populations"]["F"][0]["effective_sinr_db"] = 29.99
        with self.assertRaises(ValueError):
            flatten_population(modified, self.mapping)

    def test_rv_order_and_f_fresh_tb_are_preserved(self):
        support = json.loads(Path("results/harq_mi_support_v0_4e.json").read_text())
        records = flatten_population(support, self.mapping)
        fresh = [row for row in records if row["scheme"] == "F" and
                 row["physical_attempt"] == 3]
        self.assertTrue(fresh)
        self.assertTrue(all(row["attempt"] == 1 and row["rv"] == 0
                            for row in fresh))
        self.assertTrue(all(len(row["diagnostic_sinr_history_db"]) == 1
                            for row in fresh))

    def test_db_to_noise_and_single_eesm_contract(self):
        self.assertAlmostEqual(noise_variance_from_sinr_db(10.0), 0.1)
        self.assertAlmostEqual(noise_variance_from_sinr_db(-10.0), 10.0)
        source = inspect.getsource(SequentialHarqAwgn)
        self.assertNotIn("EESM", source)
        self.assertNotIn("code_rate", source)

    def test_common_input_mi_anchor_is_exact(self):
        history = [-2.4080867767333984, 30.0]
        probabilities = mi_prefix_probabilities(history, self.mapping)
        self.assertEqual(1.0-probabilities[0], 0.9937969209698498)
        self.assertEqual(probabilities[1], 0.9999637479547798)
        self.assertEqual(self.result["regression_anchor"]["history_db"], history)

    def test_sequential_conditioning_and_stage_reach(self):
        for row in self.result["validation_records"]:
            sequential = row["validation"]["sequential"]
            entrants = [stage["entrants"] for stage in sequential]
            self.assertEqual(entrants, sorted(entrants, reverse=True))
            expected = math.prod(1.0-stage["model_success_probability"]
                                 for stage in sequential[:-1])
            self.assertAlmostEqual(
                expected, row["validation"]["model_stage_reach_probability"])

    def test_wilson_boundaries_and_zero_entrants(self):
        self.assertIsNone(wilson(0, 0))
        self.assertIsNone(binomial_metric(0, 0)["estimate"])
        self.assertEqual(binomial_metric(0, 200)["estimate"], 0.0)
        self.assertEqual(binomial_metric(200, 200)["estimate"], 1.0)
        self.assertGreater(wilson(0, 200)[1], 0.0)
        self.assertLess(wilson(200, 200)[0], 1.0)

        class NoLateEntrants:
            rv = (0, 2, 3, 1)

            @staticmethod
            def batch(history_db, batch_size, seed):
                del history_db, seed
                return [batch_size, 0], [0, 0]

        result = validate_history(NoLateEntrants(), [-2.0, 30.0], seed=1,
            sampling={"batch_size": 50, "minimum_initial_samples": 50,
                "minimum_conditioned_samples": 1,
                "wilson_95_max_half_width": 0.05,
                "maximum_initial_samples_by_attempt": {2: 50}},
            mapping=self.mapping, conditional_limit=0.05, reach_limit=0.05)
        self.assertEqual(result["conditioned_entrants"], 0)
        self.assertEqual(result["status"], "SAMPLE-LIMITED")

    def test_selection_uses_actual_rows_and_prioritizes_transition(self):
        def row(index, probability):
            return {"attempt": 1, "physical_attempt": 1, "rv": 0,
                "access": "CB", "diagnostic_sinr_history_db": [float(index)],
                "model_probability": probability, "support_class": "OUTSIDE_SUPPORT",
                "traffic": "t", "scheme": "R0", "seed": index,
                "source_row_index": index}
        records = [row(i, 0.5) for i in range(5)] + [row(10+i, 0.01)
                                                     for i in range(5)]
        selected = select_representatives(records,
            sinr_edges=[-100, 100], probability_edges=[0, 0.2, 0.8, 1.0001],
            representatives_per_cell=1, target_total_coverage=0.5)
        self.assertEqual(len(selected["selected"]), 1)
        self.assertEqual(selected["selected"][0]["model_probability"], 0.5)
        self.assertIn(selected["selected"][0]["source_row_index"],
                      {row["source_row_index"] for row in records})
        repeated = select_representatives(records,
            sinr_edges=[-100, 100], probability_edges=[0, 0.2, 0.8, 1.0001],
            representatives_per_cell=1, target_total_coverage=0.5)
        self.assertEqual(selected, repeated)

    def test_missing_anatomy_mass_and_accounting(self):
        anatomy = self.result["missing_support_anatomy"]
        self.assertEqual(anatomy["total_decisions"], 76190)
        self.assertEqual(anatomy["outside_decisions"], 7649)
        self.assertEqual(anatomy["attempt_2_cb_outside"], 5815)
        self.assertEqual(sum(row["outside_decisions"] for row in anatomy["groups"]),
                         anatomy["outside_decisions"])

    def test_support_extension_gate_mass_and_deterministic_hash(self):
        closure = self.result["support_closure"]
        self.assertLess(closure["empirically_evidenced_coverage"], 0.99)
        deterministic = {key: value for key, value in self.result.items()
                         if key not in {"runtime_seconds", "deterministic_payload_sha256"}}
        self.assertEqual(deterministic_hash(deterministic),
                         self.result["deterministic_payload_sha256"])

    def test_historical_v04e_artifacts_are_immutable(self):
        expected = {
            "results/harq_mi_validation_v0_4e.json":
                "b0cff4a45cd2ca7794183b9e94f9d35bcfa9c2f6d1524ac1bd12ae2019b6b0c5",
            "results/harq_mi_support_v0_4e.json":
                "a5634b52ce4b7b3e8673e6bd66065002a71eeaa7d04a00e2aac69df42e7f55ee"}
        import hashlib
        for path, digest in expected.items():
            self.assertEqual(hashlib.sha256(Path(path).read_bytes()).hexdigest(), digest)


if __name__ == "__main__":
    unittest.main()
