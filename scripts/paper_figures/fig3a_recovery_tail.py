"""Independent publication panel. Edit labels and selected metrics below."""
import numpy as np
from common import make_figure, set_title, finish_axes, common_metadata, save_package
from config import SCHEME_STYLES
from data_loading import mean, select, V05, V07

Y_LABEL = 'P99 packet latency [slots]'
TITLE = 'Recovery-domain tail across load'
DELIVERY_ANNOTATION = r"$P_{{\mathrm{{del}}}} = {value:.2f}$"
LOAD_LABELS = [r'$\rho=0.70$', r'$\rho=0.90$']
Y_LIMITS = (0, 325)

def fig3a(data, output):
    schemes = ("Legacy-20", "Fast-CB", "B")
    values, source = {}, []
    for scheme in schemes:
        old = "Fast-SB" if scheme == "B" else scheme
        low = mean(select([row for row in data["v07"] if row["rho"] == "0.70"],
                          "scheme", scheme), "p99_latency")
        high = mean(select(data["v05"], "policy", old),
                    "p99_correct_latency_slots")
        values[scheme] = (low, high)
        source.extend([
            {"rho": "0.70", "scheme": scheme, "conditional_p99_slots": low,
             "source_stage": "V0.7"},
            {"rho": "0.90", "scheme": scheme, "conditional_p99_slots": high,
             "source_stage": "V0.5"},
        ])
    fast_cd = mean(select(data["v05"], "policy", "Fast-CB"),
                   "correct_delivery_probability")
    fig, ax = make_figure()
    x = np.arange(2)
    for scheme in schemes:
        style = SCHEME_STYLES[scheme]
        ax.plot(x, values[scheme], color=style["color"], marker=style["marker"],
                linestyle=style["linestyle"], label=style["label"],
                markerfacecolor="none" if scheme == "Fast-CB" else style["color"])
    ax.annotate(DELIVERY_ANNOTATION.format(value=fast_cd), xy=(1, values["Fast-CB"][1]),
                xytext=(-4, 12), textcoords="offset points", ha="right",
                color=SCHEME_STYLES["Fast-CB"]["color"], fontsize=7.2,
                arrowprops={"arrowstyle": "-", "linewidth": 0.7,
                            "color": SCHEME_STYLES["Fast-CB"]["color"]})
    ax.set_xticks(x, LOAD_LABELS)
    ax.set_ylim(*Y_LIMITS)
    ax.set_ylabel(Y_LABEL)
    set_title(ax, TITLE)
    ax.legend(frameon=False, loc="upper left", handlelength=2.4)
    finish_axes(ax)
    metadata = common_metadata(
        "fig3a_recovery_domain_tail", "WHERE should fast recovery occur?",
        list(schemes), [0.70, 0.90],
        {"conditional_p99_slots": "Per-seed P99 over correctly delivered packets, averaged over eight seeds.",
         "correct_delivery_probability": "Fraction of all measured packets delivered with the correct payload."},
        (V05 / "run_level_kpis.csv", V07 / "run_level_kpis.csv"),
    )
    metadata["caveat"] = (
        "Fast-CB rho=0.90 P99 conditions on correct deliveries; its mean packet delivery probability is 0.7325."
    )
    return save_package(output, "fig3a_recovery_domain_tail", fig, source, metadata)

