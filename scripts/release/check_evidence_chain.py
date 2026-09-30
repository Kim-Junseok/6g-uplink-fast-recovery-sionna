#!/usr/bin/env python3
"""Validate the published raw-record-to-manuscript evidence path without SLS.

The reanalysis and figure builders must have completed into scratch directories.
This command rechecks their numerical outputs, confirms every distributed
figure-input copy, and runs the manuscript evidence-map validator. It does not
assert byte-identical PDF rendering or new-SLS equivalence.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

import reanalyze
import rebuild_figures


ROOT = Path(__file__).resolve().parents[2]
LINKS = (
    ("high_load_baselines", "run_level_kpis.csv"),
    ("high_load_baselines", "v0_5_1_completion_source.csv"),
    ("fresh_harq_after_three_cb_failures", "run_level_kpis.csv"),
    ("fresh_harq_after_three_cb_failures", "completion_curve_source.csv"),
    ("harq_continuation_after_three_cb_failures", "completion_curve_source.csv"),
    ("lower_load_policy_comparison", "run_level_kpis.csv"),
    ("lower_load_policy_comparison", "completion_curve_source.csv"),
)
PROTECTED_OUTPUTS = (
    ROOT / "data",
    ROOT / "figures",
    ROOT / "provenance",
    ROOT / "snapshots",
    ROOT / "configs",
    ROOT / "scripts",
    ROOT / "experiments",
)


def require_output_path(path: Path) -> Path:
    """Write validation receipts only below the real package scratch directory."""
    scratch = ROOT / "scratch"
    if scratch.is_symlink():
        raise RuntimeError("scratch directory must not be a symlink")
    resolved = path.resolve()
    scratch_root = scratch.resolve()
    if any(resolved == root or resolved.is_relative_to(root) for root in PROTECTED_OUTPUTS):
        raise RuntimeError(f"refusing to write into protected distributed input: {resolved}")
    if resolved == scratch_root or not resolved.is_relative_to(scratch_root):
        raise RuntimeError(f"validation receipt must stay under package scratch/: {resolved}")
    return resolved



def require_distributed_integrity() -> str:
    """Run the complete public hash, frozen-source, and raw-manifest gate."""
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts/release/verify_integrity.py")],
        cwd=ROOT, text=True, capture_output=True, check=True,
    )
    expected = (
        "PUBLIC-RAW-INTEGRITY-PASS 136 manifests 752 output files "
        "6 source snapshots 6 frozen configs"
    )
    summary = completed.stdout.strip()
    if summary != expected:
        raise RuntimeError(f"distributed integrity gate returned an unexpected scope: {summary}")
    return summary

def require_campaign_inventory() -> tuple[set[str], int]:
    mapping = json.loads((ROOT / "provenance/public_id_map.json").read_text(encoding="utf-8"))
    campaigns = mapping["campaigns"]
    public_ids = [entry["public_id"] for entry in campaigns]
    if len(campaigns) != 6 or len(set(public_ids)) != 6:
        raise RuntimeError("expected six unique mapped public campaigns")
    scenario_total = 0
    for entry in campaigns:
        public = entry["public_id"]
        scenarios = [item["public_id"] for item in entry["scenarios"]]
        if not scenarios or len(set(scenarios)) != len(scenarios):
            raise RuntimeError(f"{public}: public scenario mapping is incomplete")
        actual = {p.name for p in (ROOT / "data/raw" / public).iterdir() if p.is_dir()}
        if actual != set(scenarios):
            raise RuntimeError(f"{public}: public scenario package differs from the ID map")
        scenario_total += len(scenarios)
    raw_roots = {p.name for p in (ROOT / "data/raw").iterdir() if p.is_dir()}
    analysis_roots = {
        p.name for p in (ROOT / "data/accepted_analysis").iterdir() if p.is_dir()
    }
    if raw_roots != set(public_ids) or analysis_roots != set(public_ids):
        raise RuntimeError("published raw or accepted-analysis campaign set differs from the ID map")
    if scenario_total != 17 or sum(int(e["run_count"]) for e in campaigns) != 136:
        raise RuntimeError("expected 17 mapped scenarios and 136 distinct production runs")
    # This also validates every seed manifest, accepted-file hash, and frozen config.
    copied_count, raw_counts, _ = reanalyze.verify_inputs()
    if set(raw_counts) != set(public_ids) or sum(raw_counts.values()) != 136:
        raise RuntimeError("raw production inventory differs from the public ID map")
    if copied_count < 59:
        raise RuntimeError("accepted-analysis hash inventory is incomplete")
    return set(public_ids), scenario_total


def require_reanalysis(output: Path, campaigns: set[str]) -> tuple[int, float]:
    receipt_path = output / "reanalysis_receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if (receipt.get("classification") != "REANALYSIS-PASS"
            or receipt.get("raw_production_run_total") != 136
            or receipt.get("compared_csv_files") != 59
            or receipt.get("numeric_absolute_tolerance") != str(reanalyze.ABS_TOL)
            or receipt.get("numeric_relative_tolerance") != str(reanalyze.REL_TOL)
            or set(receipt.get("raw_production_runs", {})) != campaigns
            or set(receipt.get("comparisons", {})) != campaigns):
        raise RuntimeError("reanalysis receipt has an unexpected scope or tolerance")
    compared = 0
    maximum = 0.0
    for campaign in sorted(campaigns):
        accepted_paths = sorted((ROOT / "data/accepted_analysis" / campaign).glob("*.csv"))
        receipt_files = receipt["comparisons"][campaign]
        if {p.name for p in accepted_paths} != {item["file"] for item in receipt_files}:
            raise RuntimeError(f"{campaign}: reanalysis CSV set differs from the accepted set")
        for accepted in accepted_paths:
            comparison = reanalyze.compare_csv(
                accepted, output / campaign / accepted.name
            )
            compared += 1
            maximum = max(maximum, comparison["maximum_absolute_numeric_difference"])
    if compared != 59:
        raise RuntimeError(f"expected 59 accepted-analysis CSVs; found {compared}")
    return compared, maximum


def require_figure_input_links(reanalysis: Path) -> list[dict[str, str]]:
    actual_links = {
        (path.parent.name, path.name)
        for path in (ROOT / "data/figure_inputs").glob("*/*.csv")
        if path.parent.name != "accepted_figure_sources"
    }
    if actual_links != set(LINKS):
        raise RuntimeError("published figure-input CSV set differs from the seven mapped links")
    checked = []
    for campaign, filename in LINKS:
        accepted = ROOT / "data/accepted_analysis" / campaign / filename
        published = ROOT / "data/figure_inputs" / campaign / filename
        regenerated = reanalysis / campaign / filename
        if not accepted.is_file() or not published.is_file():
            raise RuntimeError(f"missing accepted/figure-input link: {campaign}/{filename}")
        accepted_hash = reanalyze.sha(accepted)
        if reanalyze.sha(published) != accepted_hash:
            raise RuntimeError(f"figure input differs from accepted CSV: {campaign}/{filename}")
        # Independent numerical link back to the newly regenerated raw-record analysis.
        reanalyze.compare_csv(published, regenerated)
        checked.append({
            "accepted_csv": str(accepted.relative_to(ROOT)),
            "figure_input_csv": str(published.relative_to(ROOT)),
            "sha256": accepted_hash,
            "reanalysis_numeric_agreement": True,
        })
    return checked


def require_figure_sources(output: Path) -> dict[str, dict[str, str]]:
    receipt = json.loads((output / "figure_validation.json").read_text(encoding="utf-8"))
    generation = json.loads((output / "generation_manifest.json").read_text(encoding="utf-8"))
    selected = list(rebuild_figures.STEMS)
    if (receipt.get("classification") != "THREE-FIGURE-NUMERICAL-REBUILD-PASS"
            or receipt.get("selected") != selected
            or generation.get("last_built") != selected
            or generation.get("accepted_number_consistency_checks") != "PASS"
            or set(receipt.get("comparisons", {})) != set(selected)):
        raise RuntimeError("figure rebuild did not validate exactly the three manuscript figures")
    checked = {}
    for selector, stem in rebuild_figures.STEMS.items():
        expected = rebuild_figures.EXPECTED_SOURCE_SHA256[selector]
        accepted_source = (
            ROOT / "data/figure_inputs/accepted_figure_sources" / f"{stem}_source.csv"
        )
        generated_source = output / f"{stem}_source.csv"
        for path in (accepted_source, generated_source):
            if not path.is_file() or rebuild_figures.sha256(path) != expected:
                raise RuntimeError(f"{selector}: figure source CSV differs from the frozen source")
        for path in (ROOT / "figures" / f"{stem}.pdf", output / f"{stem}.pdf"):
            if not path.is_file() or path.stat().st_size == 0:
                raise RuntimeError(f"{selector}: included or regenerated PDF is absent/empty")
        comparison = receipt["comparisons"][selector]
        if (comparison.get("accepted_source_csv_sha256") != expected
                or comparison.get("rebuilt_source_csv_sha256") != expected
                or comparison.get("source_csv_byte_identical") is not True):
            raise RuntimeError(f"{selector}: figure rebuild receipt does not match the actual source")
        checked[selector] = {
            "source_csv_sha256": expected,
            "included_pdf_present": True,
            "regenerated_pdf_present": True,
        }
    return checked


def require_evidence_map(output: Path) -> dict:
    command = [
        sys.executable,
        str(ROOT / "scripts/release/check_evidence_map.py"),
        "--output",
        str(output),
    ]
    subprocess.run(command, cwd=ROOT, check=True)
    receipt = json.loads(output.read_text(encoding="utf-8"))
    if (receipt.get("classification") != "EVIDENCE-MAP-PASS"
            or receipt.get("claim_rows") != 52
            or receipt.get("numerical_prose_claims_checked", 0) < 20):
        raise RuntimeError("manuscript evidence-map validation is incomplete")
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reanalysis", type=Path, default=Path("scratch/reanalysis"))
    parser.add_argument("--figures", type=Path, default=Path("scratch/figures"))
    parser.add_argument(
        "--output", type=Path, default=Path("scratch/evidence_chain_validation.json")
    )
    args = parser.parse_args()
    reanalysis = args.reanalysis.resolve()
    figures = args.figures.resolve()
    output = require_output_path(args.output)
    integrity_summary = require_distributed_integrity()
    campaigns, scenarios = require_campaign_inventory()
    compared, max_difference = require_reanalysis(reanalysis, campaigns)
    links = require_figure_input_links(reanalysis)
    figure_sources = require_figure_sources(figures)
    evidence_path = require_output_path(output.with_name("manuscript_evidence_map_validation.json"))
    evidence = require_evidence_map(evidence_path)
    receipt = {
        "classification": "EVIDENCE-CHAIN-PASS",
        "validation_levels": {
            "level_1_distributed_file_integrity": {
                "status": "SOURCE_AND_RAW_PASS",
                "method": "verify_integrity.py",
                "summary": integrity_summary,
                "package_wide_sha256sums_verified_here": False,
            },
            "level_2_figure_regeneration": {
                "status": "PASS", "method": "supplied rebuild_figures.py output verified",
            },
            "level_3_raw_reanalysis_and_manuscript_evidence": {
                "status": "PASS", "method": "reanalyze.py outputs compared and evidence map recomputed",
            },
            "level_4_full_sls_reproduction": {
                "status": "DOCUMENTED_ONLY", "executed": False,
            },
        },
        "full_sls_rerun": False,
        "mapped_campaigns": len(campaigns),
        "mapped_scenarios": scenarios,
        "raw_production_runs": 136,
        "reanalysis_csv_compared": compared,
        "numeric_absolute_tolerance": str(reanalyze.ABS_TOL),
        "numeric_relative_tolerance": str(reanalyze.REL_TOL),
        "maximum_absolute_numeric_difference": max_difference,
        "figure_input_links": links,
        "figure_sources": figure_sources,
        "manuscript_evidence_map": {
            "claim_rows": evidence["claim_rows"],
            "resolved_source_locators": evidence["resolved_source_locators"],
            "numerical_prose_claims_checked": evidence["numerical_prose_claims_checked"],
            "receipt": str(evidence_path),
        },
        "scope": (
            "Distributed records and regenerated numerical sources agree. "
            "PDF render-byte identity and new-SLS equivalence are not asserted."
        ),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        "EVIDENCE-CHAIN-PASS "
        f"runs=136 csv_files={compared} links={len(links)} figures={len(figure_sources)} "
        f"receipt={output}"
    )


if __name__ == "__main__":
    main()
