#!/usr/bin/env python3
"""Analyze the frozen V0.6 P3 B/H/F campaign at the seed level."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import itertools
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


SEEDS = tuple(range(9101, 9109))
SCENARIOS = {
    "B": "V0_5_RHO090_FAST_SB",
    "H": "V0_6_RHO090_H_HARQ_PRESERVING_SHARED_CAP2",
    "F": "V0_6_RHO090_F_FRESH_HARQ_SHARED_CAP2",
}
CLASSIFICATIONS = (
    "P3-HARQ-PRESERVE-SUPPORTED",
    "P3-EARLY-RESCUE-CONDITIONAL",
    "P3-EARLY-RESCUE-NOT-SUPPORTED",
)
T95_7 = 2.364624251


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--b-raw-root", type=Path,
                        default=Path("results/raw_v0_5"))
    parser.add_argument("--p3-raw-root", type=Path,
                        default=Path("results/raw_v0_6_p3"))
    parser.add_argument("--output", type=Path,
                        default=Path("results/v0_6_p3"))
    parser.add_argument("--config", type=Path,
                        default=Path("configs/p3_bhf_v0_6.yaml"))
    parser.add_argument("--implementation-sha", required=True)
    parser.add_argument("--results-sha", required=True)
    parser.add_argument("--classification", choices=CLASSIFICATIONS,
                        required=True)
    return parser.parse_args()


def read_rows(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def truth(value) -> bool:
    return value is True or str(value).lower() == "true"


def percentile(values, q):
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * q / 100
    low, high = math.floor(position), math.ceil(position)
    return values[low] if low == high else values[low] + (
        position - low) * (values[high] - values[low])


def write_csv(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]),
                                lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def sha256(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_dir(args, scheme, seed):
    root = args.b_raw_root if scheme == "B" else args.p3_raw_root
    return root / SCENARIOS[scheme] / f"seed_{seed}"


def load_run(args, scheme, seed):
    directory = run_dir(args, scheme, seed)
    manifest = json.loads((directory / "run_manifest.json").read_text())
    summary = json.loads((directory / "summary.json").read_text())
    packets = read_rows(directory / "packets.csv.gz")
    attempts = read_rows(directory / "attempts.csv.gz")
    slots = read_rows(directory / "slot_prb.csv.gz")
    hashes = {name: sha256(directory / name)
              for name in manifest["output_sha256"]}
    if hashes != manifest["output_sha256"]:
        raise RuntimeError(f"raw hash mismatch: {scheme}/{seed}")
    if len(packets) != 2500 or summary["packets"]["unfinished_after_drain"]:
        raise RuntimeError(f"incomplete run: {scheme}/{seed}")
    return manifest, summary, packets, attempts, slots


def run_kpis(scheme, seed, summary, packets, attempts, slots):
    correct = [row for row in packets if truth(row["delivered_correctly"])]
    latencies = [float(row["completion_latency_slots"]) for row in correct]
    scheduled = [row for row in attempts if row["access"] == "scheduled_rescue"]
    rescued = {row["payload_id"] for row in scheduled}
    first_scheduled = {}
    for row in scheduled:
        key = row["payload_id"]
        if key not in first_scheduled or int(row["actual_tx_slot"]) < int(
                first_scheduled[key]["actual_tx_slot"]):
            first_scheduled[key] = row
    queue_lengths = [int(row["scheduled_queue_length_before_service"])
                     for row in slots]
    waits = [int(row["scheduled_queue_wait"]) for row in scheduled
             if row["scheduled_queue_wait"] not in (None, "")]
    rlc = [int(row["rlc_retx_count"]) for row in packets]
    residual = [int(row["cb_prbs_available"]) for row in slots]
    llr = [int(row["llr_observation_count"]) for row in scheduled
           if row.get("llr_observation_count") not in (None, "")]
    trigger_delays = [int(row["rescue_trigger_to_tx_delay"]) for row in packets
                      if row.get("rescue_trigger_to_tx_delay") not in (None, "")]
    return {
        "scheme": scheme, "seed": seed,
        "receiver_acceptance_probability": sum(
            truth(row["receiver_accepted"]) for row in packets) / 2500,
        "correct_delivery_probability": len(correct) / 2500,
        "undetected_error_probability": sum(
            truth(row["undetected_error"]) for row in packets) / 2500,
        "terminal_failure_probability": sum(
            row["oracle_terminal_status"] == "protocol_drop" for row in packets) / 2500,
        "mean_latency": statistics.fmean(latencies),
        "median_latency": statistics.median(latencies),
        "p95_latency": percentile(latencies, 95),
        "p99_latency": percentile(latencies, 99),
        "max_latency": max(latencies),
        "phy_attempts_per_packet": len(attempts) / 2500,
        "harq_episodes_per_packet": sum(int(row["num_harq_episodes"])
                                         for row in packets) / 2500,
        "scheduled_attempts_per_packet": len(scheduled) / 2500,
        "scheduled_attempts_per_rescued_packet": (
            len(scheduled) / len(rescued) if rescued else 0.0),
        "first_scheduled_success_probability": (
            sum(truth(row["ack"]) for row in first_scheduled.values()) /
            len(first_scheduled) if first_scheduled else 0.0),
        "fraction_entering_early_rescue": sum(
            truth(row.get("early_rescue_triggered", False)) for row in packets) / 2500,
        "fraction_completing_scheduled_rv3": sum(
            truth(row["ack"]) and int(row["rv"]) == 3 for row in scheduled) /
            2500,
        "fraction_requiring_scheduled_rv1": len({
            row["payload_id"] for row in scheduled if int(row["rv"]) == 1}) / 2500,
        "mean_llr_observation_count": statistics.fmean(llr) if llr else 0.0,
        "mean_rescue_trigger_to_tx_delay": (
            statistics.fmean(trigger_delays) if trigger_delays else 0.0),
        "final_harq_failures_per_packet": summary["phy"][
            "final_harq_failure_events"] / 2500,
        "rlc_retransmissions_per_packet": statistics.fmean(rlc),
        "fraction_reaching_rlc_6": sum(value >= 6 for value in rlc) / 2500,
        "scheduled_attempts": len(scheduled),
        "scheduled_prb_slots": len(scheduled),
        "total_occupied_prb_slots": sum(int(row["total_prbs_occupied"])
                                         for row in slots),
        "mean_scheduled_queue_length": statistics.fmean(queue_lengths),
        "mean_scheduled_wait": statistics.fmean(waits) if waits else 0.0,
        "p95_scheduled_wait": percentile(waits, 95) or 0.0,
        "fraction_at_cap2": sum(
            int(row["scheduled_service_count"]) == 2 for row in slots) / len(slots),
        "mean_residual_cb_prbs": statistics.fmean(residual),
        "runtime_seconds": summary["runtime"]["wall_seconds"],
        "peak_rss_kib": summary["runtime"]["peak_rss_kib"],
    }


def paired_stats(rows, treatment, baseline, metric):
    lookup = {(row["scheme"], int(row["seed"])): float(row[metric]) for row in rows}
    differences = [lookup[(treatment, seed)] - lookup[(baseline, seed)]
                   for seed in SEEDS]
    mean = statistics.fmean(differences)
    sd = statistics.stdev(differences)
    se = sd / math.sqrt(len(differences))
    sign_flip = sum(abs(sum(sign * value for sign, value in zip(signs, differences)) /
                            len(differences)) >= abs(mean) - 1e-15
                    for signs in itertools.product((-1, 1), repeat=len(differences))) / 256
    loo = [statistics.fmean(differences[:i] + differences[i + 1:])
           for i in range(len(differences))]
    return differences, {"comparison": f"{treatment}-{baseline}",
        "metric": metric, "mean_difference": mean, "sample_sd": sd,
        "standard_error": se, "ci95_low": mean - T95_7 * se,
        "ci95_high": mean + T95_7 * se, "exact_sign_flip_p": sign_flip,
        "leave_one_out_min": min(loo), "leave_one_out_max": max(loo),
        "leave_one_out_sign_stable": min(loo) > 0 or max(loo) < 0}


def curve_rows(all_runs, conditional=False):
    maxima = max(max(float(row["completion_latency_slots"])
                     for row in packets if truth(row["delivered_correctly"]))
                 for _, _, packets, _, _ in all_runs.values())
    grid = np.arange(0, int(maxima) + 1)
    output = []
    for scheme in ("B", "H", "F"):
        curves = []
        for seed in SEEDS:
            packets = all_runs[(scheme, seed)][2]
            values = sorted(float(row["completion_latency_slots"])
                            for row in packets if truth(row["delivered_correctly"]))
            denominator = len(values) if conditional else 2500
            curves.append(np.searchsorted(values, grid, side="right") / denominator)
        curves = np.asarray(curves)
        means = curves.mean(axis=0)
        half = T95_7 * curves.std(axis=0, ddof=1) / math.sqrt(8)
        for x, mean, delta in zip(grid, means, half):
            output.append({"scheme": scheme, "latency_slots": int(x),
                           "mean": mean, "ci95_low": max(0, mean - delta),
                           "ci95_high": min(1, mean + delta),
                           "population": ("conditional_correct_delivery" if conditional
                                          else "all_measured_arrivals")})
    return output


def line_figure(rows, path, *, tail=False, conditional=False):
    fig, ax = plt.subplots(figsize=(6.4, 4.0))
    colors = {"B": "#555555", "H": "#0072B2", "F": "#D55E00"}
    for scheme in ("B", "H", "F"):
        group = [row for row in rows if row["scheme"] == scheme]
        x = np.array([row["latency_slots"] for row in group])
        mean = np.array([row["mean"] for row in group])
        low = np.array([row["ci95_low"] for row in group])
        high = np.array([row["ci95_high"] for row in group])
        ax.plot(x, mean, label=scheme, color=colors[scheme])
        ax.fill_between(x, low, high, color=colors[scheme], alpha=.15)
    ax.set_xlabel("Latency (slots)")
    ax.set_ylabel("Conditional ECDF" if conditional else
                  "Correct-delivery probability")
    if tail:
        ax.set_ylim(.85, 1.005)
        candidates = [row["latency_slots"] for row in rows if row["mean"] >= .82]
        if candidates:
            ax.set_xlim(min(candidates), max(row["latency_slots"] for row in rows))
    ax.grid(alpha=.25); ax.legend(); fig.tight_layout()
    for suffix in ("png", "svg", "pdf"):
        fig.savefig(path.with_suffix(f".{suffix}"), dpi=200)
    plt.close(fig)


def bar_figure(kpis, metrics, labels, path, schemes=("B", "H", "F")):
    fig, axes = plt.subplots(1, len(metrics), figsize=(3.1 * len(metrics), 3.6))
    axes = np.atleast_1d(axes)
    for ax, metric, label in zip(axes, metrics, labels):
        values = [[float(row[metric]) for row in kpis if row["scheme"] == scheme]
                  for scheme in schemes]
        means = [statistics.fmean(x) for x in values]
        errors = [T95_7 * statistics.stdev(x) / math.sqrt(8) for x in values]
        colors = {"B": "#555555", "H": "#0072B2", "F": "#D55E00"}
        ax.bar(schemes, means, yerr=errors,
               color=[colors[x] for x in schemes], capsize=3)
        ax.set_title(label); ax.grid(axis="y", alpha=.25)
    fig.tight_layout()
    for suffix in ("png", "svg", "pdf"):
        fig.savefig(path.with_suffix(f".{suffix}"), dpi=200)
    plt.close(fig)


def main():
    args = parse_args(); args.output.mkdir(parents=True, exist_ok=True)
    all_runs = {(scheme, seed): load_run(args, scheme, seed)
                for scheme in ("B", "H", "F") for seed in SEEDS}
    kpis = [run_kpis(scheme, seed, *all_runs[(scheme, seed)][1:])
            for scheme in ("B", "H", "F") for seed in SEEDS]
    write_csv(args.output / "run_level_kpis.csv", kpis)
    metrics = [
        "correct_delivery_probability", "terminal_failure_probability",
        "mean_latency", "p95_latency", "p99_latency",
        "scheduled_attempts_per_packet", "scheduled_prb_slots",
        "mean_scheduled_queue_length", "mean_scheduled_wait",
        "rlc_retransmissions_per_packet", "total_occupied_prb_slots",
    ]
    differences, statistics_rows = [], []
    for treatment, baseline in (("H", "B"), ("H", "F")):
        for metric in metrics:
            values, summary = paired_stats(kpis, treatment, baseline, metric)
            statistics_rows.append(summary)
            differences.extend({"comparison": f"{treatment}-{baseline}",
                                "metric": metric, "seed": seed,
                                "difference": value}
                               for seed, value in zip(SEEDS, values))
    write_csv(args.output / "paired_seed_differences.csv", differences)
    write_csv(args.output / "paired_statistics.csv", statistics_rows)
    tail = [{key: row[key] for key in ("scheme", "seed", "p95_latency",
                                       "p99_latency", "max_latency")}
            for row in kpis]
    write_csv(args.output / "per_seed_tail.csv", tail)
    completion = curve_rows(all_runs)
    conditional = curve_rows(all_runs, conditional=True)
    write_csv(args.output / "completion_curve_source.csv", completion)
    write_csv(args.output / "conditional_ecdf_source.csv", conditional)
    line_figure(completion, args.output / "v0_6_p3_a_completion")
    line_figure(completion, args.output / "v0_6_p3_b_tail_zoom", tail=True)
    line_figure(conditional, args.output / "v0_6_p3_c_conditional_ecdf",
                conditional=True)
    bar_figure(kpis,
        ["scheduled_attempts_per_packet", "mean_scheduled_queue_length",
         "mean_scheduled_wait", "fraction_at_cap2", "scheduled_prb_slots",
         "mean_residual_cb_prbs"],
        ["Scheduled attempts/packet", "Mean queue", "Mean wait",
         "Fraction at Cap2", "Scheduled PRB-slots", "Residual CB PRBs"],
        args.output / "v0_6_p3_d_scheduled_workload")
    bar_figure(kpis,
        ["scheduled_attempts_per_rescued_packet",
         "first_scheduled_success_probability", "mean_scheduled_wait",
         "p99_latency"],
        ["Attempts/rescued packet", "First scheduled success",
         "Scheduled wait", "P99 latency"],
        args.output / "v0_6_p3_e_hf_state", schemes=("H", "F"))
    hist_rows = []
    for scheme in ("B", "H", "F"):
        seed_values = []
        for seed in SEEDS:
            packets = all_runs[(scheme, seed)][2]
            counts = Counter(int(row["rlc_retx_count"]) for row in packets)
            seed_values.append([counts[index] / 2500 for index in range(7)])
        array = np.asarray(seed_values)
        for index in range(7):
            hist_rows.append({"scheme": scheme, "rlc_retx_count": index,
                              "mean_fraction": array[:, index].mean(),
                              "ci95_half_width": T95_7 *
                              array[:, index].std(ddof=1) / math.sqrt(8)})
    write_csv(args.output / "rlc_retry_distribution.csv", hist_rows)
    fig, ax = plt.subplots(figsize=(6.4, 4.0))
    for scheme, marker in zip(("B", "H", "F"), ("o", "s", "^")):
        group = [row for row in hist_rows if row["scheme"] == scheme]
        ax.errorbar([row["rlc_retx_count"] for row in group],
                    [row["mean_fraction"] for row in group],
                    yerr=[row["ci95_half_width"] for row in group],
                    marker=marker, label=scheme, capsize=2)
    ax.set_xlabel("RLC retransmission count"); ax.set_ylabel("Packet fraction")
    ax.set_yscale("log"); ax.grid(alpha=.25); ax.legend(); fig.tight_layout()
    for suffix in ("png", "svg", "pdf"):
        fig.savefig((args.output / "v0_6_p3_f_rlc_chain").with_suffix(
            f".{suffix}"), dpi=200)
    plt.close(fig)
    max_histories = []
    for scheme in ("B", "H", "F"):
        for seed in SEEDS:
            packets = all_runs[(scheme, seed)][2]
            maximum = max((row for row in packets if truth(row["delivered_correctly"])),
                          key=lambda row: float(row["completion_latency_slots"]))
            attempts = [row for row in all_runs[(scheme, seed)][3]
                        if row["payload_id"] == maximum["payload_id"]]
            max_histories.append({"scheme": scheme, "seed": seed,
                "payload_id": maximum["payload_id"],
                "max_latency": maximum["completion_latency_slots"],
                "rlc_retx_count": maximum["rlc_retx_count"],
                "attempt_history_json": json.dumps([{
                    "slot": int(row["actual_tx_slot"]), "rv": int(row["rv"]),
                    "access": row["access"], "episode": row["harq_episode_id"],
                    "ack": truth(row["ack"])} for row in attempts])})
    write_csv(args.output / "max_latency_histories.csv", max_histories)
    means = [{"scheme": scheme, **{metric: statistics.fmean(
        float(row[metric]) for row in kpis if row["scheme"] == scheme)
        for metric in kpis[0] if metric not in {"scheme", "seed"}}}
        for scheme in ("B", "H", "F")]
    write_csv(args.output / "scheme_seed_means.csv", means)
    files = sorted(path for path in args.output.iterdir() if path.is_file())
    manifest = {"execution_classification": "V0_6-P3-COMPLETE",
        "scientific_classification": args.classification,
        "implementation_sha": args.implementation_sha,
        "results_sha": args.results_sha,
        "config_sha256": sha256(args.config), "seeds": list(SEEDS),
        "schemes": {"B": "8 reused V0.5 runs", "H": "8 new runs",
                    "F": "8 new runs"},
        "statistics": "paired seed-level Student-t 95% CI; exact sign-flip; leave-one-out",
        "figure_notes": {"A": "correct-delivery-aware completion with pointwise seed bands",
                         "B": "tail zoom", "C": "conditional ECDF excludes failures",
                         "D": "scheduled-workload mechanism", "E": "H/F state",
                         "F": "RLC recovery chain"},
        "artifact_sha256": {path.name: sha256(path) for path in files}}
    (args.output / "analysis_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"execution": "V0_6-P3-COMPLETE",
                      "classification": args.classification,
                      "output": str(args.output)}, sort_keys=True))


if __name__ == "__main__":
    main()
