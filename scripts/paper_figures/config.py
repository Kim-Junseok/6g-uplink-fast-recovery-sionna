"""Editable publication typography, colors, output paths, and dimensions."""
from pathlib import Path

SHOW_TITLES = False
SUBFIG_WIDTH = 3.35
SUBFIG_HEIGHT = 2.45
SUBFIG_SIZE = (SUBFIG_WIDTH, SUBFIG_HEIGHT)

FONT_PREFERENCES = (
    "Times New Roman",
    "Times",
    "Nimbus Roman",
    "STIXGeneral",
)

SCHEME_STYLES = {
    "Legacy-20": {
        "color": "#666666", "marker": "o", "linestyle": "--",
        "label": "Legacy-20",
    },
    "Fast-CB": {
        "color": "#111111", "marker": "s", "linestyle": "-.",
        "label": "CB FARQ",
    },
    "B": {
        "color": "#D62728", "marker": "D", "linestyle": "-",
        "label": "SB FARQ",
    },
    "H3": {
        "color": "#0072B2", "marker": "^",
        "linestyle": (0, (5, 1, 1, 1)),
        "label": "H3",
    },
    "F3": {
        "color": "#CC0099", "marker": "v", "linestyle": ":",
        "label": "F3",
    },
    "H2": {
        "color": "#0072B2", "marker": "^", "linestyle": "-",
        "label": "H2",
    },
    "F2": {
        "color": "#CC0099", "marker": "v", "linestyle": "-",
        "label": "F2",
    },
}

CATEGORY_COLORS = {
    "correct_delivery": "#C8C8C8",
    "terminal_failure": "#F2F2F2",
}

BAR_HATCHES = {"primary": "", "secondary": "///", "K2": "", "K3": "///"}



ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIRECTORY = ROOT / "figures/rebuilt"
EXPORT_FORMATS = ("pdf", "svg", "png")
FIGURE_SIZES = {"one_column": SUBFIG_SIZE, "two_column": (7.0, 2.45)}
FONT_SIZES = {"base": 7.5, "title": 9.2, "axis": 8.2, "tick": 7.2, "legend": 7.2}
LINE_WIDTH = 1.5
MARKER_SIZE = 4.5
RATIO_COLOR = "#B0B0B0"

PNG_DPI = 300

SPINE_WIDTH = 0.8
MARKER_EDGE_WIDTH = 0.7
HATCH_LINE_WIDTH = 0.45
