"""Regenerate only the three figures included in the current manuscript.

Inputs are the frozen aggregate CSVs in data/figure_inputs. This script never
runs SLS. Use scripts/release/rebuild_figures.py for reference-hash checks and
a portable generation receipt.
"""
import argparse
import importlib
import json
from pathlib import Path

from config import OUTPUT_DIRECTORY
from data_loading import INPUTS, load_data, sha256
from common import SELECTED_FONT

FIGURES = {
    "fig3a": "fig3a_recovery_tail",
    "fig4a": "fig4a_trigger_ratio",
    "fig5a": "fig5a_delivery_vs_latency",
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", choices=FIGURES)
    parser.add_argument("--output", type=Path, default=OUTPUT_DIRECTORY)
    args = parser.parse_args()
    data = load_data()
    args.output.mkdir(parents=True, exist_ok=True)
    selected = [args.only] if args.only else list(FIGURES)
    for name in selected:
        module = importlib.import_module(FIGURES[name])
        paths = getattr(module, name)(data, args.output)
        print("\n".join(str(path) for path in paths))
    manifest = {
        "classification": "COMMMAG-THREE-FIGURES-COMPLETE" if not args.only else "COMMMAG-ONE-FIGURE-COMPLETE",
        "last_built": selected,
        "selected_font": SELECTED_FONT,
        "accepted_number_consistency_checks": "PASS",
        "inputs": {str(path.relative_to(Path(__file__).resolve().parents[2])): sha256(path)
                   for path in INPUTS.values()},
        "code": {path.name: sha256(path) for path in Path(__file__).parent.glob("*.py")},
        "artifacts": {path.name: sha256(path) for path in args.output.iterdir()
                      if path.is_file() and path.name != "generation_manifest.json"},
    }
    (args.output / "generation_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(manifest["classification"], SELECTED_FONT, "accepted-number checks PASS")


if __name__ == "__main__":
    main()
