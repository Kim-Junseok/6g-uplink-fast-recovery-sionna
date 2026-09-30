"""Integration checks for the three paper figure identities (no SLS)."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]
REBUILD = ROOT / "scripts/release/rebuild_figures.py"
STEMS = {
    "fig3a": "fig3a_recovery_domain_tail",
    "fig4a": "fig4a_delay_scheduled_recovery_admission",
    "fig5a": "fig5a_high_load_packet_delivery_vs_latency",
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PublicFigureTests(unittest.TestCase):
    def run_rebuild(self, output: Path, *options: str) -> dict:
        command = [sys.executable, str(REBUILD), "--output", str(output), *options]
        completed = subprocess.run(command, cwd=ROOT, text=True,
                                   capture_output=True, timeout=120, check=True)
        self.assertIn("NUMERICAL-REBUILD-PASS", completed.stdout)
        return json.loads((output / "figure_validation.json").read_text(encoding="utf-8"))

    def test_full_set_is_exactly_the_three_paper_figures(self) -> None:
        with TemporaryDirectory(prefix="commmag-three-figure-test-") as temporary:
            output = Path(temporary) / "figures"
            receipt = self.run_rebuild(output)
            self.assertEqual(receipt["classification"], "THREE-FIGURE-NUMERICAL-REBUILD-PASS")
            self.assertEqual(receipt["selected"], list(STEMS))
            self.assertEqual({path.name for path in output.glob("*.pdf")},
                             {f"{stem}.pdf" for stem in STEMS.values()})
            for name, stem in STEMS.items():
                public_source = (ROOT / "data/figure_inputs/accepted_figure_sources"
                                 / f"{stem}_source.csv")
                self.assertEqual(digest(output / f"{stem}_source.csv"), digest(public_source))
                self.assertTrue((ROOT / "figures" / f"{stem}.pdf").is_file())
                comparison = receipt["comparisons"][name]
                self.assertTrue(comparison["source_csv_byte_identical"])
                self.assertTrue(comparison["included_figure_pdf_present"])
                self.assertTrue(comparison["generated_figure_pdf_present"])
                self.assertFalse(any("pdf" in key and "sha256" in key
                                     for key in comparison))

    def test_single_selection_does_not_emit_former_panels(self) -> None:
        with TemporaryDirectory(prefix="commmag-one-figure-test-") as temporary:
            output = Path(temporary) / "figures"
            receipt = self.run_rebuild(output, "--only", "fig4a")
            self.assertEqual(receipt["classification"], "ONE-FIGURE-NUMERICAL-REBUILD-PASS")
            self.assertEqual(receipt["selected"], ["fig4a"])
            self.assertEqual({path.name for path in output.glob("*.pdf")},
                             {f"{STEMS['fig4a']}.pdf"})


if __name__ == "__main__":
    unittest.main()
