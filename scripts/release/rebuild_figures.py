"""Regenerate the three paper figures and verify their numerical sources.

The included figure PDFs stay in figures/. The source CSVs must match
byte-for-byte, and the figure code checks accepted numerical anchors.
Generated PDFs are checked for presence; rendering bytes are not a gate.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
STEMS = {
    "fig3a": "fig3a_recovery_domain_tail",
    "fig4a": "fig4a_delay_scheduled_recovery_admission",
    "fig5a": "fig5a_high_load_packet_delivery_vs_latency",
}
EXPECTED_SOURCE_SHA256 = {
    "fig3a": "a3ab6f642a4190da86552fdc3b594913aaac561a7a991376ea07ae1b9d907c1a",
    "fig4a": "0b1313f4e85d3a2a371a176452b7d112e4dede86ded95fb213f621e88b6443a0",
    "fig5a": "340450895014653eb988b63d9430c58c09bad01a4acfa88c9b97f7edb723cb13",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require_sha256(path: Path, expected: str) -> None:
    actual = sha256(path)
    if actual != expected:
        raise RuntimeError(f"reference SHA-256 mismatch: {path}: {actual} != {expected}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", choices=STEMS)
    parser.add_argument("--output", type=Path, default=ROOT / "figures/rebuilt")
    args = parser.parse_args()
    selected = [args.only] if args.only else list(STEMS)
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        raise RuntimeError(f"output directory must be empty: {output}")
    for name in selected:
        stem = STEMS[name]
        require_sha256(
            ROOT / "data/figure_inputs/accepted_figure_sources" / f"{stem}_source.csv",
            EXPECTED_SOURCE_SHA256[name],
        )
        figure_pdf = ROOT / "figures" / f"{stem}.pdf"
        if not figure_pdf.is_file() or figure_pdf.stat().st_size == 0:
            raise RuntimeError(f"included figure PDF is missing or empty: {figure_pdf}")
    command = [sys.executable, str(ROOT / "scripts/paper_figures/build_all.py"),
               "--output", str(output)]
    if args.only:
        command.extend(("--only", args.only))
    with TemporaryDirectory(prefix="commmag-figure-font-cache-") as cache:
        environment = dict(os.environ)
        environment["MPLCONFIGDIR"] = cache
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        subprocess.run(command, cwd=ROOT, env=environment, check=True)
    generation = json.loads((output / "generation_manifest.json").read_text(encoding="utf-8"))
    if generation["last_built"] != selected or generation["accepted_number_consistency_checks"] != "PASS":
        raise RuntimeError("figure-generation manifest does not match the requested set")
    comparisons = {}
    for name in selected:
        stem = STEMS[name]
        generated_source = output / f"{stem}_source.csv"
        require_sha256(generated_source, EXPECTED_SOURCE_SHA256[name])
        generated_pdf = output / f"{stem}.pdf"
        if not generated_pdf.is_file() or generated_pdf.stat().st_size == 0:
            raise RuntimeError(f"generated figure PDF is missing or empty: {generated_pdf}")
        generated_meta = json.loads((output / f"{stem}_metadata.json").read_text(encoding="utf-8"))
        accepted_meta = json.loads((
            ROOT / "data/figure_inputs/accepted_figure_sources" / f"{stem}_metadata.json"
        ).read_text(encoding="utf-8"))
        comparisons[name] = {
            "figure_pdf": str((ROOT / "figures" / f"{stem}.pdf").relative_to(ROOT)),
            "included_figure_pdf_present": True,
            "generated_figure_pdf_present": True,
            "accepted_source_csv_sha256": EXPECTED_SOURCE_SHA256[name],
            "rebuilt_source_csv_sha256": sha256(generated_source),
            "source_csv_byte_identical": True,
            "accepted_metadata_selected_font": accepted_meta["selected_font"],
            "rebuilt_metadata_selected_font": generated_meta["selected_font"],
        }
    receipt = {
        "classification": "ONE-FIGURE-NUMERICAL-REBUILD-PASS" if args.only else "THREE-FIGURE-NUMERICAL-REBUILD-PASS",
        "selected": selected,
        "comparisons": comparisons,
        "full_sls_rerun": False,
    }
    (output / "figure_validation.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(receipt["classification"])
    print(output)


if __name__ == "__main__":
    main()
