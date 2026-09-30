#!/usr/bin/env python3
"""Analyze V0.7 rho=0.70 and compare it descriptively with rho=0.90."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import itertools
import json
import math
import statistics
from collections import Counter
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


SEEDS = tuple(range(9101, 9109))
T95_7 = 2.364624251
SCENARIOS_070 = {
    "Legacy-20": "V0_7_RHO070_LEGACY_RLC_SB_T20",
    "Legacy-15": "V0_7_RHO070_LEGACY_RLC_SB_T15",
    "Fast-CB": "V0_7_RHO070_FAST_CB", "B": "V0_7_RHO070_FAST_SB",
    "H2": "V0_7_RHO070_H2_HARQ_PRESERVING_SHARED_CAP2",
    "F2": "V0_7_RHO070_F2_FRESH_HARQ_SHARED_CAP2",
    "H3": "V0_7_RHO070_H3_HARQ_PRESERVING_SHARED_CAP2",
    "F3": "V0_7_RHO070_F3_FRESH_HARQ_SHARED_CAP2",
}
SCENARIOS_090 = {
    "Legacy-20": ("v05", "V0_5_RHO090_LEGACY_RLC_SB_T20"),
    "Legacy-15": ("v05", "V0_5_RHO090_LEGACY_RLC_SB_T15"),
    "Fast-CB": ("v05", "V0_5_RHO090_FAST_CB"),
    "B": ("v05", "V0_5_RHO090_FAST_SB"),
    "H2": ("p3", "V0_6_RHO090_H_HARQ_PRESERVING_SHARED_CAP2"),
    "F2": ("p3", "V0_6_RHO090_F_FRESH_HARQ_SHARED_CAP2"),
    "H3": ("h3", "V0_6_RHO090_HK3_HARQ_PRESERVING_SHARED_CAP2"),
    "F3": ("f3", "V0_6_RHO090_FK3_FRESH_HARQ_SHARED_CAP2"),
}
COMPARISONS = (
    ("B", "Legacy-20"), ("B", "Legacy-15"), ("B", "Fast-CB"),
    ("H2", "B"), ("H3", "B"), ("H3", "H2"),
    ("H2", "F2"), ("H3", "F3"), ("F3", "F2"),
)
PRIMARY_METRICS = (
    "correct_delivery_probability", "terminal_failure_probability",
    "mean_latency", "p95_latency", "p99_latency",
    "scheduled_attempts_per_packet", "mean_queue", "mean_wait",
    "scheduled_prb_slots", "total_occupied_prb_slots",
    "rlc_retransmissions_per_packet",
)
MECHANISM_METRICS = (
    "first_scheduled_success_probability",
    "scheduled_attempts_per_rescued_packet", "rescue_admission_rate",
    "eligible_stage_population", "rescue_trigger_to_first_tx_mean",
    "phy_attempts_per_packet", "harq_episodes_per_packet",
    "final_harq_failure_probability", "cb_attempts_per_slot",
    "cb_overlap_probability", "scheduled_attempts_per_slot", "p95_queue",
    "max_queue", "p95_wait", "fraction_at_cap2", "mean_residual_cb_prbs",
    "cb_prb_slots",
)
STAT_METRICS = PRIMARY_METRICS + MECHANISM_METRICS


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rho070-root", type=Path,
                        default=Path("results/raw_v0_7_rho070"))
    parser.add_argument("--v05-root", type=Path, default=Path("results/raw_v0_5"))
    parser.add_argument("--p3-root", type=Path, default=Path("results/raw_v0_6_p3"))
    parser.add_argument("--h3-root", type=Path, default=Path("results/raw_v0_6_k3"))
    parser.add_argument("--f3-root", type=Path, default=Path("results/raw_v0_6_f3"))
    parser.add_argument("--config", type=Path,
                        default=Path("configs/load_robustness_v0_7.yaml"))
    parser.add_argument("--implementation-sha", required=True)
    parser.add_argument("--results-sha", required=True)
    parser.add_argument("--classification", required=True, choices=(
        "LOAD-ROBUSTNESS-SUPPORTED", "LOAD-ROBUSTNESS-CONDITIONAL",
        "LOAD-ROBUSTNESS-NOT-SUPPORTED"))
    parser.add_argument("--load-decision", required=True, choices=(
        "LOAD-STUDY-SUFFICIENT", "RHO050-GO"))
    parser.add_argument("--output", type=Path,
                        default=Path("results/v0_7_rho070"))
    return parser.parse_args()


def rows(path):
    with gzip.open(path, "rt", encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def truth(value):
    return value is True or str(value).lower() == "true"


def present(value):
    return value not in (None, "")


def percentile(values, q):
    values = sorted(values)
    position = (len(values)-1)*q/100
    low, high = math.floor(position), math.ceil(position)
    return values[low] if low == high else values[low] + (position-low)*(
        values[high]-values[low])


def write_csv(path, data):
    if not data:
        path.write_text("")
        return
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(data[0]),
                                lineterminator="\n")
        writer.writeheader()
        writer.writerows(data)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def directory(args, rho, scheme, seed):
    if rho == "0.70":
        return args.rho070_root/SCENARIOS_070[scheme]/f"seed_{seed}"
    key, scenario = SCENARIOS_090[scheme]
    root = {"v05": args.v05_root, "p3": args.p3_root,
            "h3": args.h3_root, "f3": args.f3_root}[key]
    return root/scenario/f"seed_{seed}"


def load(args, rho, scheme, seed):
    path = directory(args, rho, scheme, seed)
    manifest = json.loads((path/"run_manifest.json").read_text())
    summary = json.loads((path/"summary.json").read_text())
    packets, attempts = rows(path/"packets.csv.gz"), rows(path/"attempts.csv.gz")
    slots = rows(path/"slot_prb.csv.gz")
    hashes = {name: sha256(path/name) for name in manifest["output_sha256"]}
    if (hashes != manifest["output_sha256"] or len(packets) != 2500 or
            summary["packets"]["unfinished_after_drain"]):
        raise RuntimeError(f"invalid input run: rho={rho}/{scheme}/{seed}")
    if rho == "0.70" and (manifest["git_sha"] != args.implementation_sha or
                          manifest["dirty_worktree"]):
        raise RuntimeError(f"V0.7 provenance mismatch: {scheme}/{seed}")
    return manifest, summary, packets, attempts, slots


def run_kpis(rho, scheme, seed, summary, packets, attempts, slots):
    correct = [row for row in packets if truth(row["delivered_correctly"])]
    latency = [float(row["completion_latency_slots"]) for row in correct]
    scheduled = [row for row in attempts if row["access"] == "scheduled_rescue"]
    cb = [row for row in attempts if row["access"] == "grant_free"]
    rescued = {row["payload_id"] for row in scheduled}
    first = {}
    scheduled_counts = Counter(row["payload_id"] for row in scheduled)
    for row in scheduled:
        if (row["payload_id"] not in first or int(row["actual_tx_slot"]) <
                int(first[row["payload_id"]]["actual_tx_slot"])):
            first[row["payload_id"]] = row
    queue = [int(row["scheduled_queue_length_before_service"]) for row in slots]
    waits = [int(row["scheduled_queue_wait"]) for row in scheduled
             if present(row.get("scheduled_queue_wait"))]
    residual = [int(row["cb_prbs_available"]) for row in slots]
    rlc = [int(row["rlc_retx_count"]) for row in packets]
    k = 2 if scheme in {"H2", "F2"} else 3
    eligible = set()
    if scheme in {"H2", "F2", "H3", "F3"}:
        target_rv = 2 if k == 2 else 3
        eligible = {row["payload_id"] for row in cb
                    if int(row["rv"]) == target_rv and not truth(row["ack"])}
    early = [row for row in packets if truth(row.get("early_rescue_triggered"))]
    overlap = sum(int(row["cb_occupancy_2"]) + int(row["cb_occupancy_3plus"])
                  for row in slots)
    occupied_cb = sum(int(row["cb_occupancy_1"]) + int(row["cb_occupancy_2"]) +
                      int(row["cb_occupancy_3plus"]) for row in slots)
    opportunities = sum(int(row["cb_prbs_available"]) for row in slots)
    first_success = sum(truth(row["ack"]) for row in first.values())
    record = {
        "rho": rho, "scheme": scheme, "seed": seed,
        "correct_packet_count": len(correct),
        "receiver_acceptance_probability": sum(truth(r["receiver_accepted"])
                                                 for r in packets)/2500,
        "correct_delivery_probability": len(correct)/2500,
        "undetected_error_probability": sum(truth(r["undetected_error"])
                                              for r in packets)/2500,
        "terminal_failure_probability": sum(truth(r.get("terminal_failure")) or
            r.get("oracle_terminal_status") == "protocol_drop" for r in packets)/2500,
        "mean_latency": statistics.fmean(latency),
        "median_latency": statistics.median(latency),
        "p95_latency": percentile(latency, 95),
        "p99_latency": percentile(latency, 99), "max_latency": max(latency),
        "phy_attempts_per_packet": len(attempts)/2500,
        "harq_episodes_per_packet": statistics.fmean(int(r.get(
            "harq_episode_count") or r["num_harq_episodes"]) for r in packets),
        "rlc_retransmissions_per_packet": statistics.fmean(rlc),
        "final_harq_failure_probability": sum(present(r.get(
            "initial_harq_final_failure_slot")) for r in packets)/2500,
        "terminal_failure_after_retry_exhaustion": sum(
            int(r["rlc_retx_count"]) == 6 and
            (truth(r.get("terminal_failure")) or
             r.get("oracle_terminal_status") == "protocol_drop")
            for r in packets)/2500,
        "cb_attempts_per_slot": len(cb)/len(slots),
        "cb_overlap_probability": overlap/occupied_cb if occupied_cb else 0.0,
        "cb_occupancy_0_probability": sum(int(r["cb_occupancy_0"])
            for r in slots)/opportunities,
        "cb_occupancy_1_probability": sum(int(r["cb_occupancy_1"])
            for r in slots)/opportunities,
        "cb_occupancy_2_probability": sum(int(r["cb_occupancy_2"])
            for r in slots)/opportunities,
        "cb_occupancy_3plus_probability": sum(int(r["cb_occupancy_3plus"])
            for r in slots)/opportunities,
        "cb_prb_slots": occupied_cb,
        "scheduled_attempts_per_slot": len(scheduled)/len(slots),
        "scheduled_attempts_per_packet": len(scheduled)/2500,
        "scheduled_attempts_per_rescued_packet": len(scheduled)/len(rescued)
            if rescued else 0.0,
        "scheduled_prb_slots": len(scheduled),
        "mean_queue": statistics.fmean(queue), "p95_queue": percentile(queue, 95),
        "max_queue": max(queue), "mean_wait": statistics.fmean(waits) if waits else 0.0,
        "p95_wait": percentile(waits, 95) if waits else 0.0,
        "fraction_at_cap2": sum(int(r["scheduled_service_count"]) == 2
                                  for r in slots)/len(slots),
        "mean_residual_cb_prbs": statistics.fmean(residual),
        "total_occupied_prb_slots": sum(int(r["total_prbs_occupied"])
                                         for r in slots),
        "rescued_packet_count": len(rescued),
        "eligible_stage_population": len(eligible),
        "rescue_admission_rate": len(early)/len(eligible) if eligible else 0.0,
        "first_scheduled_success_probability": first_success/len(first)
            if first else 0.0,
        "rescue_trigger_to_first_tx_mean": statistics.fmean(int(r[
            "rescue_trigger_to_tx_delay"]) for r in early) if early else 0.0,
        "runtime_seconds": summary["runtime"]["wall_seconds"],
        "peak_rss_kib": summary["runtime"]["peak_rss_kib"],
    }
    return record


def rlc_rows(rho, scheme, seed, packets):
    counts = Counter(int(row["rlc_retx_count"]) for row in packets)
    return [{"rho": rho, "scheme": scheme, "seed": seed,
             "rlc_retransmissions": count, "packet_count": counts[count],
             "probability": counts[count]/2500} for count in range(7)]


def state_rows(rho, scheme, seed, packets, attempts):
    if scheme not in {"H2", "F2", "H3", "F3"}:
        return []
    by_payload = {}
    for row in attempts:
        by_payload.setdefault(row["payload_id"], []).append(row)
    output = []
    for packet in packets:
        if not truth(packet.get("early_rescue_triggered")):
            continue
        event = sorted(by_payload[packet["payload_id"]],
                       key=lambda row: int(row["actual_tx_slot"]))
        first = next(row for row in event if row["access"] == "scheduled_rescue")
        cb = [row for row in event if row["access"] == "grant_free"]
        output.append({"rho": rho, "scheme": scheme, "seed": seed,
            "payload_id": packet["payload_id"],
            "retained_rv_history": json.dumps([int(row["rv"]) for row in cb]),
            "scheduled_first_rv": int(first["rv"]),
            "scheduled_first_llr_observation_count": int(
                first.get("llr_observation_count") or 0),
            "old_episode_terminated": scheme.startswith("F"),
            "fresh_rv0": scheme.startswith("F") and int(first["rv"]) == 0,
            "old_llr_absent": scheme.startswith("F") and int(
                first.get("llr_observation_count") or 0) == 1})
    return output


def paired(table, rho, treatment, baseline, metric):
    lookup = {(r["rho"], r["scheme"], int(r["seed"])): float(r[metric])
              for r in table}
    values = [lookup[(rho, treatment, seed)] - lookup[(rho, baseline, seed)]
              for seed in SEEDS]
    mean = statistics.fmean(values)
    sd = statistics.stdev(values)
    se = sd/math.sqrt(8)
    observed = abs(mean)
    pvalue = sum(abs(statistics.fmean(sign*x for sign, x in zip(signs, values)))
                 >= observed-1e-15
                 for signs in itertools.product((-1, 1), repeat=8))/256
    loo = [statistics.fmean(values[:i] + values[i+1:]) for i in range(8)]
    name = f"{treatment}-{baseline}"
    detail = [{"rho": rho, "comparison": name, "metric": metric,
               "seed": seed, "difference": value}
              for seed, value in zip(SEEDS, values)]
    summary = {"rho": rho, "comparison": name, "metric": metric,
        "mean_difference": mean, "sample_sd": sd, "standard_error": se,
        "ci95_low": mean-T95_7*se, "ci95_high": mean+T95_7*se,
        "exact_sign_flip_p": pvalue, "leave_one_out_min": min(loo),
        "leave_one_out_max": max(loo),
        "leave_one_out_sign_stable": min(loo) > 0 or max(loo) < 0}
    return detail, summary


def cross_load(table):
    lookup = {(r["rho"], r["scheme"], int(r["seed"])): r for r in table}
    output = []
    for scheme in SCENARIOS_070:
        for seed in SEEDS:
            low, high = lookup[("0.70", scheme, seed)], lookup[("0.90", scheme, seed)]
            for metric in STAT_METRICS:
                x, y = float(low[metric]), float(high[metric])
                output.append({"scheme": scheme, "seed": seed, "metric": metric,
                    "rho070": x, "rho090": y, "difference_070_minus_090": x-y,
                    "relative_change": (x-y)/abs(y) if y else ""})
    return output


def summarize(table):
    excluded = {"rho", "scheme", "seed"}
    metrics = [key for key in table[0] if key not in excluded]
    output = []
    for rho in ("0.70", "0.90"):
        for scheme in SCENARIOS_070:
            group = [row for row in table if row["rho"] == rho and
                     row["scheme"] == scheme]
            for metric in metrics:
                values = [float(row[metric]) for row in group]
                mean = statistics.fmean(values)
                half = T95_7*statistics.stdev(values)/math.sqrt(8)
                output.append({"rho": rho, "scheme": scheme, "metric": metric,
                    "mean": mean, "sample_sd": statistics.stdev(values),
                    "ci95_low": mean-half, "ci95_high": mean+half})
    return output


def summarize_cross_load(data):
    output = []
    for scheme in SCENARIOS_070:
        for metric in STAT_METRICS:
            group = [row for row in data if row["scheme"] == scheme and
                     row["metric"] == metric]
            differences = [float(row["difference_070_minus_090"]) for row in group]
            relative = [float(row["relative_change"]) for row in group
                        if row["relative_change"] != ""]
            output.append({"scheme": scheme, "metric": metric,
                "mean_rho070": statistics.fmean(float(row["rho070"]) for row in group),
                "mean_rho090": statistics.fmean(float(row["rho090"]) for row in group),
                "mean_difference_070_minus_090": statistics.fmean(differences),
                "mean_relative_change": statistics.fmean(relative) if relative else "",
                "minimum_seed_difference": min(differences),
                "maximum_seed_difference": max(differences)})
    return output


def curves(all_runs, conditional=False):
    maximum = int(max(float(row["completion_latency_slots"])
        for run in all_runs.values() for row in run[2]
        if truth(row["delivered_correctly"])))
    grid = np.arange(maximum+1)
    output = []
    for rho in ("0.70", "0.90"):
        for scheme in SCENARIOS_070:
            seed_curves = []
            for seed in SEEDS:
                packets = all_runs[(rho, scheme, seed)][2]
                latency = sorted(float(r["completion_latency_slots"]) for r in packets
                                 if truth(r["delivered_correctly"]))
                denominator = len(latency) if conditional else 2500
                seed_curves.append(np.searchsorted(latency, grid, side="right") /
                                   denominator)
            array = np.asarray(seed_curves)
            mean = array.mean(axis=0)
            half = T95_7*array.std(axis=0, ddof=1)/math.sqrt(8)
            output += [{"rho": rho, "scheme": scheme, "latency_slots": int(x),
                        "mean": y, "ci95_low": max(0, y-d),
                        "ci95_high": min(1, y+d)}
                       for x, y, d in zip(grid, mean, half)]
    return output


def max_histories(all_runs):
    output = []
    for (rho, scheme, seed), run in all_runs.items():
        packets, attempts = run[2], run[3]
        correct = [r for r in packets if truth(r["delivered_correctly"])]
        packet = max(correct, key=lambda r: float(r["completion_latency_slots"]))
        history = sorted((r for r in attempts if r["payload_id"] ==
                          packet["payload_id"]), key=lambda r: int(r["actual_tx_slot"]))
        output.append({"rho": rho, "scheme": scheme, "seed": seed,
            "max_correct_delivery_latency": packet["completion_latency_slots"],
            "payload_id": packet["payload_id"],
            "rlc_retransmission_count": packet["rlc_retx_count"],
            "harq_episode_count": packet.get("harq_episode_count") or
                packet["num_harq_episodes"],
            "scheduled_queue_wait": packet.get("scheduled_queue_wait_slots"),
            "terminal_recovery_history": json.dumps([{
                "slot": int(r["actual_tx_slot"]), "access": r["access"],
                "rv": int(r["rv"]), "rlc": int(r["rlc_retx_index"]),
                "ack": truth(r["ack"])} for r in history])})
    return output


COLORS = {"Legacy-20": "#555555", "Legacy-15": "#999999",
          "Fast-CB": "#CC79A7", "B": "#000000", "H2": "#56B4E9",
          "F2": "#E69F00", "H3": "#0072B2", "F3": "#D55E00"}


def save(fig, base):
    fig.tight_layout()
    for suffix in ("png", "svg", "pdf"):
        fig.savefig(base.with_suffix(f".{suffix}"), dpi=200)
    plt.close(fig)


def completion_plot(data, base, conditional=False):
    panels = (("Legacy-20", "Legacy-15", "Fast-CB", "B"),
              ("B", "H2", "F2", "H3", "F3"))
    fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.1), sharey=True)
    for ax, schemes in zip(axes, panels):
        for scheme in schemes:
            group = [r for r in data if r["rho"] == "0.70" and
                     r["scheme"] == scheme]
            x = np.array([r["latency_slots"] for r in group])
            y = np.array([r["mean"] for r in group])
            ax.plot(x, y, label=scheme, color=COLORS[scheme])
        ax.grid(alpha=.25); ax.legend(fontsize=8); ax.set_xlabel("Latency (slots)")
    axes[0].set_ylabel("Conditional ECDF" if conditional else
                       "Correct-delivery probability")
    save(fig, base)


def mean_ci(table, rho, scheme, metric):
    values = [float(r[metric]) for r in table if r["rho"] == rho and
              r["scheme"] == scheme]
    return statistics.fmean(values), T95_7*statistics.stdev(values)/math.sqrt(8)


def bars(table, schemes, metrics, titles, base, rho="0.70"):
    fig, axes = plt.subplots(1, len(metrics), figsize=(2.75*len(metrics), 3.7))
    for ax, metric, title in zip(np.atleast_1d(axes), metrics, titles):
        values = [mean_ci(table, rho, s, metric) for s in schemes]
        ax.bar(schemes, [x[0] for x in values], yerr=[x[1] for x in values],
               color=[COLORS[s] for s in schemes], capsize=3)
        ax.set_title(title); ax.tick_params(axis="x", rotation=30); ax.grid(axis="y", alpha=.25)
    save(fig, base)


def load_bars(table, schemes, metrics, titles, base):
    fig, axes = plt.subplots(1, len(metrics), figsize=(2.9*len(metrics), 3.7))
    axes = np.atleast_1d(axes)
    x = np.arange(len(schemes)); width = .36
    for ax, metric, title in zip(axes, metrics, titles):
        for offset, rho, color in ((-.5, "0.70", "#56B4E9"),
                                   (.5, "0.90", "#D55E00")):
            values = [mean_ci(table, rho, s, metric) for s in schemes]
            ax.bar(x+offset*width, [v[0] for v in values], width,
                   yerr=[v[1] for v in values], label=f"rho={rho}",
                   color=color, capsize=2)
        ax.set_xticks(x, schemes, rotation=30); ax.set_title(title)
        ax.grid(axis="y", alpha=.25)
    axes[0].legend(fontsize=8)
    save(fig, base)


def main():
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    all_runs = {(rho, scheme, seed): load(args, rho, scheme, seed)
                for rho in ("0.70", "0.90") for scheme in SCENARIOS_070
                for seed in SEEDS}
    table = [run_kpis(rho, scheme, seed, *all_runs[(rho, scheme, seed)][1:])
             for rho in ("0.70", "0.90") for scheme in SCENARIOS_070
             for seed in SEEDS]
    write_csv(args.output/"run_level_kpis.csv", table)
    write_csv(args.output/"scheme_summary.csv", summarize(table))
    rlc = [row for key, run in all_runs.items() for row in
           rlc_rows(*key, run[2])]
    write_csv(args.output/"rlc_retry_distribution.csv", rlc)
    state = [row for key, run in all_runs.items() for row in
             state_rows(*key, run[2], run[3])]
    write_csv(args.output/"harq_rescue_state.csv", state)
    details, summaries = [], []
    for rho in ("0.70", "0.90"):
        for treatment, baseline in COMPARISONS:
            for metric in STAT_METRICS:
                detail, summary = paired(table, rho, treatment, baseline, metric)
                details.extend(detail); summaries.append(summary)
    write_csv(args.output/"paired_seed_differences.csv", details)
    write_csv(args.output/"paired_statistics.csv", summaries)
    cross = cross_load(table)
    write_csv(args.output/"cross_load_descriptive.csv", cross)
    write_csv(args.output/"cross_load_summary.csv", summarize_cross_load(cross))
    maximum = max_histories(all_runs)
    write_csv(args.output/"max_latency_histories.csv", maximum)
    max_summary = []
    for rho in ("0.70", "0.90"):
        for scheme in SCENARIOS_070:
            values = [float(r["max_correct_delivery_latency"]) for r in maximum
                      if r["rho"] == rho and r["scheme"] == scheme]
            max_summary.append({"rho": rho, "scheme": scheme,
                "median_max_latency": statistics.median(values),
                "minimum_max_latency": min(values), "maximum_max_latency": max(values)})
    write_csv(args.output/"max_latency_summary.csv", max_summary)
    caution = [{"rho": r["rho"], "scheme": r["scheme"], "seed": r["seed"],
                "correct_packet_count": r["correct_packet_count"],
                "p99_latency": r["p99_latency"],
                "caution": "P99 is computed within seed from correctly delivered packets."}
               for r in table]
    write_csv(args.output/"p99_seed_caution.csv", caution)
    completion = curves(all_runs, conditional=False)
    conditional = curves(all_runs, conditional=True)
    write_csv(args.output/"completion_curve_source.csv", completion)
    write_csv(args.output/"conditional_ecdf_source.csv", conditional)
    completion_plot(completion, args.output/"v0_7_a_completion")
    completion_plot(conditional, args.output/"v0_7_a2_conditional_ecdf", True)
    bars(table, ("Legacy-20", "B", "H3", "F3"),
         ("correct_delivery_probability", "mean_latency", "p95_latency", "p99_latency"),
         ("Correct delivery", "Mean latency", "P95", "P99"),
         args.output/"v0_7_b_selected_tail")
    bars(table, ("H2", "H3", "F2", "F3"),
         ("rescue_admission_rate", "scheduled_attempts_per_packet", "mean_queue",
          "mean_wait", "p99_latency"),
         ("Rescue admission", "Scheduled attempts/packet", "Mean queue",
          "Mean wait", "P99"), args.output/"v0_7_c_trigger")
    bars(table, ("H2", "F2", "H3", "F3"),
         ("first_scheduled_success_probability",
          "scheduled_attempts_per_rescued_packet", "mean_queue", "mean_wait",
          "p99_latency"),
         ("First scheduled success", "Attempts/rescued packet", "Mean queue",
          "Mean wait", "P99"), args.output/"v0_7_d_harq_state")
    load_bars(table, ("Fast-CB",),
              ("correct_delivery_probability", "terminal_failure_probability",
               "cb_attempts_per_slot", "rlc_retransmissions_per_packet"),
              ("Correct delivery", "Terminal failure", "CB attempts/slot",
               "RLC retransmissions/packet"), args.output/"v0_7_e_fast_cb_load")
    load_bars(table, ("Legacy-20", "B", "H3", "F3"), ("p99_latency",),
              ("P99 latency",), args.output/"v0_7_f_p99_load")
    figure_index = [{"figure": name, "panels": panels} for name, panels in (
        ("v0_7_a_completion", "two policy-group panels; rho=0.70"),
        ("v0_7_a2_conditional_ecdf", "two policy-group panels; rho=0.70"),
        ("v0_7_b_selected_tail", "Legacy-20, B, H3, F3"),
        ("v0_7_c_trigger", "H2/H3 and F2/F3"),
        ("v0_7_d_harq_state", "H2/F2 and H3/F3"),
        ("v0_7_e_fast_cb_load", "Fast-CB at rho=0.70 and rho=0.90"),
        ("v0_7_f_p99_load", "Legacy-20, B, H3, F3 across load"))]
    write_csv(args.output/"figure_index.csv", figure_index)
    artifacts = sorted(p for p in args.output.iterdir() if p.is_file() and
                       p.name != "analysis_manifest.json")
    manifest = {"classification": "V0_7-RHO070-ANALYSIS-COMPLETE",
        "load_robustness_classification": args.classification,
        "load_study_decision": args.load_decision,
        "implementation_sha": args.implementation_sha,
        "results_sha": args.results_sha, "config_sha256": sha256(args.config),
        "statistical_unit": "seed", "paired_seeds": list(SEEDS),
        "cross_load_inference": "descriptive_mechanistic_only",
        "artifact_sha256": {p.name: sha256(p) for p in artifacts}}
    (args.output/"analysis_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"classification": manifest["classification"],
                      "output": str(args.output)}, sort_keys=True))


if __name__ == "__main__":
    main()
