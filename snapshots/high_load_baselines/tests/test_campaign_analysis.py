import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import matplotlib.pyplot as plt

from ul_access.analysis import (CampaignDataset, RunArtifact, evaluate_ecdf,
    mean_student_t_ci95, paired_difference_summary, seed_averaged_curve,
    support_coverage)
from ul_access.analysis.plotting import (capacity_distribution_figure,
    export_figure, multi_metric_panel_figure, two_curve_panel_figure,
    two_metric_bar_figure)


class CampaignAnalysisTest(unittest.TestCase):
    def test_existing_harq_support_api_remains_exported(self):
        self.assertTrue(callable(support_coverage))

    def test_all_p2_paired_summaries_expose_ci_intermediates(self):
        path = Path("results/p2_campaign_v0_4g/paper_analysis/paired_metric_statistics.csv")
        with path.open() as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 2 * 3 * 4)
        expected = {"sample_count", "degrees_of_freedom", "mean",
                    "sample_standard_deviation", "standard_error",
                    "t_critical_975", "ci95_lower", "ci95_upper",
                    "ci95_half_width"}
        self.assertTrue(all(expected <= row.keys() for row in rows))
        self.assertTrue(all(row["sample_count"] == "8" for row in rows))

    def test_paired_student_t_fixture_and_sign(self):
        result = paired_difference_summary([3, 5, 7], [4, 7, 10])
        self.assertEqual(result["direction"], "treatment_minus_baseline")
        self.assertEqual(result["differences"], [1.0, 2.0, 3.0])
        self.assertAlmostEqual(result["mean"], 2.0)
        self.assertAlmostEqual(result["sample_standard_deviation"], 1.0)
        self.assertAlmostEqual(result["standard_error"], 1 / 3**0.5)
        self.assertEqual(result["degrees_of_freedom"], 2)
        self.assertEqual(result["t_critical_975"], 4.303)
        self.assertAlmostEqual(result["ci95_half_width"], 4.303 / 3**0.5)

    def test_ecdf_monotonicity_endpoint_and_completion_saturation(self):
        grid = [0, 1, 2, 3]
        conditional = evaluate_ecdf([1, 2], grid)
        completion = evaluate_ecdf([1, 2], grid, denominator=4)
        self.assertEqual(conditional, sorted(conditional))
        self.assertEqual(conditional[-1], 1.0)
        self.assertEqual(completion[-1], 0.5)

    def test_seed_curves_receive_equal_weight(self):
        rows = seed_averaged_curve({1: [1], 2: [100] * 9}, [1])
        self.assertAlmostEqual(rows[0]["seed_mean"], 0.5)
        self.assertEqual(rows[0]["seed_count"], 2)

    def test_legacy_compatibility_and_undetected_accounting(self):
        compatibility = json.loads(Path(
            "results/p2_crc_compatibility_v0_4g.json").read_text())
        self.assertEqual(compatibility["classification"],
                         "P2-CRC-COMPATIBILITY-PASS")
        self.assertEqual(len(compatibility["legacy_runs"]), 48)
        self.assertTrue(all(run["compatibility_backfill"]["undetected_error_count"] == 0
                            for run in compatibility["legacy_runs"]))
        with Path("results/p2_campaign_v0_4g/paper_analysis/canonical_run_table.csv").open() as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 72)
        self.assertEqual(sum(int(row["undetected_errors"]) for row in rows), 6)
        self.assertEqual(sum(int(row["undetected_errors"]) for row in rows
                             if float(row["rho"]) < 0.9), 0)

    def test_plot_export_and_source_curve_fields(self):
        curve = seed_averaged_curve({1: [1, 2], 2: [1, 3]}, [0, 1, 2, 3])
        self.assertIn("pointwise_ci95_lower", curve[0])
        with tempfile.TemporaryDirectory() as raw:
            stem = Path(raw) / "figure"
            fig, axis = plt.subplots(); axis.plot([0, 1], [0, 1])
            outputs = export_figure(fig, stem, {"source_csv": "fixture.csv"})
            self.assertEqual({path.suffix for path in outputs},
                             {".pdf", ".svg", ".png", ".json"})
            self.assertTrue(all(path.is_file() for path in outputs))

    def test_p2_5_plot_helpers_accept_publication_inputs(self):
        curve = seed_averaged_curve({1: [1, 2], 2: [1, 3]}, [0, 1, 2, 3])
        figures = [
            two_curve_panel_figure(
                {"a": curve}, {"a": curve}, left_title="left",
                right_title="right", left_ylabel="probability",
                right_ylabel="conditional ECDF"),
            two_metric_bar_figure(
                ["a"], {"mean": [2.0], "lower": [1.0], "upper": [3.0]},
                {"mean": [1.0], "lower": [0.5], "upper": [1.5]},
                left_title="left", right_title="right",
                left_ylabel="packets", right_ylabel="slots"),
            capacity_distribution_figure(
                ["a"], [4.0], [3.5], [4.5], {0: [0.25], 6: [0.75]}),
        ]
        self.assertTrue(all(len(figure.axes) == 2 for figure in figures))
        for figure in figures:
            plt.close(figure)

    def test_p2_5_analysis_integrity_and_primary_comparison(self):
        root = Path("results/p2_5_resource_diagnostic_v0_4g")
        integrity = json.loads((root / "integrity_audit.json").read_text())
        self.assertEqual(integrity["classification"],
                         "P2_5-ANALYSIS-INTEGRITY-PASS")
        self.assertEqual(integrity["run_count"], 24)
        for key in ("all_output_hashes_match", "all_clean_production",
                    "all_measured_packets_match", "all_implementation_shas_match",
                    "all_config_shas_match", "all_resource_invariants_match"):
            self.assertTrue(integrity[key])
        with (root / "figure_index.csv").open() as stream:
            self.assertEqual(len(list(csv.DictReader(stream))), 5)
        with (root / "paired_statistics.csv").open() as stream:
            rows = list(csv.DictReader(stream))
        primary = next(row for row in rows
                       if row["comparison"] == "B" and
                       row["metric"] == "mean_correct_latency_slots")
        self.assertLess(float(primary["ci95_upper"]), 0.0)

    def test_legacy_timing_plot_helper_and_analysis_artifacts(self):
        figure = multi_metric_panel_figure([{
            "title": "fixture", "xlabel": "x", "ylabel": "y",
            "series": {"series": {"x": [5, 10], "mean": [1, 2],
                                    "lower": [0.5, 1.5],
                                    "upper": [1.5, 2.5]}},
            "references": {"a": 1.0, "b": 2.0},
        }])
        self.assertEqual(len(figure.axes), 3)
        references = {line.get_label(): line.get_linestyle()
                      for line in figure.axes[0].lines
                      if line.get_label() in {"a", "b"}}
        self.assertEqual(set(references), {"a", "b"})
        self.assertNotEqual(references["a"], references["b"])
        plt.close(figure)

        root = Path("results/legacy_timing_sensitivity_v0_4g")
        integrity = json.loads((root / "integrity_audit.json").read_text())
        self.assertEqual(integrity["classification"],
                         "LEGACY-TIMING-ANALYSIS-INTEGRITY-PASS")
        self.assertEqual(integrity["run_count"], 48)
        with (root / "figure_index.csv").open() as stream:
            self.assertEqual(len(list(csv.DictReader(stream))), 3)
        with (root / "paired_statistics.csv").open() as stream:
            rows = list(csv.DictReader(stream))
        primary = next(row for row in rows
                       if row["comparison_group"] == "A" and
                       row["baseline"] == "Legacy-20" and
                       row["treatment"] == "Legacy-15" and
                       row["metric"] == "p95_correct_latency_slots")
        self.assertLess(float(primary["ci95_upper"]), 0.0)

    def test_artifact_reader_does_not_mutate_raw_files(self):
        directory = Path("results/raw/P2_MEDIUM_POISSON_RHO090_LEGACY_CB/seed_9101")
        files = sorted(directory.iterdir())
        before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
        run = RunArtifact("fixture", "arbitrary-scheme", 0.9, 9101, directory)
        dataset = CampaignDataset([run])
        self.assertEqual(dataset.select(schemes=["arbitrary-scheme"]), [run])
        self.assertTrue(run.verify_hashes())
        run.manifest(); run.summary(); run.rows("packets")
        after = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
        self.assertEqual(before, after)

    def test_v0_5_analysis_artifacts_when_campaign_exists(self):
        root = Path("results/v0_5_rebaseline")
        if not root.exists():
            self.skipTest("V0.5 production analysis has not run")
        integrity = json.loads((root / "integrity_audit.json").read_text())
        self.assertEqual(integrity["classification"],
                         "V0_5-ANALYSIS-INTEGRITY-PASS")
        self.assertEqual(integrity["run_count"], 32)
        self.assertTrue(all(value for key, value in integrity.items()
                            if key.startswith("all_")))
        with (root / "figure_index.csv").open() as stream:
            self.assertEqual(len(list(csv.DictReader(stream))), 8)
        with (root / "paired_statistics.csv").open() as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 4 * 18)
        self.assertTrue(all(row["sample_count"] == "8" for row in rows))


if __name__ == "__main__":
    unittest.main()
