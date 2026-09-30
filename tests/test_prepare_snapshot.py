"""Fail-closed checks for preparing frozen SLS sources without running SLS."""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/release"))
import prepare_snapshot as prep  # noqa: E402


class PrepareSnapshotTests(unittest.TestCase):
    def make_fixture(self, temporary: Path) -> Path:
        """Copy public metadata/source and only raw manifests to an isolated root."""
        root = temporary / "package"
        (root / "provenance").mkdir(parents=True)
        (root / "scratch/sls").mkdir(parents=True)
        for name in ("public_id_map.json", "source_sha_crosswalk.csv"):
            shutil.copy2(ROOT / "provenance" / name, root / "provenance" / name)
        shutil.copytree(ROOT / "snapshots", root / "snapshots")
        shutil.copytree(ROOT / "configs", root / "configs")
        for original in (ROOT / "data/raw").glob("*/*/seed_*/run_manifest.json"):
            target = root / original.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, target)
        return root

    def test_all_six_mapped_campaigns_prepare_clean_snapshots_and_17_links(self) -> None:
        mapped = prep.load_public_map(ROOT)
        self.assertEqual(len(mapped), 6)
        self.assertEqual(sum(len(item["scenarios"]) for item in mapped.values()), 17)
        with tempfile.TemporaryDirectory(prefix=".prepare-test-", dir=ROOT / "scratch/sls") as raw:
            container = Path(raw)
            for campaign, spec in mapped.items():
                with self.subTest(campaign=campaign):
                    result = prep.prepare(campaign, destination=container / campaign)
                    self.assertEqual(result.execution_dir, container / campaign)
                    self.assertEqual(result.output_root,
                                     container / campaign / "generated" / campaign)
                    self.assertEqual(len(result.original_implementation_sha), 40)
                    self.assertEqual(len(result.local_reproduction_sha), 40)
                    self.assertNotEqual(result.original_implementation_sha,
                                        result.local_reproduction_sha)
                    self.assertFalse((result.execution_dir / "source_manifest.json").exists())
                    status = subprocess.check_output(
                        ["git", "-C", str(result.execution_dir), "status", "--porcelain"],
                        text=True).strip()
                    self.assertEqual(status, "")
                    links = list((result.execution_dir / "results").glob("raw_*/*"))
                    self.assertEqual(len(links), 17)
                    for public, mapped_spec in mapped.items():
                        for scenario, historical in mapped_spec["scenarios"].items():
                            link = (result.execution_dir / "results" /
                                    f"raw_{mapped_spec['legacy_id']}" / historical)
                            self.assertTrue(link.is_symlink())
                            self.assertEqual(link.resolve(),
                                             ROOT / "data/raw" / public / scenario)

    def test_reproduction_validation_root_prepares_under_requested_scratch_path(self) -> None:
        validation_root = ROOT / "scratch/reproduction-validation"
        validation_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".prepare-validation-test-",
                                         dir=validation_root) as raw:
            destination = Path(raw) / "high_load_baselines"
            result = prep.prepare("high_load_baselines", destination=destination)
            self.assertEqual(result.execution_dir, destination)
            self.assertEqual(result.output_root,
                             destination / "generated/high_load_baselines")
            self.assertTrue(result.execution_dir.is_relative_to(validation_root))
            self.assertFalse(result.output_root.exists())
            self.assertEqual(
                subprocess.check_output(
                    ["git", "-C", str(destination), "status", "--porcelain"],
                    text=True,
                ).strip(), "",
            )

    def test_unknown_campaign_fails_without_preparing(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".prepare-test-", dir=ROOT / "scratch/sls") as raw:
            destination = Path(raw) / "unknown"
            with self.assertRaisesRegex(RuntimeError, "unknown public campaign ID"):
                prep.prepare("unknown", destination=destination)
            self.assertFalse(destination.exists())
            output = io.StringIO()
            error = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
                status = prep.main(["unknown", "--destination", str(destination)])
            self.assertEqual(status, 1)
            self.assertNotIn("PREPARE-SNAPSHOT-PASS", output.getvalue())
            self.assertIn("PREPARE-SNAPSHOT-FAIL", error.getvalue())

    def test_corrupted_source_file_or_manifest_fails_before_final_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="prepare-fixture-") as raw:
            root = self.make_fixture(Path(raw))
            destination = root / "scratch/sls/high_load_baselines"
            source = root / "snapshots/high_load_baselines/pyproject.toml"
            source.write_bytes(source.read_bytes() + b"\n# changed\n")
            with self.assertRaisesRegex(RuntimeError, "source file hash/size mismatch"):
                prep.prepare("high_load_baselines", root=root)
            self.assertFalse(destination.exists())
            self.assertEqual(list(destination.parent.glob(".high_load_baselines.staging-*")), [])
            shutil.copy2(ROOT / "snapshots/high_load_baselines/pyproject.toml", source)
            manifest = root / "snapshots/high_load_baselines/source_manifest.json"
            manifest.write_bytes(manifest.read_bytes() + b"\n")
            with self.assertRaisesRegex(RuntimeError, "source manifest hash mismatch"):
                prep.prepare("high_load_baselines", root=root)
            self.assertFalse(destination.exists())

    def test_historical_ids_cannot_escape_staged_link_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="prepare-fixture-") as raw:
            root = self.make_fixture(Path(raw))
            map_path = root / "provenance/public_id_map.json"
            original = json.loads(map_path.read_text(encoding="utf-8"))
            for kind in ("campaign", "scenario"):
                with self.subTest(kind=kind):
                    changed = json.loads(json.dumps(original))
                    if kind == "campaign":
                        changed["campaigns"][0]["legacy_id"] = "../../escape"
                    else:
                        changed["campaigns"][0]["scenarios"][0]["legacy_id"] = "../escape"
                    map_path.write_text(json.dumps(changed), encoding="utf-8")
                    with self.assertRaisesRegex(RuntimeError, "invalid or duplicate"):
                        prep.prepare("high_load_baselines", root=root)
                    self.assertFalse((root / "scratch/sls/high_load_baselines").exists())
                    self.assertFalse((root / "escape").exists())

    def test_symlinked_raw_scenario_is_not_accepted_as_mapped_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="prepare-fixture-") as raw:
            root = self.make_fixture(Path(raw))
            campaign = root / "data/raw/high_load_baselines"
            linked = campaign / "fast_cb"
            shutil.rmtree(linked)
            linked.symlink_to(campaign / "legacy_poll_20_slots", target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, "linked child directory"):
                prep.prepare("high_load_baselines", root=root)
            self.assertFalse((root / "scratch/sls/high_load_baselines").exists())

    def test_missing_raw_input_fails_before_final_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="prepare-fixture-") as raw:
            root = self.make_fixture(Path(raw))
            missing = root / "data/raw/high_load_baselines/fast_cb/seed_9101/run_manifest.json"
            missing.unlink()
            with self.assertRaisesRegex(RuntimeError, "missing or linked raw run manifest"):
                prep.prepare("high_load_baselines", root=root)
            self.assertFalse((root / "scratch/sls/high_load_baselines").exists())

    def test_existing_destination_directory_and_symlink_are_not_replaced(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".prepare-test-", dir=ROOT / "scratch/sls") as raw:
            container = Path(raw)
            occupied = container / "occupied"
            occupied.mkdir()
            sentinel = occupied / "keep.txt"
            sentinel.write_text("preserve", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "already exists"):
                prep.prepare("high_load_baselines", destination=occupied)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")
            linked = container / "linked"
            linked.symlink_to(occupied, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, "already exists"):
                prep.prepare("high_load_baselines", destination=linked)
            self.assertTrue(linked.is_symlink())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")

    def test_late_failure_cleans_temporary_tree(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".prepare-test-", dir=ROOT / "scratch/sls") as raw:
            container = Path(raw)
            final = container / "high_load_baselines"
            with mock.patch.object(prep, "rename_without_replace",
                                   side_effect=RuntimeError("injected publish failure")):
                with self.assertRaisesRegex(RuntimeError, "injected publish failure"):
                    prep.prepare("high_load_baselines", destination=final)
            self.assertFalse(final.exists())
            self.assertEqual(list(container.glob(".high_load_baselines.staging-*")), [])

    def test_atomic_move_refuses_a_destination_created_during_preparation(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".prepare-test-", dir=ROOT / "scratch/sls") as raw:
            container = Path(raw)
            final = container / "high_load_baselines"
            original = prep.rename_without_replace

            def occupy_then_move(source: Path, destination: Path) -> None:
                destination.mkdir()
                (destination / "keep.txt").write_text("preserve", encoding="utf-8")
                original(source, destination)

            with mock.patch.object(prep, "rename_without_replace", side_effect=occupy_then_move):
                with self.assertRaises(FileExistsError):
                    prep.prepare("high_load_baselines", destination=final)
            self.assertEqual((final / "keep.txt").read_text(encoding="utf-8"), "preserve")
            self.assertEqual(list(container.glob(".high_load_baselines.staging-*")), [])

    def test_unsafe_execution_and_output_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".prepare-test-", dir=ROOT / "scratch/sls") as raw:
            container = Path(raw)
            bad_destinations = [
                ROOT / "data/raw/new_execution",
                ROOT / "data/accepted_analysis/new_execution",
                ROOT / "data/figure_inputs/new_execution",
                ROOT / "figures/new_execution",
                ROOT / "provenance/new_execution",
                ROOT / "snapshots/new_execution",
            ]
            for destination in bad_destinations:
                with self.subTest(destination=destination):
                    with self.assertRaisesRegex(RuntimeError, "unsafe execution directory"):
                        prep.prepare("high_load_baselines", destination=destination)
                    self.assertFalse(destination.exists())
            final = container / "high_load_baselines"
            bad_outputs = [Path("results/raw_v0_5"), Path("../other"),
                           ROOT / "data/raw", ROOT / "figures",
                           Path("generated/../../data/raw")]
            for output in bad_outputs:
                with self.subTest(output=output):
                    with self.assertRaisesRegex(RuntimeError, "unsafe output root"):
                        prep.prepare("high_load_baselines", destination=final,
                                     output_root=output)
                    self.assertFalse(final.exists())
            self.assertEqual(list(container.glob(".high_load_baselines.staging-*")), [])


if __name__ == "__main__":
    unittest.main()
