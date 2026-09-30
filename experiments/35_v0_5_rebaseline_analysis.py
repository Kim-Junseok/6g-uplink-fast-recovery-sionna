#!/usr/bin/env python3
"""Analyze the frozen V0.5 high-load protocol re-baseline."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import Counter
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

from ul_access.analysis import (
    RunArtifact,
    exact_sign_flip_pvalue,
    leave_one_out_means,
    mean_student_t_ci95,
    paired_difference_summary,
    seed_averaged_curve,
)
from ul_access.analysis.campaign import sha256
from ul_access.analysis.plotting import (
    curve_figure,
    export_figure,
    multi_metric_panel_figure,
    two_metric_bar_figure,
)


SEEDS = tuple(range(9101, 9109))
POLICIES = ("Legacy-20", "Legacy-15", "Fast-CB", "Fast-SB")
SCENARIOS = {
    "Legacy-20": "V0_5_RHO090_LEGACY_RLC_SB_T20",
    "Legacy-15": "V0_5_RHO090_LEGACY_RLC_SB_T15",
    "Fast-CB": "V0_5_RHO090_FAST_CB",
    "Fast-SB": "V0_5_RHO090_FAST_SB",
}
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", type=Path,
                        default=Path("results/raw_v0_5"))
    parser.add_argument("--output", type=Path,
                        default=Path("results/v0_5_rebaseline"))
    parser.add_argument("--config", type=Path,
                        default=Path("configs/timing_rebaseline_v0_5.yaml"))
    parser.add_argument("--implementation-sha",
                        help="Expected freeze SHA; inferred when omitted")
    return parser.parse_args()


def truth(value) -> bool:
    return value is True or str(value).lower() == "true"


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q / 100
    lower, upper = math.floor(position), math.ceil(position)
    return (ordered[lower] if lower == upper else
            ordered[lower] + (position - lower) *
            (ordered[upper] - ordered[lower]))


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        raise ValueError(f"cannot write empty table: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]),
                                lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def artifacts(root: Path) -> list[RunArtifact]:
    return [RunArtifact(SCENARIOS[policy], policy, 0.90, seed,
                        root / SCENARIOS[policy] / f"seed_{seed}")
            for policy in POLICIES for seed in SEEDS]


def normalize(run: RunArtifact, implementation_sha: str,
              config_sha: str) -> tuple[dict, dict, dict]:
    manifest, summary = run.manifest(), run.summary()
    packets, attempts, slots = (run.rows(name)
                                for name in ("packets", "attempts", "slot_prb"))
    timing = run.rows("recovery_timing")
    correct = [row for row in packets if truth(row["delivered_correctly"])]
    latencies = [float(row["completion_latency_slots"]) for row in correct]
    cb = [row for row in attempts if row["access"] == "grant_free"]
    scheduled = [row for row in attempts
                 if row["access"] == "scheduled_rescue"]
    retry_hist = Counter(int(row["rlc_retx_count"]) for row in packets)
    resource = summary["resources"]
    record = {
        "policy": run.scheme, "scenario_id": run.scenario, "seed": run.seed,
        "measured_packets": len(packets),
        "receiver_acceptance_probability": sum(
            truth(row["receiver_accepted"]) for row in packets) / len(packets),
        "correct_delivery_probability": len(correct) / len(packets),
        "undetected_error_probability": sum(
            truth(row["undetected_error"]) for row in packets) / len(packets),
        "protocol_terminal_failure_probability": sum(
            row["oracle_terminal_status"] == "protocol_drop"
            for row in packets) / len(packets),
        "mean_correct_latency_slots": statistics.fmean(latencies),
        "median_correct_latency_slots": percentile(latencies, 50),
        "p95_correct_latency_slots": percentile(latencies, 95),
        "p99_correct_latency_slots": percentile(latencies, 99),
        "phy_attempts": len(attempts),
        "harq_episode_count": sum(int(row["num_harq_episodes"])
                                  for row in packets),
        "final_harq_failure_count": summary["phy"][
            "final_harq_failure_events"],
        "rlc_retransmissions": sum(int(row["rlc_retx_count"])
                                   for row in packets),
        "terminal_failure_after_threshold": sum(
            row["oracle_terminal_status"] == "protocol_drop"
            for row in packets),
        "cb_attempts_per_slot": len(cb) / len(slots),
        "cb_overlap_probability": sum(
            int(row["same_prb_occupancy"]) >= 2 for row in cb) / len(cb),
        "scheduled_attempts_per_slot": len(scheduled) / len(slots),
        "mean_scheduled_queue_length": statistics.fmean(
            int(row["scheduled_queue_length"]) for row in slots),
        "mean_scheduled_queue_wait_slots": statistics.fmean(
            float(row["scheduled_queue_wait"])
            for row in scheduled) if scheduled else 0.0,
        "cb_prb_slots": resource["cb_prb_slots"],
        "scheduled_prb_slots": resource["scheduled_prb_slots"],
        "total_occupied_prb_slots": resource["combined_physical_resource_use"],
        "mean_residual_cb_prbs": statistics.fmean(
            int(row["cb_prbs_available"]) for row in slots),
        "drain_slots": max(0, len(slots) - math.ceil(
            summary["traffic"]["arrival_horizon_slots"])),
        "mean_legacy_sr_wait_slots": (statistics.fmean(
            int(row["sr_wait_slots"]) for row in timing) if timing else None),
        "mean_legacy_sr_to_tx_slots": (statistics.fmean(
            int(row["sr_to_scheduled_tx_slots"]) for row in timing)
            if timing else None),
        "mean_legacy_queue_wait_slots": (statistics.fmean(
            int(row["scheduled_queue_wait_slots"]) for row in timing)
            if timing else None),
    }
    for index in range(7):
        record[f"rlc_retx_{index}_fraction"] = retry_hist[index] / len(packets)
        if index:
            record[f"fraction_reaching_retx_{index}"] = sum(
                int(row["rlc_retx_count"]) >= index
                for row in packets) / len(packets)
    diagnostic = {
        "correct_latencies": latencies,
        "conditional_latencies": latencies,
        "retry_hist": retry_hist,
        "residual_cb": Counter(int(row["cb_prbs_available"]) for row in slots),
        "slots": len(slots),
    }
    audit = {
        "policy": run.scheme, "seed": run.seed,
        "hashes_match": run.verify_hashes(),
        "clean_production": manifest["dirty_worktree"] is False,
        "measured_packets_match": len(packets) == 2500,
        "implementation_sha_matches": manifest["git_sha"] == implementation_sha,
        "config_sha_matches": manifest["config_file_sha256"] == config_sha,
        "terminal": summary["packets"]["unfinished_after_drain"] == 0,
        "resource_invariants": all(truth(row["pool_invariant"]) and
                                   int(row["scheduled_service_count"]) <= 2 and
                                   int(row["cb_prbs_available"]) >= 4
                                   for row in slots),
    }
    return record, diagnostic, audit


def interval(records: list[dict], policy: str, metric: str) -> dict:
    return mean_student_t_ci95([
        row[metric] for row in records if row["policy"] == policy])


def export(output: Path, name: str, fig, source: list[dict], metadata: dict,
           claim: str, index: list[dict]) -> None:
    source_name = f"{name}_source.csv"
    write_csv(output / source_name, source)
    export_figure(fig, output / name, {
        **metadata, "source_csv": source_name,
        "figure_class": "MAIN-FIGURE-CANDIDATE", "caption_source": claim})
    index.append({"figure": name, "class": "MAIN-FIGURE-CANDIDATE",
                  "claim": claim})


def ci_vectors(records: list[dict], metric: str) -> dict:
    values = [interval(records, policy, metric) for policy in POLICIES]
    return {"mean": [row["mean"] for row in values],
            "lower": [row["ci95_lower"] for row in values],
            "upper": [row["ci95_upper"] for row in values]}


def main() -> None:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    runs = artifacts(args.raw_root)
    config_sha = sha256(args.config)
    implementation_sha = args.implementation_sha or runs[0].manifest()["git_sha"]
    records, diagnostics, audits = [], {}, []
    for run in runs:
        record, diagnostic, audit = normalize(
            run, implementation_sha, config_sha)
        records.append(record)
        diagnostics[(run.scheme, run.seed)] = diagnostic
        audits.append(audit)
    write_csv(args.output / "run_level_kpis.csv", records)

    metric_names = [key for key in records[0]
                    if key not in {"policy", "scenario_id", "seed"}]
    means = []
    for policy in POLICIES:
        row = {"policy": policy}
        for metric in metric_names:
            values = [entry[metric] for entry in records
                      if entry["policy"] == policy and
                      entry[metric] is not None]
            row[metric] = statistics.fmean(values) if values else None
        means.append(row)
    write_csv(args.output / "policy_seed_means.csv", means)

    metadata = {
        "generation_command": (
            "PYTHONPATH=src MPLCONFIGDIR=/tmp/matplotlib "
            "python "
            "experiments/35_v0_5_rebaseline_analysis.py"),
        "implementation_sha": implementation_sha,
        "config_sha256": config_sha, "rho": 0.90,
        "seeds": list(SEEDS),
        "uncertainty": "95% Student-t interval over eight paired seeds",
    }
    figure_index = []
    max_latency = math.ceil(max(
        max(value["correct_latencies"], default=0)
        for value in diagnostics.values()))
    grid = list(range(max_latency + 1))
    completion = {policy: seed_averaged_curve(
        {seed: diagnostics[(policy, seed)]["correct_latencies"]
         for seed in SEEDS}, grid,
        denominators={seed: 2500 for seed in SEEDS})
        for policy in POLICIES}
    export(args.output, "v0_5_1_completion",
           curve_figure(completion, title="V0.5 completion at rho=0.90",
                        ylabel="Correct completion probability"),
           [{"policy": policy, **row} for policy, curve in completion.items()
            for row in curve], metadata,
           "Correct-delivery-aware completion includes terminal-failure mass.",
           figure_index)

    conditional = {policy: seed_averaged_curve(
        {seed: diagnostics[(policy, seed)]["conditional_latencies"]
         for seed in SEEDS}, grid) for policy in POLICIES}
    export(args.output, "v0_5_2_conditional_latency",
           curve_figure(conditional, title="Conditional correct-delivery latency",
                        ylabel="Conditional ECDF"),
           [{"policy": policy, **row} for policy, curve in conditional.items()
            for row in curve], metadata,
           "Conditional latency is shown separately from completion.",
           figure_index)

    export(args.output, "v0_5_3_reliability",
           two_metric_bar_figure(
               list(POLICIES),
               ci_vectors(records, "correct_delivery_probability"),
               ci_vectors(records, "protocol_terminal_failure_probability"),
               left_title="Correct delivery", right_title="Terminal failure",
               left_ylabel="Probability", right_ylabel="Probability"),
           [{"policy": policy, "metric": metric, **interval(records, policy, metric)}
            for policy in POLICIES
            for metric in ("correct_delivery_probability",
                           "protocol_terminal_failure_probability")], metadata,
           "Reliability uses the full measured-packet population.", figure_index)

    export(args.output, "v0_5_4_latency",
           two_metric_bar_figure(
               list(POLICIES), ci_vectors(records, "mean_correct_latency_slots"),
               ci_vectors(records, "p95_correct_latency_slots"),
               left_title="Mean correct latency", right_title="P95 correct latency",
               left_ylabel="Slots", right_ylabel="Slots"),
           [{"policy": policy, "metric": metric, **interval(records, policy, metric)}
            for policy in POLICIES
            for metric in ("mean_correct_latency_slots",
                           "p95_correct_latency_slots")], metadata,
           "Latency intervals use seed-level replication.", figure_index)

    retry_source = []
    fig, axis = plt.subplots(figsize=(8.0, 4.5))
    x = np.arange(7); width = 0.19
    for policy_index, policy in enumerate(POLICIES):
        values = [statistics.fmean(
            diagnostics[(policy, seed)]["retry_hist"].get(index, 0) / 2500
            for seed in SEEDS) for index in range(7)]
        axis.bar(x + (policy_index - 1.5) * width, values, width,
                 label=policy, alpha=0.82)
        retry_source.extend({"policy": policy, "rlc_retx_count": index,
                             "packet_fraction": value}
                            for index, value in enumerate(values))
    axis.set(xlabel="RLC retransmission episodes", ylabel="Packet fraction",
             title="RLC retransmission-count distribution")
    axis.set_xticks(x); axis.grid(True, axis="y", alpha=0.25)
    axis.legend(frameon=False)
    fig.tight_layout()
    export(args.output, "v0_5_5_rlc_retry_distribution", fig, retry_source,
           metadata, "The distribution retains every count from zero to six.",
           figure_index)

    legacy = ("Legacy-20", "Legacy-15")
    decomposition_metrics = (
        ("mean_legacy_sr_wait_slots", "Timer expiry to SR"),
        ("mean_legacy_sr_to_tx_slots", "SR to scheduled TX"),
        ("mean_legacy_queue_wait_slots", "Queue wait beyond target"))
    panels, source = [], []
    for metric, title in decomposition_metrics:
        values = [interval(records, policy, metric) for policy in legacy]
        panels.append({"title": title, "xlabel": "Legacy condition",
                       "ylabel": "Slots", "series": {"mean": {
                           "x": [15, 20],
                           "mean": [values[1]["mean"], values[0]["mean"]],
                           "lower": [values[1]["ci95_lower"],
                                     values[0]["ci95_lower"]],
                           "upper": [values[1]["ci95_upper"],
                                     values[0]["ci95_upper"]]}}})
        source.extend({"metric": metric, "policy": policy, **row}
                      for policy, row in zip(legacy, values))
    export(args.output, "v0_5_6_legacy_timing",
           multi_metric_panel_figure(panels, figsize=(12, 4)), source,
           metadata, "Legacy timing separates SR and scheduled-queue delays.",
           figure_index)

    fast_metrics = (
        ("cb_attempts_per_slot", "CB attempts/slot"),
        ("cb_overlap_probability", "CB-attempt overlap"),
        ("scheduled_attempts_per_slot", "Scheduled attempts/slot"),
        ("total_occupied_prb_slots", "Occupied PRB-slots"))
    panels, source = [], []
    for metric, title in fast_metrics:
        values = [interval(records, policy, metric)
                  for policy in ("Fast-CB", "Fast-SB")]
        panels.append({"title": title, "xlabel": "Fast recovery",
                       "ylabel": title, "series": {"mean": {
                           "x": [0, 1],
                           "mean": [row["mean"] for row in values],
                           "lower": [row["ci95_lower"] for row in values],
                           "upper": [row["ci95_upper"] for row in values]}},
                       "xticks": [0, 1],
                       "xtick_labels": ["Fast-CB", "Fast-SB"]})
        source.extend({"metric": metric, "policy": policy, **row}
                      for policy, row in zip(("Fast-CB", "Fast-SB"), values))
    export(args.output, "v0_5_7_fast_resource_domain",
           multi_metric_panel_figure(panels, columns=2), source, metadata,
           "Fast-CB and Fast-SB isolate the recovery resource domain.",
           figure_index)

    queue_metrics = (
        ("mean_scheduled_queue_length", "Mean scheduled queue"),
        ("mean_scheduled_queue_wait_slots", "Mean scheduled wait"),
        ("scheduled_prb_slots", "Scheduled PRB-slots"),
        ("mean_residual_cb_prbs", "Mean residual CB PRBs"))
    panels, source = [], []
    for metric, title in queue_metrics:
        values = [interval(records, policy, metric) for policy in POLICIES]
        panels.append({"title": title, "xlabel": "Condition",
                       "ylabel": title, "series": {"mean": {
                           "x": list(range(4)),
                           "mean": [row["mean"] for row in values],
                           "lower": [row["ci95_lower"] for row in values],
                           "upper": [row["ci95_upper"] for row in values]}},
                       "xticks": list(range(4)),
                       "xtick_labels": list(POLICIES)})
        source.extend({"metric": metric, "policy": policy, **row}
                      for policy, row in zip(POLICIES, values))
    export(args.output, "v0_5_8_scheduled_queue_resource",
           multi_metric_panel_figure(panels, columns=2), source, metadata,
           "Scheduled queueing is reported with shared-pool capacity.",
           figure_index)

    comparisons = (
        ("A", "Legacy-20", "Legacy-15"),
        ("B", "Legacy-20", "Fast-SB"),
        ("B", "Legacy-15", "Fast-SB"),
        ("C", "Fast-CB", "Fast-SB"))
    statistical_metrics = (
        "correct_delivery_probability",
        "protocol_terminal_failure_probability",
        "mean_correct_latency_slots", "p95_correct_latency_slots",
        "phy_attempts", "harq_episode_count", "final_harq_failure_count",
        "rlc_retransmissions", "cb_attempts_per_slot",
        "cb_overlap_probability", "scheduled_attempts_per_slot",
        "mean_scheduled_queue_length", "mean_scheduled_queue_wait_slots",
        "cb_prb_slots", "scheduled_prb_slots",
        "total_occupied_prb_slots", "mean_residual_cb_prbs", "drain_slots")
    differences, summaries = [], []
    for group, baseline, treatment in comparisons:
        for metric in statistical_metrics:
            before = [next(row[metric] for row in records
                           if row["policy"] == baseline and row["seed"] == seed)
                      for seed in SEEDS]
            after = [next(row[metric] for row in records
                          if row["policy"] == treatment and row["seed"] == seed)
                     for seed in SEEDS]
            result = paired_difference_summary(before, after)
            for seed, old, new, difference in zip(
                    SEEDS, before, after, result["differences"]):
                differences.append({
                    "comparison_group": group, "baseline": baseline,
                    "treatment": treatment, "metric": metric, "seed": seed,
                    "baseline_value": old, "treatment_value": new,
                    "paired_difference": difference})
            loo = leave_one_out_means(result["differences"])
            summaries.append({
                "comparison_group": group, "baseline": baseline,
                "treatment": treatment, "metric": metric,
                **{key: value for key, value in result.items()
                   if key != "differences"},
                "leave_one_out_min": min(loo),
                "leave_one_out_max": max(loo),
                "leave_one_out_sign_stable": (
                    all(value > 0 for value in loo) or
                    all(value < 0 for value in loo)),
                "exact_two_sided_sign_flip_pvalue":
                    exact_sign_flip_pvalue(result["differences"])})
    write_csv(args.output / "paired_seed_differences.csv", differences)
    write_csv(args.output / "paired_statistics.csv", summaries)
    write_csv(args.output / "figure_index.csv", figure_index)

    integrity_checks = {
        "all_output_hashes_match": all(row["hashes_match"] for row in audits),
        "all_clean_production": all(row["clean_production"] for row in audits),
        "all_measured_packets_match": all(
            row["measured_packets_match"] for row in audits),
        "all_implementation_shas_match": all(
            row["implementation_sha_matches"] for row in audits),
        "all_config_shas_match": all(
            row["config_sha_matches"] for row in audits),
        "all_terminal": all(row["terminal"] for row in audits),
        "all_resource_invariants_match": all(
            row["resource_invariants"] for row in audits),
    }
    integrity_pass = len(audits) == 32 and all(integrity_checks.values())
    integrity = {
        "classification": ("V0_5-ANALYSIS-INTEGRITY-PASS" if integrity_pass
                           else "V0_5-ANALYSIS-INTEGRITY-FAILED"),
        "run_count": len(audits),
        **integrity_checks,
        "runs": audits,
    }
    (args.output / "integrity_audit.json").write_text(
        json.dumps(integrity, indent=2, sort_keys=True) + "\n")
    (args.output / "analysis_manifest.json").write_text(json.dumps({
        **metadata, "classification": "V0_5-REBASELINE-ANALYSIS-COMPLETE",
        "run_count": len(records), "policies": list(POLICIES),
        "figures": len(figure_index)}, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "classification": "V0_5-REBASELINE-ANALYSIS-COMPLETE",
        "runs": len(records), "figures": len(figure_index),
        "output": str(args.output)}, sort_keys=True))
    if not integrity_pass:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
