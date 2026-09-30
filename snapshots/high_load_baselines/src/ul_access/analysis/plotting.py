"""Generic publication-candidate plotting helpers."""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


def export_figure(fig, stem: Path, metadata: dict) -> list[Path]:
    """Export PDF, SVG, PNG, and deterministic plot metadata."""
    stem.parent.mkdir(parents=True, exist_ok=True)
    outputs = []
    for suffix in ("pdf", "svg", "png"):
        path = stem.with_suffix(f".{suffix}")
        fig.savefig(path, bbox_inches="tight", dpi=220)
        if suffix == "svg":
            lines = path.read_text().splitlines()
            path.write_text("\n".join(line.rstrip() for line in lines) + "\n")
        outputs.append(path)
    metadata_path = stem.with_name(stem.name + "_metadata.json")
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    outputs.append(metadata_path)
    plt.close(fig)
    return outputs


def curve_figure(curves: dict[str, list[dict]], *, title: str, ylabel: str,
                 show_seed_curves: bool = False):
    """Plot arbitrary named curves with pointwise confidence bands."""
    fig, axis = plt.subplots(figsize=(6.8, 4.3))
    curve_panel(axis, curves, title=title, ylabel=ylabel,
                show_seed_curves=show_seed_curves)
    return fig


def curve_panel(axis, curves: dict[str, list[dict]], *, title: str,
                ylabel: str, show_seed_curves: bool = False) -> None:
    """Draw seed-averaged curves on an existing axis."""
    for label, rows in curves.items():
        x = [row["latency_slots"] for row in rows]
        y = [row["seed_mean"] for row in rows]
        lower = [row["pointwise_ci95_lower"] for row in rows]
        upper = [row["pointwise_ci95_upper"] for row in rows]
        line, = axis.plot(x, y, linewidth=2.1, label=label)
        axis.fill_between(x, lower, upper, color=line.get_color(), alpha=0.16)
        if show_seed_curves:
            seed_keys = [key for key in rows[0] if key.startswith("seed_")
                         and key != "seed_count"]
            for key in seed_keys:
                axis.plot(x, [row[key] for row in rows], color=line.get_color(),
                          alpha=0.18, linewidth=0.6)
    axis.set(xlabel="Completion latency (slots)", ylabel=ylabel, title=title,
             ylim=(0.0, 1.01))
    axis.grid(True, alpha=0.25)
    axis.legend(frameon=False)


def two_curve_panel_figure(left: dict[str, list[dict]],
                           right: dict[str, list[dict]], *,
                           left_title: str, right_title: str,
                           left_ylabel: str, right_ylabel: str):
    """Plot two related curve populations without merging their semantics."""
    fig, axes = plt.subplots(1, 2, figsize=(11.4, 4.3))
    curve_panel(axes[0], left, title=left_title, ylabel=left_ylabel)
    curve_panel(axes[1], right, title=right_title, ylabel=right_ylabel)
    fig.tight_layout()
    return fig


def two_metric_bar_figure(labels: list[str], left: dict[str, list[float]],
                          right: dict[str, list[float]], *,
                          left_title: str, right_title: str,
                          left_ylabel: str, right_ylabel: str):
    """Plot two grouped metric summaries with mean and asymmetric intervals."""
    fig, axes = plt.subplots(1, 2, figsize=(10.8, 4.3))
    for axis, values, title, ylabel in (
            (axes[0], left, left_title, left_ylabel),
            (axes[1], right, right_title, right_ylabel)):
        mean, lower, upper = values["mean"], values["lower"], values["upper"]
        errors = [[m - lo for m, lo in zip(mean, lower)],
                  [hi - m for m, hi in zip(mean, upper)]]
        axis.bar(np.arange(len(labels)), mean, yerr=errors, capsize=4,
                 alpha=0.82)
        axis.set_xticks(np.arange(len(labels)), labels, rotation=20, ha="right")
        axis.set(title=title, ylabel=ylabel)
        axis.grid(True, axis="y", alpha=0.25)
    fig.tight_layout()
    return fig


def capacity_distribution_figure(labels: list[str], means: list[float],
                                 lower: list[float], upper: list[float],
                                 distributions: dict[int, list[float]]):
    """Plot mean capacity and its categorical distribution by policy."""
    fig, axes = plt.subplots(1, 2, figsize=(11.2, 4.3))
    x = np.arange(len(labels))
    axes[0].bar(x, means,
                yerr=[[m - lo for m, lo in zip(means, lower)],
                      [hi - m for m, hi in zip(means, upper)]],
                capsize=4, alpha=0.82)
    axes[0].set_xticks(x, labels, rotation=20, ha="right")
    axes[0].set(title="Mean residual CB capacity",
                ylabel="Available CB PRBs per slot", ylim=(0, 6.4))
    bottom = np.zeros(len(labels))
    for prbs in sorted(distributions):
        values = np.asarray(distributions[prbs])
        axes[1].bar(x, values, bottom=bottom, label=str(prbs), alpha=0.82)
        bottom += values
    axes[1].set_xticks(x, labels, rotation=20, ha="right")
    axes[1].set(title="Residual CB-capacity distribution",
                ylabel="Fraction of recorded slots", ylim=(0, 1.01))
    axes[1].legend(title="Available PRBs", frameon=False, ncol=2)
    for axis in axes:
        axis.grid(True, axis="y", alpha=0.25)
    fig.tight_layout()
    return fig


def metric_figure(series: dict[str, tuple[list[float], list[float], list[float], list[float]]],
                  *, xlabel: str, ylabel: str, title: str,
                  ylim: tuple[float, float] | None = None):
    """Plot arbitrary group means and two-sided error bars over an x variable."""
    fig, axis = plt.subplots(figsize=(6.8, 4.3))
    for label, (x, mean, lower, upper) in series.items():
        errors = [[m - lo for m, lo in zip(mean, lower)],
                  [hi - m for m, hi in zip(mean, upper)]]
        axis.errorbar(x, mean, yerr=errors, marker="o", capsize=3,
                      linewidth=1.8, label=label)
    axis.set(xlabel=xlabel, ylabel=ylabel, title=title)
    if ylim is not None:
        axis.set_ylim(*ylim)
    axis.grid(True, alpha=0.25)
    axis.legend(frameon=False)
    return fig


def stacked_component_figure(groups: list[str], x_labels: list[str],
                             components: dict[str, list[list[float]]], *,
                             xlabel: str, ylabel: str, title: str):
    """Plot stacked components for arbitrary groups at common x categories."""
    fig, axis = plt.subplots(figsize=(7.8, 4.6))
    x = np.arange(len(x_labels), dtype=float)
    width = 0.8 / len(groups)
    for group_index, group in enumerate(groups):
        bottom = np.zeros(len(x_labels))
        for component, values_by_group in components.items():
            values = np.asarray(values_by_group[group_index], dtype=float)
            axis.bar(x + (group_index - (len(groups) - 1) / 2) * width, values,
                     width, bottom=bottom, label=(component if group_index == 0 else None),
                     alpha=0.82)
            bottom += values
    axis.set_xticks(x, x_labels, rotation=30, ha="right")
    axis.set(xlabel=xlabel, ylabel=ylabel, title=title)
    axis.grid(True, axis="y", alpha=0.25)
    axis.legend(frameon=False)
    return fig


def paired_panel_figure(rows: list[dict], metrics: list[tuple[str, str]], *, title: str):
    """Plot paired seed differences for arbitrary metrics."""
    fig, axes = plt.subplots(2, 2, figsize=(8.2, 6.3))
    for axis, (metric, label) in zip(axes.flat, metrics):
        selected = [row for row in rows if row["metric"] == metric]
        axis.axhline(0.0, color="black", linewidth=0.8)
        axis.plot([row["seed"] for row in selected],
                  [row["paired_difference"] for row in selected], marker="o")
        axis.set(xlabel="Seed", ylabel="Treatment − baseline", title=label)
        axis.grid(True, alpha=0.25)
    fig.suptitle(title)
    fig.tight_layout()
    return fig


def multi_metric_panel_figure(panels: list[dict], *, columns: int = 3,
                              figsize: tuple[float, float] = (12.0, 6.8)):
    """Plot metric series and optional horizontal references in a panel grid."""
    rows = int(np.ceil(len(panels) / columns))
    fig, axes = plt.subplots(rows, columns, figsize=figsize, squeeze=False)
    for axis, panel in zip(axes.flat, panels):
        for label, values in panel["series"].items():
            x, mean = values["x"], values["mean"]
            lower, upper = values.get("lower"), values.get("upper")
            if lower is None or upper is None:
                axis.plot(x, mean, marker="o", linewidth=1.8, label=label)
            else:
                errors = [[m - lo for m, lo in zip(mean, lower)],
                          [hi - m for m, hi in zip(mean, upper)]]
                axis.errorbar(x, mean, yerr=errors, marker="o", capsize=3,
                              linewidth=1.8, label=label)
        reference_styles = ("--", "-.", ":")
        for index, (label, value) in enumerate(
                panel.get("references", {}).items()):
            axis.axhline(value, color=f"C{index + 1}",
                         linestyle=reference_styles[index % 3],
                         linewidth=1.4, label=label)
        axis.set(title=panel["title"], xlabel=panel.get("xlabel", ""),
                 ylabel=panel.get("ylabel", ""))
        if panel.get("xtick_labels") is not None:
            ticks = panel.get("xticks", x)
            axis.set_xticks(ticks, panel["xtick_labels"], rotation=20,
                            ha="right")
        if panel.get("ylim") is not None:
            axis.set_ylim(*panel["ylim"])
        axis.grid(True, alpha=0.25)
        if panel.get("legend", True):
            axis.legend(frameon=False, fontsize=8)
    for axis in axes.flat[len(panels):]:
        axis.set_visible(False)
    fig.tight_layout()
    return fig
