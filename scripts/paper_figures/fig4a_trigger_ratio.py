"""Independent publication panel. Edit labels and selected metrics below."""
import numpy as np
from common import make_figure, set_title, finish_axes, common_metadata, save_package
from config import SCHEME_STYLES, BAR_HATCHES
from data_loading import mean, select, V06_F3

Y_LABEL = 'K=3 / K=2 ratio'
TITLE = 'Recovery-trigger timing: K=2 vs K=3'
H_LABEL = 'H3 / H2'
F_LABEL = 'F3 / F2'
METRICS = (
    ("scheduled_attempts_per_packet", "Scheduled\nattempts"),
    ("mean_queue", "Mean\nqueue"),
    ("mean_wait", "Mean\nwait"),
    ("p99_latency", "P99"),
)

def fig4a(data, output):
    metrics = METRICS
    rows = []
    for field, label in metrics:
        values = {scheme: mean(select(data["v06"], "scheme", scheme), field)
                  for scheme in ("H2", "H3", "F2", "F3")}
        rows.append({"metric": field, "display_label": label.replace("\n", " "),
                     "H3_mean": values["H3"], "H2_mean": values["H2"],
                     "H3_over_H2": values["H3"]/values["H2"],
                     "F3_mean": values["F3"], "F2_mean": values["F2"],
                     "F3_over_F2": values["F3"]/values["F2"]})
    fig, ax = make_figure()
    x = np.arange(len(metrics)); width = .34
    ax.bar(x-width/2, [row["H3_over_H2"] for row in rows], width,
           color=SCHEME_STYLES["H3"]["color"], label=H_LABEL,
           edgecolor="#444444", linewidth=.45, hatch=BAR_HATCHES["primary"])
    ax.bar(x+width/2, [row["F3_over_F2"] for row in rows], width,
           color=SCHEME_STYLES["F3"]["color"], label=F_LABEL,
           edgecolor="#444444", linewidth=.45, hatch=BAR_HATCHES["primary"])
    ax.axhline(1.0, color="#555555", linewidth=.8, linestyle="--")
    ax.set_xticks(x, [label for _, label in metrics])
    ax.set_ylim(0, 1.08)
    ax.set_ylabel(Y_LABEL)
    set_title(ax, TITLE)
    ax.legend(frameon=False, loc="upper right", bbox_to_anchor=(.98, .92),
              ncol=2, handlelength=1.5, borderaxespad=0)
    finish_axes(ax)
    metadata = common_metadata(
        "fig4a_delay_scheduled_recovery_admission", "WHEN should scheduled recovery occur?",
        ["H2", "H3", "F2", "F3"], [0.90],
        {field: f"Accepted rho=0.90 seed-mean {label.replace(chr(10), ' ').lower()}."
         for field, label in metrics},
        (V06_F3 / "run_level_kpis.csv",),
    )
    metadata["ratio_definition"] = "ratio of the two accepted eight-seed arithmetic means; values below one favor K=3"
    metadata["bar_hatches"] = {
        "H3/H2": BAR_HATCHES["primary"],
        "F3/F2": BAR_HATCHES["primary"],
    }
    return save_package(output, "fig4a_delay_scheduled_recovery_admission", fig, rows, metadata)

