"""Shared vector exports, metadata, and plotting helpers."""
import csv, json
from pathlib import Path
import matplotlib.pyplot as plt
from matplotlib import font_manager
from config import *
from data_loading import *

def select_times_font() -> str:
    """Return the first installed font from the approved preference order."""
    installed = {entry.name for entry in font_manager.fontManager.ttflist}
    for family in FONT_PREFERENCES:
        if family in installed:
            font_manager.findfont(family, fallback_to_default=False)
            return family
    raise RuntimeError("No approved Times-like font is available")


SELECTED_FONT = select_times_font()


def apply_style() -> None:
    """Apply the common publication typography and line style."""
    plt.rcParams.update({
        "font.family": "serif",
        "font.serif": [SELECTED_FONT],
        "font.size": FONT_SIZES["base"],
        "axes.titlesize": FONT_SIZES["title"],
        "axes.titleweight": "normal",
        "axes.labelsize": FONT_SIZES["axis"],
        "axes.linewidth": SPINE_WIDTH,
        "xtick.labelsize": FONT_SIZES["tick"],
        "ytick.labelsize": FONT_SIZES["tick"],
        "xtick.major.width": 0.8,
        "ytick.major.width": 0.8,
        "legend.fontsize": FONT_SIZES["legend"],
        "lines.linewidth": LINE_WIDTH,
        "lines.markersize": MARKER_SIZE,
        "lines.markeredgewidth": MARKER_EDGE_WIDTH,
        "hatch.linewidth": HATCH_LINE_WIDTH,
        "mathtext.fontset": "stix",
        "pdf.fonttype": 42,
        "ps.fonttype": 42,
        "svg.fonttype": "none",
        "svg.hashsalt": "commmag-paper-figures-v1",
        "figure.facecolor": "white",
        "axes.facecolor": "white",
        "savefig.facecolor": "white",
        "axes.unicode_minus": True,
    })


def make_figure():
    """Create one independent subfigure at final manuscript scale."""
    return plt.subplots(figsize=FIGURE_SIZES["one_column"], constrained_layout=True)


def set_title(ax, title: str) -> None:
    """Apply the temporary descriptive title when the shared switch is on."""
    if SHOW_TITLES:
        ax.set_title(title, pad=5)


def finish_axes(ax, *, horizontal_grid: bool = True) -> None:
    """Apply light axes treatment without a dashboard-style background."""
    for spine in ax.spines.values():
        spine.set_linewidth(SPINE_WIDTH)
    ax.set_axisbelow(True)
    if horizontal_grid:
        ax.grid(axis="y", which="major", color="#D9D9D9", linewidth=0.55)
    else:
        ax.grid(False)


def export_figure(fig, base: Path) -> list[Path]:
    """Export PDF/SVG vector files and a 300-dpi PNG."""
    base.parent.mkdir(parents=True, exist_ok=True)
    paths = []
    for fmt in EXPORT_FORMATS:
        path = base.with_suffix("." + fmt)
        metadata = {
            "pdf": {"Creator": "CommMag paper-figure generator", "CreationDate": None, "ModDate": None},
            "svg": {"Creator": "CommMag paper-figure generator", "Date": None},
            "png": {"Software": "CommMag paper-figure generator"},
        }[fmt]
        fig.savefig(path, dpi=PNG_DPI, metadata=metadata)
        if fmt == "svg":
            path.write_text("\n".join(line.rstrip() for line in path.read_text().splitlines()) + "\n")
        paths.append(path)
    plt.close(fig)
    return paths


apply_style()

def write_csv(path: Path, rows: list[dict]) -> None:
    fields: list[str] = []
    for row in rows:
        fields.extend(key for key in row if key not in fields)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

def common_metadata(stem, role, schemes, loads, definitions, source_paths):
    styles = {scheme: SCHEME_STYLES[scheme] for scheme in schemes
              if scheme in SCHEME_STYLES}
    return {
        "artifact": stem,
        "manuscript_role": role,
        "data_source_files": sources(*source_paths),
        "accepted_result_commits": {
            "V0.5_rebaseline": V05_COMMIT,
            "V0.6_F3_report": V06_COMMIT,
            "V0.7_implementation_freeze": V07_IMPLEMENTATION,
            "V0.7_raw_results": V07_RESULTS,
            "V0.7_final_report": V07_REPORT,
        },
        "schemes": schemes,
        "loads": loads,
        "metric_definitions": definitions,
        "aggregation": "arithmetic mean of eight accepted seed-level estimates",
        "selected_font": SELECTED_FONT,
        "figure_dimensions_inches": list(FIGURE_SIZES["one_column"]),
        "scheme_styles": styles,
        "colors": {scheme: style["color"] for scheme, style in styles.items()},
        "linestyles": {scheme: style["linestyle"] for scheme, style in styles.items()},
        "markers": {scheme: style["marker"] for scheme, style in styles.items()},
        "SHOW_TITLES": SHOW_TITLES,
        "line_width_pt": LINE_WIDTH,
        "marker_size_pt": MARKER_SIZE,
        "spine_width_pt": SPINE_WIDTH,
        "hatch_line_width_pt": HATCH_LINE_WIDTH,
        "generation_command": GENERATION_COMMAND,
        "main_figure_uncertainty": "omitted; accepted paired-seed analysis remains authoritative",
    }

def save_package(output, stem, fig, rows, metadata):
    paths = export_figure(fig, output / stem)
    source_path = output / f"{stem}_source.csv"
    metadata_path = output / f"{stem}_metadata.json"
    write_csv(source_path, rows)
    metadata["source_data_csv"] = source_path.name
    metadata["exports"] = [path.name for path in paths]
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    return paths + [source_path, metadata_path]

