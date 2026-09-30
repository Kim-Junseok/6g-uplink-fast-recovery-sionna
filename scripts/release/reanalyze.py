#!/usr/bin/env python3
"""Recompute accepted analysis CSVs from archived raw records without running SLS."""

from __future__ import annotations

import argparse
import csv
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data/raw"
ACCEPTED = ROOT / "data/accepted_analysis"
# Frozen internal IDs identify the original analysis contracts. Public IDs name
# the packaged directories and regenerated outputs.
CAMPAIGNS = {
    "v0_5": ("high_load_baselines", 32),
    "v0_6_p3": ("scheduled_recovery_after_two_cb_failures", 16),
    "v0_6_k3": ("harq_continuation_after_three_cb_failures", 8),
    "v0_6_f3": ("fresh_harq_after_three_cb_failures", 8),
    "v0_7_rho070": ("lower_load_policy_comparison", 64),
    "v0_8_test_d": ("fast_cb_reinjection_backoff", 8),
}
CONFIG_CAMPAIGN = {
    "timing_rebaseline_v0_5.yaml": "v0_5",
    "p3_bhf_v0_6.yaml": "v0_6_p3",
    "p3_hk3_v0_6.yaml": "v0_6_k3",
    "p3_fk3_v0_6.yaml": "v0_6_f3",
    "load_robustness_v0_7.yaml": "v0_7_rho070",
    "fastcb_reinjection_backoff_v0_8.yaml": "v0_8_test_d",
}
ABS_TOL, REL_TOL = Decimal("1e-9"), Decimal("1e-12")


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_inputs() -> tuple[int, dict[str, int], list[dict]]:
    with (ROOT / "provenance/accepted_analysis_sha256.csv").open(
        newline="", encoding="utf-8"
    ) as stream:
        copied = list(csv.DictReader(stream))
    for row in copied:
        path = ROOT / row["path"]
        if not path.is_file() or sha(path) != row["sha256"]:
            raise RuntimeError(f"accepted analysis copy changed: {row['path']}")
        if path.stat().st_size != int(row["bytes"]):
            raise RuntimeError(f"accepted analysis size changed: {row['path']}")

    aliases = json.loads((ROOT / "provenance/public_id_map.json").read_text(
        encoding="utf-8"
    ))["campaigns"]
    if len(aliases) != len(CAMPAIGNS) or {
        entry["legacy_id"] for entry in aliases
    } != set(CAMPAIGNS):
        raise RuntimeError("public campaign ID mapping is incomplete")
    counts: dict[str, int] = {}
    scenario_count = 0
    for entry in aliases:
        legacy = entry["legacy_id"]
        public, expected = CAMPAIGNS[legacy]
        scenarios = entry["scenarios"]
        if entry["public_id"] != public or entry["run_count"] != expected:
            raise RuntimeError(f"{legacy}: public campaign mapping changed")
        if len(scenarios) * 8 != expected or len({
            item["legacy_id"] for item in scenarios
        }) != len(scenarios) or len({
            item["public_id"] for item in scenarios
        }) != len(scenarios):
            raise RuntimeError(f"{legacy}: scenario mapping is incomplete")
        raw_root = RAW / public
        actual_scenarios = {path.name for path in raw_root.iterdir() if path.is_dir()}
        expected_scenarios = {item["public_id"] for item in scenarios}
        if actual_scenarios != expected_scenarios:
            raise RuntimeError(f"{legacy}: public scenario directories changed")
        for scenario in scenarios:
            manifests = sorted(
                (raw_root / scenario["public_id"]).glob("seed_*/run_manifest.json")
            )
            if len(manifests) != 8:
                raise RuntimeError(
                    f"{legacy}/{scenario['public_id']}: expected 8 runs; "
                    f"found {len(manifests)}"
                )
            if {int(path.parent.name.removeprefix("seed_")) for path in manifests} != set(
                range(9101, 9109)
            ):
                raise RuntimeError(f"{legacy}: unexpected seed set")
            for manifest_path in manifests:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest["scenario_id"] != scenario["legacy_id"] or manifest["seed"] != int(
                    manifest_path.parent.name.removeprefix("seed_")
                ):
                    raise RuntimeError(f"frozen run identity changed: {manifest_path}")
                for name in (
                    "packets.csv.gz", "attempts.csv.gz", "slot_prb.csv.gz", "summary.json"
                ):
                    if not (manifest_path.parent / name).is_file():
                        raise RuntimeError(f"missing {name} beside {manifest_path}")
        counts[public] = expected
        scenario_count += len(scenarios)
    if scenario_count != 17 or sum(counts.values()) != 136:
        raise RuntimeError("expected 17 scenarios and 136 distinct production runs")
    for legacy, (public, _) in CAMPAIGNS.items():
        receipt = json.loads((ACCEPTED / public / "analysis_manifest.json").read_text())
        expected = receipt.get("config_sha256")
        # The Test D analysis receipt omits this field; the production contract records it.
        if legacy == "v0_8_test_d":
            expected = "d77465ce116833921ec329468f1d8fd49f614ff3a5a04f8c32ef4415915519db"
        if not expected or sha(ROOT / "configs" / f"{public}.yaml") != expected:
            raise RuntimeError(f"{public}: config hash mismatch")
    return len(copied), counts, aliases


def make_legacy_raw_view(output: Path, aliases: list[dict]) -> Path:
    """Expose frozen internal scenario IDs only inside the disposable work area."""
    root = output / "legacy_raw"
    for campaign in aliases:
        historical = root / f"raw_{campaign['legacy_id']}"
        historical.mkdir(parents=True)
        for scenario in campaign["scenarios"]:
            target = (RAW / campaign["public_id"] / scenario["public_id"]).resolve()
            (historical / scenario["legacy_id"]).symlink_to(
                target, target_is_directory=True
            )
    return root


def commands(output: Path, legacy_raw: Path) -> list[tuple[str, list[str]]]:
    def raw(name: str) -> str:
        return str(legacy_raw / name)

    def cfg(name: str) -> str:
        return str(ROOT / "configs" / f"{CAMPAIGNS[CONFIG_CAMPAIGN[name]][0]}.yaml")

    def out(name: str) -> str:
        return str(output / CAMPAIGNS["v0_5" if name == "v0_5_rebaseline" else name][0])

    return [
        ("v0_5_rebaseline", [
            "35_v0_5_rebaseline_analysis.py",
            "--raw-root", raw("raw_v0_5"), "--output", out("v0_5_rebaseline"),
            "--config", cfg("timing_rebaseline_v0_5.yaml"),
            "--implementation-sha", "d66574649168feeb4a48d95784ea7ecf1255babc",
        ]),
        ("v0_6_p3", [
            "38_v0_6_p3_analysis.py",
            "--b-raw-root", raw("raw_v0_5"), "--p3-raw-root", raw("raw_v0_6_p3"),
            "--output", out("v0_6_p3"), "--config", cfg("p3_bhf_v0_6.yaml"),
            "--implementation-sha", "799a08aafabaa24fcf153bb2cdebc3948a2a295a",
            "--results-sha", "f4cb8b35c35c91735b0d08a8c3d2138ae225c34a",
            "--classification", "P3-EARLY-RESCUE-CONDITIONAL",
        ]),
        ("v0_6_k3", [
            "40_v0_6_k3_analysis.py",
            "--b-raw-root", raw("raw_v0_5"), "--h2-raw-root", raw("raw_v0_6_p3"),
            "--k3-raw-root", raw("raw_v0_6_k3"), "--output", out("v0_6_k3"),
            "--config", cfg("p3_hk3_v0_6.yaml"),
            "--implementation-sha", "1c8a9203c7a4a5c642611c9363cb30e00e9cad86",
            "--results-sha", "4e1238c95de99d88c8205cbe128807b0321c24fb",
            "--classification", "K3-EARLY-RESCUE-CONDITIONAL", "--f3-decision", "F3-GO",
        ]),
        ("v0_6_f3", [
            "42_v0_6_f3_analysis.py",
            "--b-raw-root", raw("raw_v0_5"), "--p3-raw-root", raw("raw_v0_6_p3"),
            "--h3-raw-root", raw("raw_v0_6_k3"), "--f3-raw-root", raw("raw_v0_6_f3"),
            "--output", out("v0_6_f3"), "--config", cfg("p3_fk3_v0_6.yaml"),
            "--implementation-sha", "d86d4c6378647b4ee7a929170c0f70b35795fcb0",
            "--results-sha", "4f27c43dbaf5ee1a3089a9284a71ffd9ebb8fcac",
            "--classification", "K3-HARQ-PRESERVATION-SUPPORTED",
            "--rho070-decision", "RHO070-GO",
        ]),
        ("v0_7_rho070", [
            "44_v0_7_load_robustness_analysis.py",
            "--rho070-root", raw("raw_v0_7_rho070"), "--v05-root", raw("raw_v0_5"),
            "--p3-root", raw("raw_v0_6_p3"), "--h3-root", raw("raw_v0_6_k3"),
            "--f3-root", raw("raw_v0_6_f3"), "--output", out("v0_7_rho070"),
            "--config", cfg("load_robustness_v0_7.yaml"),
            "--implementation-sha", "39120e373e8e53e6153e5e8fa69bed510e5fc53c",
            "--results-sha", "c90d1013a7e6e9192ea931bf66b3a6f6a25b1312",
            "--classification", "LOAD-ROBUSTNESS-CONDITIONAL",
            "--load-decision", "LOAD-STUDY-SUFFICIENT",
        ]),
        ("v0_8_test_d", [
            "46_v0_8_fastcb_backoff_analysis.py",
            "--raw-root", raw("raw_v0_8_test_d"),
            "--reference-root", raw("raw_v0_5"), "--output", out("v0_8_test_d"),
        ]),
    ]


def numeric(value: str) -> Decimal | None:
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def compare_csv(expected: Path, actual: Path) -> dict[str, int | float | str]:
    if not actual.is_file():
        raise RuntimeError(f"reanalysis did not generate {actual}")
    with expected.open(newline="", encoding="utf-8") as stream:
        reference = csv.DictReader(stream)
        columns, old_rows = reference.fieldnames, list(reference)
    with actual.open(newline="", encoding="utf-8") as stream:
        rerun = csv.DictReader(stream)
        if rerun.fieldnames != columns:
            raise RuntimeError(f"CSV columns changed: {expected.name}")
        new_rows = list(rerun)
    if len(old_rows) != len(new_rows):
        raise RuntimeError(f"CSV row count changed: {expected.name}")
    maximum = Decimal(0)
    for index, (left, right) in enumerate(zip(old_rows, new_rows, strict=True)):
        for field in columns or []:
            a, b = left[field], right[field]
            if a == b:
                continue
            aa, bb = numeric(a), numeric(b)
            if aa is None or bb is None or not aa.is_finite() or not bb.is_finite():
                raise RuntimeError(f"{expected.name} row {index} {field}: {a!r} != {b!r}")
            delta = abs(aa - bb)
            tolerance = max(ABS_TOL, REL_TOL * max(abs(aa), abs(bb)))
            if delta > tolerance:
                raise RuntimeError(
                    f"{expected.name} row {index} {field}: {a} != {b}; "
                    f"absolute difference {delta} exceeds {tolerance}"
                )
            maximum = max(maximum, delta)
    return {
        "file": expected.name,
        "rows": len(old_rows),
        "columns": len(columns or []),
        "maximum_absolute_numeric_difference": float(maximum),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("scratch/reanalysis"))
    args = parser.parse_args()
    output = args.output.resolve()
    for protected in (RAW, ACCEPTED, ROOT / "configs", ROOT / "snapshots", ROOT / "figures"):
        if output == protected or output.is_relative_to(protected):
            raise RuntimeError(f"refusing to write into protected input: {output}")
    if output.exists() and any(output.iterdir()):
        raise RuntimeError(f"output directory must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    copied_count, raw_counts, aliases = verify_inputs()
    legacy_raw = make_legacy_raw_view(output, aliases)
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(ROOT / "src"),
        "MPLBACKEND": "Agg",
        "MPLCONFIGDIR": str(output / "matplotlib"),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    (output / "matplotlib").mkdir()
    checks = {}
    for legacy_stage, parameters in commands(output, legacy_raw):
        stage = CAMPAIGNS["v0_5" if legacy_stage == "v0_5_rebaseline" else legacy_stage][0]
        script = ROOT / "experiments" / parameters[0]
        command = [sys.executable, str(script), *parameters[1:]]
        result = subprocess.run(
            command, cwd=ROOT, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
        )
        log = output / f"{stage}.log"
        log.write_text(result.stdout, encoding="utf-8")
        if result.returncode:
            raise RuntimeError(f"{stage} failed (exit {result.returncode}); see {log}")
        if legacy_stage == "v0_8_test_d":
            audit = ROOT / "scripts/audits/v0_8_test_d_integrity.py"
            audit_command = [sys.executable, str(audit),
                             "--reference-root", str(legacy_raw / "raw_v0_5"),
                             "--test-root", str(legacy_raw / "raw_v0_8_test_d"),
                             "--output", str(output / stage / "integrity_audit.csv")]
            audit_result = subprocess.run(
                audit_command, cwd=ROOT, env=env, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
            )
            audit_log = output / "fast_cb_reinjection_backoff_integrity.log"
            audit_log.write_text(audit_result.stdout, encoding="utf-8")
            if audit_result.returncode:
                raise RuntimeError(f"Test D integrity audit failed; see {audit_log}")
        comparisons = [
            compare_csv(path, output / stage / path.name)
            for path in sorted((ACCEPTED / stage).glob("*.csv"))
        ]
        if not comparisons:
            raise RuntimeError(f"{stage}: no accepted CSV files")
        checks[stage] = comparisons
        print(f"{stage}: {len(comparisons)} CSV files verified", flush=True)
    total_csv = sum(len(items) for items in checks.values())
    receipt = {
        "classification": "REANALYSIS-PASS",
        "raw_production_runs": raw_counts,
        "raw_production_run_total": sum(raw_counts.values()),
        "accepted_analysis_files_hashed": copied_count,
        "compared_csv_files": total_csv,
        "numeric_absolute_tolerance": str(ABS_TOL),
        "numeric_relative_tolerance": str(REL_TOL),
        "comparisons": checks,
        "scope": "Archived raw records were reanalyzed. No SLS was run. PDF/SVG byte identity is not asserted.",
    }
    receipt_path = output / "reanalysis_receipt.json"
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(f"REANALYSIS-PASS runs=136 csv_files={total_csv} receipt={receipt_path}")


if __name__ == "__main__":
    main()
