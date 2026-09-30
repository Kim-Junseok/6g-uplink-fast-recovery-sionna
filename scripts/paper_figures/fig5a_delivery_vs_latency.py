"""Independent publication panel. Edit labels and selected metrics below."""
import numpy as np
from common import make_figure, set_title, finish_axes, common_metadata, save_package
from config import SCHEME_STYLES
from data_loading import mean, select, V05, V06_F3

X_LABEL = 'Packet latency [ms]'
Y_LABEL = 'Packet delivery probability'
TITLE = 'High-load packet delivery versus latency'
X_LIMITS = (0, 780)
Y_LIMITS = (.55, 1.005)
MARKER_SPACING = .16  # Fraction of the axes diagonal.

def fig5a(data, output):
    curve_sources = {
        "Legacy-20": (data["v05_curve"], "policy", "Legacy-20", "seed_mean", "V0.5"),
        "B": (data["v06_curve"], "scheme", "B", "mean", "V0.6"),
        "H3": (data["v06_curve"], "scheme", "H3", "mean", "V0.6"),
        "F3": (data["v06_curve"], "scheme", "F3", "mean", "V0.6"),
    }
    rows = []
    fig, ax = make_figure()
    for scheme, (table, key, source_scheme, field, stage) in curve_sources.items():
        group = sorted((row for row in table if row[key] == source_scheme),
                       key=lambda row: float(row["latency_slots"]))
        x = np.asarray([float(row["latency_slots"]) for row in group])
        y = np.asarray([float(row[field]) for row in group])
        style = SCHEME_STYLES[scheme]
        ax.plot(x, y, color=style["color"], linestyle=style["linestyle"],
                marker=style["marker"], markevery=MARKER_SPACING, label=style["label"])
        rows.extend({"scheme": scheme, "latency_slots": xv,
                     "packet_delivery_probability": yv,
                     "source_stage": stage} for xv, yv in zip(x, y))
    ax.set_xlim(*X_LIMITS)
    ax.set_ylim(*Y_LIMITS)
    ax.set_yticks([.6, .7, .8, .9, 1.0])
    ax.set_xlabel(X_LABEL)
    ax.set_ylabel(Y_LABEL)
    set_title(ax, TITLE)
    ax.legend(frameon=False, loc="lower right", handlelength=2.5)
    finish_axes(ax)
    metadata = common_metadata(
        "fig5a_high_load_packet_delivery_vs_latency",
        "What does the selected recovery design do at high load?",
        ["Legacy-20", "B", "H3", "F3"], [0.90],
        {"packet_delivery_probability": "Fraction of all measured packets that were successfully delivered by the latency threshold; delivery failures remain in the denominator.",
         "packet_latency_slots": "End-to-end packet latency in slots."},
        (V05 / "v0_5_1_completion_source.csv",
         V06_F3 / "completion_curve_source.csv"),
    )
    metadata["caveat"] = "The y-axis is truncated at 0.55 and explicitly ticked; failure mass remains in the packet delivery denominator."
    return save_package(output, "fig5a_high_load_packet_delivery_vs_latency", fig, rows, metadata)

