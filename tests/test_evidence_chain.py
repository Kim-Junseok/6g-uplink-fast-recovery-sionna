"""Focused non-SLS checks for the public numerical evidence chain."""

from __future__ import annotations

import csv
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/release"))
import check_evidence_chain as chain  # noqa: E402
import check_evidence_map as evidence  # noqa: E402
import reanalyze  # noqa: E402


class EvidenceChainTests(unittest.TestCase):
    def test_mapped_campaign_and_scenario_inventory(self) -> None:
        campaigns, scenarios = chain.require_campaign_inventory()
        self.assertEqual(len(campaigns), 6)
        self.assertEqual(scenarios, 17)

    def test_seven_figure_input_links_and_reanalysis_values(self) -> None:
        with TemporaryDirectory(prefix="commmag-link-test-") as temporary:
            generated = Path(temporary) / "reanalysis"
            for campaign, filename in chain.LINKS:
                source = ROOT / "data/accepted_analysis" / campaign / filename
                destination = generated / campaign / filename
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
            links = chain.require_figure_input_links(generated)
            self.assertEqual(len(links), 7)
            self.assertEqual(
                {(Path(item["accepted_csv"]).parent.name, Path(item["accepted_csv"]).name)
                 for item in links},
                set(chain.LINKS),
            )
            damaged = generated / chain.LINKS[0][0] / chain.LINKS[0][1]
            text = damaged.read_text(encoding="utf-8")
            damaged.write_text(text.replace("9101", "9199", 1), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "!=|exceeds"):
                chain.require_figure_input_links(generated)

    def test_existing_reanalysis_numeric_tolerances_are_retained(self) -> None:
        with TemporaryDirectory(prefix="commmag-numeric-test-") as temporary:
            expected = Path(temporary) / "accepted.csv"
            actual = Path(temporary) / "regenerated.csv"
            expected.write_text("metric,value\na,1.0\n", encoding="utf-8")
            actual.write_text("metric,value\na,1.0000000005\n", encoding="utf-8")
            reanalyze.compare_csv(expected, actual)
            actual.write_text("metric,value\na,1.000000002\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "exceeds"):
                reanalyze.compare_csv(expected, actual)
        self.assertEqual(str(reanalyze.ABS_TOL), "1E-9")
        self.assertEqual(str(reanalyze.REL_TOL), "1E-12")

    def test_three_figure_sources_and_pdfs(self) -> None:
        with TemporaryDirectory(prefix="commmag-figure-chain-test-") as temporary:
            output = Path(temporary) / "figures"
            subprocess.run(
                [sys.executable, str(ROOT / "scripts/release/rebuild_figures.py"),
                 "--output", str(output)],
                cwd=ROOT, check=True, capture_output=True, text=True, timeout=120,
            )
            checked = chain.require_figure_sources(output)
            self.assertEqual(set(checked), {"fig3a", "fig4a", "fig5a"})
            changed = output / "fig3a_recovery_domain_tail_source.csv"
            changed.write_text(changed.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "figure source CSV differs"):
                chain.require_figure_sources(output)

    def test_manuscript_claims_include_prose_and_test_d(self) -> None:
        claims = evidence.rows(ROOT / "provenance/manuscript_evidence_map.csv")
        numerical, curve_points = evidence.check_numerical_claims(claims)
        self.assertEqual(numerical, 31)
        self.assertEqual(curve_points, 2927)
        with TemporaryDirectory(prefix="commmag-evidence-test-") as temporary:
            output = Path(temporary) / "evidence.json"
            receipt = chain.require_evidence_map(output)
            self.assertEqual(receipt["claim_rows"], 52)
            self.assertEqual(receipt["numerical_prose_claims_checked"], 31)
            self.assertEqual(receipt["figure_curve_points_checked"], 2927)

    def test_test_d_ci_and_seed_counterexample_fail_closed(self) -> None:
        claims = {
            row["claim_id"]: row
            for row in evidence.rows(ROOT / "provenance/manuscript_evidence_map.csv")
        }
        with TemporaryDirectory(prefix="commmag-test-d-") as temporary:
            accepted = Path(temporary)
            test_d = accepted / "fast_cb_reinjection_backoff"
            test_d.mkdir()
            for name in ("paired_statistics.csv", "paired_seed_differences.csv"):
                shutil.copyfile(
                    ROOT / "data/accepted_analysis/fast_cb_reinjection_backoff" / name,
                    test_d / name,
                )
            self.assertEqual(
                evidence.check_test_d_claims(claims, accepted),
                {"R11", "R12", "R13", "R14"},
            )
            path = test_d / "paired_statistics.csv"
            with path.open(newline="", encoding="utf-8") as stream:
                reader = csv.DictReader(stream)
                fields, rows = reader.fieldnames, list(reader)
            for row in rows:
                if row["metric"] == "packets_unresolved_at_injection_end":
                    row["ci95_upper"] = "0.1"
            with path.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            with self.assertRaisesRegex(RuntimeError, "R13"):
                evidence.check_test_d_claims(claims, accepted)

    def test_resolved_output_path_rejects_distributed_inputs(self) -> None:
        with TemporaryDirectory(prefix="commmag-output-path-test-") as temporary:
            alias = Path(temporary) / "alias"
            alias.symlink_to(ROOT / "data/raw", target_is_directory=True)
            for protected in (
                ROOT / "data/raw/new_receipt.json",
                ROOT / "data/accepted_analysis/new_receipt.json",
                ROOT / "data/figure_inputs/new_receipt.json",
                ROOT / "figures/new_receipt.json",
                ROOT / "provenance/new_receipt.json",
                ROOT / "snapshots/new_receipt.json",
                ROOT / "README.md",
                ROOT / "SHA256SUMS",
                alias / "new_receipt.json",
            ):
                with self.subTest(protected=protected):
                    with self.assertRaisesRegex(RuntimeError, "protected|scratch/"):
                        chain.require_output_path(protected)
            self.assertEqual(
                chain.require_output_path(ROOT / "scratch/valid_receipt.json"),
                ROOT / "scratch/valid_receipt.json",
            )


if __name__ == "__main__":
    unittest.main()
