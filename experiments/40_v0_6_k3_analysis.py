#!/usr/bin/env python3
"""Analyze the V0.6 H(K=3) follow-up against reused B and H(K=2)."""

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
SCENARIOS = {"B": "V0_5_RHO090_FAST_SB",
             "H2": "V0_6_RHO090_H_HARQ_PRESERVING_SHARED_CAP2",
             "H3": "V0_6_RHO090_HK3_HARQ_PRESERVING_SHARED_CAP2"}
CLASSIFICATIONS = ("K3-EARLY-RESCUE-SUPPORTED",
                   "K3-ADMISSION-IMPROVEMENT-ONLY",
                   "K3-EARLY-RESCUE-CONDITIONAL",
                   "K3-TRIGGER-NOT-SUPPORTED")
T95_7 = 2.364624251


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--b-raw-root", type=Path,
                        default=Path("results/raw_v0_5"))
    parser.add_argument("--h2-raw-root", type=Path,
                        default=Path("results/raw_v0_6_p3"))
    parser.add_argument("--k3-raw-root", type=Path,
                        default=Path("results/raw_v0_6_k3"))
    parser.add_argument("--output", type=Path,
                        default=Path("results/v0_6_k3"))
    parser.add_argument("--config", type=Path,
                        default=Path("configs/p3_hk3_v0_6.yaml"))
    parser.add_argument("--implementation-sha", required=True)
    parser.add_argument("--results-sha", required=True)
    parser.add_argument("--classification", choices=CLASSIFICATIONS,
                        required=True)
    parser.add_argument("--f3-decision", choices=("F3-GO", "F3-HOLD"),
                        required=True)
    return parser.parse_args()


def rows(path):
    with gzip.open(path, "rt", encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def truth(value):
    return value is True or str(value).lower() == "true"


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def percentile(values, q):
    values = sorted(values)
    position = (len(values) - 1) * q / 100
    lo, hi = math.floor(position), math.ceil(position)
    return values[lo] if lo == hi else values[lo] + (position - lo) * (
        values[hi] - values[lo])


def write_csv(path, data):
    if not data:
        path.write_text("")
        return
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(data[0]),
                                lineterminator="\n")
        writer.writeheader(); writer.writerows(data)


def directory(args, scheme, seed):
    root = {"B": args.b_raw_root, "H2": args.h2_raw_root,
            "H3": args.k3_raw_root}[scheme]
    return root / SCENARIOS[scheme] / f"seed_{seed}"


def load(args, scheme, seed):
    path = directory(args, scheme, seed)
    manifest = json.loads((path / "run_manifest.json").read_text())
    summary = json.loads((path / "summary.json").read_text())
    packets = rows(path / "packets.csv.gz")
    attempts = rows(path / "attempts.csv.gz")
    slots = rows(path / "slot_prb.csv.gz")
    hashes = {name: sha256(path / name) for name in manifest["output_sha256"]}
    if hashes != manifest["output_sha256"] or len(packets) != 2500 or summary[
            "packets"]["unfinished_after_drain"]:
        raise RuntimeError(f"invalid input run: {scheme}/{seed}")
    return manifest, summary, packets, attempts, slots


def kpis(scheme, seed, summary, packets, attempts, slots):
    correct = [row for row in packets if truth(row["delivered_correctly"])]
    latency = [float(row["completion_latency_slots"]) for row in correct]
    scheduled = [row for row in attempts if row["access"] == "scheduled_rescue"]
    cb = [row for row in attempts if row["access"] == "grant_free"]
    rescued = {row["payload_id"] for row in scheduled}
    early = {row["payload_id"] for row in packets
             if truth(row.get("early_rescue_triggered", False))}
    rv2_nack = {row["payload_id"] for row in cb
                if int(row["rv"]) == 2 and not truth(row["ack"])}
    rv3_nack = {row["payload_id"] for row in cb
                if int(row["rv"]) == 3 and not truth(row["ack"])}
    first_scheduled = {}
    for row in scheduled:
        first_scheduled.setdefault(row["payload_id"], row)
        if int(row["actual_tx_slot"]) < int(
                first_scheduled[row["payload_id"]]["actual_tx_slot"]):
            first_scheduled[row["payload_id"]] = row
    queue = [int(row["scheduled_queue_length_before_service"]) for row in slots]
    waits = [int(row["scheduled_queue_wait"]) for row in scheduled
             if row["scheduled_queue_wait"] not in (None, "")]
    residual = [int(row["cb_prbs_available"]) for row in slots]
    rlc = [int(row["rlc_retx_count"]) for row in packets]
    trigger_delays = [int(row["rescue_trigger_to_tx_delay"]) for row in packets
                      if row.get("rescue_trigger_to_tx_delay") not in (None, "")]
    return {"scheme": scheme, "seed": seed,
        "receiver_acceptance_probability": sum(truth(row["receiver_accepted"])
                                                for row in packets) / 2500,
        "correct_delivery_probability": len(correct) / 2500,
        "undetected_error_probability": sum(truth(row["undetected_error"])
                                             for row in packets) / 2500,
        "terminal_failure_probability": sum(row["oracle_terminal_status"] ==
                                              "protocol_drop" for row in packets) / 2500,
        "mean_latency": statistics.fmean(latency),
        "median_latency": statistics.median(latency),
        "p95_latency": percentile(latency, 95), "p99_latency": percentile(latency, 99),
        "max_latency": max(latency), "phy_attempts_per_packet": len(attempts) / 2500,
        "harq_episodes_per_packet": sum(int(row["num_harq_episodes"])
                                         for row in packets) / 2500,
        "rlc_retransmissions_per_packet": statistics.fmean(rlc),
        "rescue_admission_rate": len(early) / 2500,
        "rv2_nack_rate": len(rv2_nack) / 2500,
        "rv3_nack_rate": len(rv3_nack) / 2500,
        "conditional_admission_rate": (len(early) / len(rv3_nack)
            if scheme == "H3" and rv3_nack else
            len(early) / len(rv2_nack) if scheme == "H2" and rv2_nack else 0.0),
        "first_scheduled_success_probability": (sum(truth(row["ack"])
            for row in first_scheduled.values()) / len(first_scheduled)
            if first_scheduled else 0.0),
        "scheduled_attempts_per_rescued_packet": (len(scheduled) / len(rescued)
                                                   if rescued else 0.0),
        "trigger_to_tx_delay": (statistics.fmean(trigger_delays)
                                 if trigger_delays else 0.0),
        "cb_attempts_per_slot": len(cb) / len(slots),
        "scheduled_attempts_per_slot": len(scheduled) / len(slots),
        "scheduled_attempts_per_packet": len(scheduled) / 2500,
        "scheduled_prb_slots": len(scheduled),
        "cb_prb_slots": len({(row["actual_tx_slot"], row["prb"]) for row in cb}),
        "total_occupied_prb_slots": sum(int(row["total_prbs_occupied"]) for row in slots),
        "mean_queue": statistics.fmean(queue), "median_queue": statistics.median(queue),
        "p95_queue": percentile(queue, 95), "max_queue": max(queue),
        "mean_wait": statistics.fmean(waits) if waits else 0.0,
        "p95_wait": percentile(waits, 95) if waits else 0.0,
        "fraction_at_cap2": sum(int(row["scheduled_service_count"]) == 2
                                for row in slots) / len(slots),
        "mean_residual_cb_prbs": statistics.fmean(residual),
        "runtime_seconds": summary["runtime"]["wall_seconds"],
        "peak_rss_kib": summary["runtime"]["peak_rss_kib"]}


def paired(table, treatment, baseline, metric):
    lookup = {(row["scheme"], int(row["seed"])): float(row[metric]) for row in table}
    values = [lookup[(treatment, seed)] - lookup[(baseline, seed)] for seed in SEEDS]
    mean, sd = statistics.fmean(values), statistics.stdev(values)
    se = sd / math.sqrt(8)
    pvalue = sum(abs(sum(sign * value for sign, value in zip(signs, values)) / 8)
                 >= abs(mean) - 1e-15
                 for signs in itertools.product((-1, 1), repeat=8)) / 256
    loo = [statistics.fmean(values[:i] + values[i + 1:]) for i in range(8)]
    return values, {"comparison": f"{treatment}-{baseline}", "metric": metric,
        "mean_difference": mean, "sample_sd": sd, "standard_error": se,
        "ci95_low": mean - T95_7 * se, "ci95_high": mean + T95_7 * se,
        "exact_sign_flip_p": pvalue, "leave_one_out_min": min(loo),
        "leave_one_out_max": max(loo),
        "leave_one_out_sign_stable": min(loo) > 0 or max(loo) < 0}


def curves(all_runs, conditional=False):
    maximum = int(max(float(row["completion_latency_slots"])
                      for run in all_runs.values() for row in run[2]
                      if truth(row["delivered_correctly"])))
    grid = np.arange(maximum + 1); output = []
    for scheme in ("B", "H2", "H3"):
        seed_curves = []
        for seed in SEEDS:
            values = sorted(float(row["completion_latency_slots"])
                            for row in all_runs[(scheme, seed)][2]
                            if truth(row["delivered_correctly"]))
            denominator = len(values) if conditional else 2500
            seed_curves.append(np.searchsorted(values, grid, side="right") / denominator)
        array = np.asarray(seed_curves); mean = array.mean(axis=0)
        half = T95_7 * array.std(axis=0, ddof=1) / math.sqrt(8)
        output += [{"scheme": scheme, "latency_slots": int(x), "mean": y,
                    "ci95_low": max(0, y-d), "ci95_high": min(1, y+d)}
                   for x, y, d in zip(grid, mean, half)]
    return output


def save(fig, base):
    fig.tight_layout()
    for suffix in ("png", "svg", "pdf"):
        fig.savefig(base.with_suffix(f".{suffix}"), dpi=200)
    plt.close(fig)


def curve_plot(data, base, ylabel, tail=False):
    fig, ax = plt.subplots(figsize=(6.4, 4.0)); colors = {
        "B": "#555555", "H2": "#D55E00", "H3": "#0072B2"}
    for scheme in colors:
        group = [row for row in data if row["scheme"] == scheme]
        x = np.array([row["latency_slots"] for row in group])
        y = np.array([row["mean"] for row in group]); lo = np.array(
            [row["ci95_low"] for row in group]); hi = np.array(
            [row["ci95_high"] for row in group])
        ax.plot(x, y, label=scheme, color=colors[scheme]); ax.fill_between(
            x, lo, hi, alpha=.15, color=colors[scheme])
    ax.set_xlabel("Latency (slots)"); ax.set_ylabel(ylabel)
    if tail: ax.set_ylim(.85, 1.005)
    ax.grid(alpha=.25); ax.legend(); save(fig, base)


def grouped_bars(table, metrics, titles, base):
    fig, axes = plt.subplots(1, len(metrics), figsize=(3.0*len(metrics), 3.7))
    for ax, metric, title in zip(np.atleast_1d(axes), metrics, titles):
        groups = [[float(row[metric]) for row in table if row["scheme"] == scheme]
                  for scheme in ("B", "H2", "H3")]
        ax.bar(("B", "H2", "H3"), [statistics.fmean(x) for x in groups],
               yerr=[T95_7*statistics.stdev(x)/math.sqrt(8) for x in groups],
               color=("#555555", "#D55E00", "#0072B2"), capsize=3)
        ax.set_title(title); ax.grid(axis="y", alpha=.25)
    save(fig, base)


def main():
    args = parse_args(); args.output.mkdir(parents=True, exist_ok=True)
    all_runs = {(scheme, seed): load(args, scheme, seed)
                for scheme in ("B", "H2", "H3") for seed in SEEDS}
    table = [kpis(scheme, seed, *all_runs[(scheme, seed)][1:])
             for scheme in ("B", "H2", "H3") for seed in SEEDS]
    write_csv(args.output / "run_level_kpis.csv", table)
    metrics = ("correct_delivery_probability", "terminal_failure_probability",
        "mean_latency", "p95_latency", "p99_latency", "max_latency",
        "scheduled_attempts_per_packet", "scheduled_prb_slots", "mean_queue",
        "mean_wait", "fraction_at_cap2", "rlc_retransmissions_per_packet",
        "total_occupied_prb_slots", "cb_attempts_per_slot",
        "scheduled_attempts_per_slot")
    differences, summaries = [], []
    for treatment, baseline in (("H3", "B"), ("H3", "H2")):
        for metric in metrics:
            values, summary = paired(table, treatment, baseline, metric)
            summaries.append(summary); differences += [
                {"comparison": f"{treatment}-{baseline}", "metric": metric,
                 "seed": seed, "difference": value}
                for seed, value in zip(SEEDS, values)]
    write_csv(args.output / "paired_seed_differences.csv", differences)
    write_csv(args.output / "paired_statistics.csv", summaries)
    write_csv(args.output / "per_seed_tail.csv", [{key: row[key] for key in
        ("scheme", "seed", "p95_latency", "p99_latency", "max_latency")}
        for row in table])
    completion, conditional = curves(all_runs), curves(all_runs, True)
    write_csv(args.output / "completion_curve_source.csv", completion)
    write_csv(args.output / "conditional_ecdf_source.csv", conditional)
    curve_plot(completion, args.output/"v0_6_k3_a_completion",
               "Correct-delivery probability")
    curve_plot(completion, args.output/"v0_6_k3_b_tail_zoom",
               "Correct-delivery probability", True)
    grouped_bars(table, ("p95_latency", "p99_latency"), ("P95", "P99"),
                 args.output/"v0_6_k3_c_tail_summary")
    grouped_bars(table, ("rescue_admission_rate", "scheduled_attempts_per_packet",
        "mean_queue", "mean_wait", "fraction_at_cap2"),
        ("Rescue admission", "Scheduled attempts/packet", "Mean queue",
         "Mean wait", "Fraction at Cap2"), args.output/"v0_6_k3_d_admission")
    grouped_bars(table, ("cb_attempts_per_slot", "scheduled_attempts_per_slot",
        "scheduled_prb_slots", "mean_residual_cb_prbs"),
        ("CB attempts/slot", "Scheduled attempts/slot", "Scheduled PRB-slots",
         "Residual CB PRBs"), args.output/"v0_6_k3_e_resource_tradeoff")
    hist = []
    for scheme in ("B", "H2", "H3"):
        values = []
        for seed in SEEDS:
            counts = Counter(int(row["rlc_retx_count"])
                             for row in all_runs[(scheme, seed)][2])
            values.append([counts[i]/2500 for i in range(7)])
        array = np.asarray(values)
        hist += [{"scheme": scheme, "rlc_retx_count": i,
                  "mean_fraction": array[:, i].mean(), "ci95_half_width":
                  T95_7*array[:, i].std(ddof=1)/math.sqrt(8)} for i in range(7)]
    write_csv(args.output/"rlc_retry_distribution.csv", hist)
    fig, ax = plt.subplots(figsize=(6.4,4.0))
    for scheme, marker in zip(("B","H2","H3"),("o","s","^")):
        group=[row for row in hist if row["scheme"]==scheme]
        ax.errorbar([row["rlc_retx_count"] for row in group],
                    [row["mean_fraction"] for row in group],
                    yerr=[row["ci95_half_width"] for row in group],
                    marker=marker,label=scheme,capsize=2)
    ax.set_yscale("log"); ax.set_xlabel("RLC retransmission count")
    ax.set_ylabel("Packet fraction"); ax.grid(alpha=.25); ax.legend()
    save(fig,args.output/"v0_6_k3_f_rlc_depth")
    maxima=[]
    for scheme in ("B","H2","H3"):
        for seed in SEEDS:
            packets=all_runs[(scheme,seed)][2]
            packet=max((row for row in packets if truth(row["delivered_correctly"])),
                       key=lambda row:float(row["completion_latency_slots"]))
            history=[row for row in all_runs[(scheme,seed)][3]
                     if row["payload_id"]==packet["payload_id"]]
            maxima.append({"scheme":scheme,"seed":seed,
                "payload_id":packet["payload_id"],"max_latency":packet["completion_latency_slots"],
                "rlc_retx_count":packet["rlc_retx_count"],"attempt_history_json":json.dumps(
                [{"slot":int(row["actual_tx_slot"]),"rv":int(row["rv"]),
                  "access":row["access"],"episode":row["harq_episode_id"],
                  "ack":truth(row["ack"])} for row in history])})
    write_csv(args.output/"max_latency_histories.csv",maxima)
    means=[{"scheme":scheme,**{metric:statistics.fmean(float(row[metric])
        for row in table if row["scheme"]==scheme) for metric in table[0]
        if metric not in {"scheme","seed"}}} for scheme in ("B","H2","H3")]
    write_csv(args.output/"scheme_seed_means.csv",means)
    artifacts=sorted(path for path in args.output.iterdir() if path.is_file())
    manifest={"execution_classification":"V0_6-K3-COMPLETE",
        "scientific_classification":args.classification,"f3_decision":args.f3_decision,
        "implementation_sha":args.implementation_sha,"results_sha":args.results_sha,
        "config_sha256":sha256(args.config),"seeds":list(SEEDS),
        "schemes":{"B":"8 reused","H2":"8 reused","H3":"8 new"},
        "statistics":"paired seed-level Student-t 95% CI; exact sign-flip; leave-one-out",
        "artifact_sha256":{path.name:sha256(path) for path in artifacts}}
    (args.output/"analysis_manifest.json").write_text(
        json.dumps(manifest,indent=2,sort_keys=True)+"\n")
    print(json.dumps({"execution":"V0_6-K3-COMPLETE",
                      "classification":args.classification,
                      "f3_decision":args.f3_decision},sort_keys=True))


if __name__=="__main__": main()
