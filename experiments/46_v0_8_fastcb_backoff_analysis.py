#!/usr/bin/env python3
"""Audit and analyze the frozen eight-seed Fast-CB Test D comparison."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import statistics
from pathlib import Path

from ul_access.analysis import (exact_sign_flip_pvalue,
                                leave_one_out_means, paired_difference_summary)

SEEDS = tuple(range(9101, 9109))
BASE_SCENARIO = "V0_5_RHO090_FAST_CB"
TEST_SCENARIO = "V0_8_RHO090_FAST_CB_U0_3"


def read_csv(path: Path) -> list[dict[str, str]]:
    with gzip.open(path, "rt", newline="") as stream:
        return list(csv.DictReader(stream))


def write_csv(path: Path, records: list[dict]) -> None:
    if not records:
        raise RuntimeError(f"refusing to write empty analysis: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(records[0]),
                                lineterminator="\n")
        writer.writeheader()
        writer.writerows(records)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def truth(value: str) -> bool:
    return value == "True"


def percentile(values: list[int], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise RuntimeError("conditional latency population is empty")
    position = (len(ordered) - 1) * q / 100
    lower, upper = math.floor(position), math.ceil(position)
    return (ordered[lower] if lower == upper else
            ordered[lower] + (position - lower) *
            (ordered[upper] - ordered[lower]))


def inspect(directory: Path, *, production: bool) -> tuple[dict, list[int], list[dict]]:
    manifest = json.loads((directory / "run_manifest.json").read_text())
    summary = json.loads((directory / "summary.json").read_text())
    if not all(sha256(directory / name) == expected for name, expected
               in manifest["output_sha256"].items()):
        raise RuntimeError(f"artifact hash mismatch: {directory}")
    if production and (manifest["dirty_worktree"] or not manifest["production"]):
        raise RuntimeError(f"Test D manifest is not clean production: {directory}")
    packets = read_csv(directory / "packets.csv.gz")
    attempts = read_csv(directory / "attempts.csv.gz")
    slots = read_csv(directory / "slot_prb.csv.gz")
    if len(packets) != 2500 or summary["packets"]["unfinished_after_drain"] != 0:
        raise RuntimeError(f"Test D measured/drain invariant failed: {directory}")
    if not all(truth(row["pool_invariant"]) for row in slots):
        raise RuntimeError(f"Test D PRB invariant failed: {directory}")
    correct = [row for row in packets if truth(row["delivered_correctly"])]
    drops = [row for row in packets if row["oracle_terminal_status"] == "protocol_drop"]
    undetected = [row for row in packets if truth(row["undetected_error"])]
    if len(correct) + len(drops) + len(undetected) != 2500:
        raise RuntimeError(f"Test D terminal partition failed: {directory}")
    latencies = [int(row["completion_latency_slots"]) for row in correct]
    horizon = int(summary["traffic"]["arrival_horizon_slots"])
    injection = [row for row in slots if int(row["slot"]) < horizon]
    cb_injection = [int(row["cb_attempt_count"]) for row in injection]
    if len(injection) != horizon or len(slots) < horizon:
        raise RuntimeError(f"Test D injection-window accounting failed: {directory}")
    decisions = (read_csv(directory / "reinjection_decisions.csv.gz")
                 if production else [])
    if production:
        for row in decisions:
            b = int(row["additional_opportunities_skipped"])
            first = int(row["original_first_opportunity"])
            target = int(row["target_opportunity"])
            if (b not in range(4) or target != first + b or
                    int(row["actual_first_rv0_slot"]) != target):
                raise RuntimeError(f"Test D backoff audit failed: {directory}")
        histogram = {b: sum(int(row["additional_opportunities_skipped"]) == b
                            for row in decisions) for b in range(4)}
        if sum(histogram.values()) != len(decisions):
            raise RuntimeError("Test D decision count mismatch")
    record = {
        "seed": int(manifest["seed"]),
        "policy": "Backoff Fast-CB" if production else "Reference Fast-CB",
        "packet_delivery_probability": len(correct) / 2500,
        "terminal_failure_probability": len(drops) / 2500,
        "undetected_error_probability": len(undetected) / 2500,
        "rlc_retransmissions_per_exogenous_packet": sum(
            int(row["rlc_retx_count"]) for row in packets) / 2500,
        "cb_attempts_per_exogenous_packet": len(attempts) / 2500,
        "cb_measured_attempts_per_recorded_slot": len(attempts) / len(slots),
        "cb_all_attempts_per_injection_slot": sum(cb_injection) / horizon,
        "cb_injection_slot_attempt_p95": percentile(cb_injection, 95),
        "cb_injection_slot_attempt_variance": statistics.pvariance(cb_injection),
        "packets_unresolved_at_injection_end": sum(
            int(row["completion_slot"]) >= horizon for row in packets),
        "drain_slots": len(slots) - horizon,
        "p99_packet_latency_conditioned_on_delivery_slots":
            percentile(latencies, 99),
        "cb_overlap_probability": sum(
            int(row["same_prb_occupancy"]) >= 2 for row in attempts) /
            len(attempts),
        "injection_horizon_slots": horizon,
        "reinjection_decisions_all_packets": len(decisions) if production else "",
    }
    if abs(record["packet_delivery_probability"] -
           summary["oracle_reliability"]["correct_delivery_probability"]) > 1e-12:
        raise RuntimeError(f"Test D summary/raw delivery mismatch: {directory}")
    return record, latencies, decisions


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", type=Path,
                        default=Path("results/raw_v0_8_test_d"))
    parser.add_argument("--reference-root", type=Path,
                        default=Path("results/raw_v0_5"))
    parser.add_argument("--output", type=Path,
                        default=Path("results/v0_8_test_d"))
    args = parser.parse_args()
    records, latency = [], {}
    impl_sha = None
    for seed in SEEDS:
        before_dir = args.reference_root / BASE_SCENARIO / f"seed_{seed}"
        after_dir = args.raw_root / TEST_SCENARIO / f"seed_{seed}"
        before, before_latency, _ = inspect(before_dir, production=False)
        after, after_latency, decisions = inspect(after_dir, production=True)
        before_manifest = json.loads((before_dir / "run_manifest.json").read_text())
        after_manifest = json.loads((after_dir / "run_manifest.json").read_text())
        if (before_manifest["git_sha"] !=
                "d66574649168feeb4a48d95784ea7ecf1255babc" or
                before_manifest["config_file_sha256"] !=
                "23e70da2233671875735f91613709ebc57d803409b5ac7f646dae49c49e9a834"):
            raise RuntimeError("accepted V0.5 Fast-CB reference identity mismatch")
        current = after_manifest["implementation_sha"]
        if impl_sha is None:
            impl_sha = current
        elif impl_sha != current:
            raise RuntimeError("Test D production implementation SHA varied")
        if (after_manifest["seed"] != seed or
                after_manifest["scenario_id"] != TEST_SCENARIO or
                before_manifest["seed"] != seed):
            raise RuntimeError("paired seed/scenario mismatch")
        records.extend((before, after))
        latency[(seed, "Reference Fast-CB")] = before_latency
        latency[(seed, "Backoff Fast-CB")] = after_latency
        if not decisions:
            raise RuntimeError("missing Test D reinjection decisions")
    produced = {p.name for p in (args.raw_root / TEST_SCENARIO).iterdir()
                if p.is_dir()}
    expected = {f"seed_{seed}" for seed in SEEDS}
    if produced != expected:
        raise RuntimeError(f"Test D production cell set mismatch: {produced ^ expected}")
    write_csv(args.output / "run_level_metrics.csv", records)
    metric_names = [name for name in records[0] if name not in {
        "seed", "policy", "injection_horizon_slots",
        "reinjection_decisions_all_packets"}]
    paired, summaries = [], []
    for metric in metric_names:
        baseline = [next(row[metric] for row in records
                         if row["seed"] == seed and
                         row["policy"] == "Reference Fast-CB")
                    for seed in SEEDS]
        treatment = [next(row[metric] for row in records
                          if row["seed"] == seed and
                          row["policy"] == "Backoff Fast-CB")
                     for seed in SEEDS]
        result = paired_difference_summary(baseline, treatment)
        for seed, old, new, difference in zip(
                SEEDS, baseline, treatment, result["differences"]):
            paired.append({"metric": metric, "seed": seed,
                           "reference": old, "backoff": new,
                           "backoff_minus_reference": difference})
        loo = leave_one_out_means(result["differences"])
        summaries.append({"metric": metric,
                          "reference_mean": statistics.fmean(baseline),
                          "backoff_mean": statistics.fmean(treatment),
                          "paired_mean_difference": result["mean"],
                          "ci95_lower": result["ci95_lower"],
                          "ci95_upper": result["ci95_upper"],
                          "exact_two_sided_sign_flip_pvalue":
                              exact_sign_flip_pvalue(result["differences"]),
                          "leave_one_out_min": min(loo),
                          "leave_one_out_max": max(loo)})
    write_csv(args.output / "paired_seed_differences.csv", paired)
    write_csv(args.output / "paired_statistics.csv", summaries)
    max_latency = max(max(values) for values in latency.values())
    curves = []
    for bound in range(max_latency + 1):
        row = {"packet_latency_bound_slots": bound}
        for policy in ("Reference Fast-CB", "Backoff Fast-CB"):
            row[policy] = statistics.fmean(
                sum(value <= bound for value in latency[(seed, policy)]) / 2500
                for seed in SEEDS)
        curves.append(row)
    write_csv(args.output / "packet_delivery_vs_latency.csv", curves)
    manifest = {"classification": "V0.8-TEST-D-ANALYSIS-COMPLETE",
                "reference_run_count": 8, "new_production_run_count": 8,
                "implementation_sha": impl_sha,
                "run_level_sha256": sha256(args.output / "run_level_metrics.csv"),
                "paired_statistics_sha256": sha256(args.output / "paired_statistics.csv"),
                "delivery_curve_sha256": sha256(
                    args.output / "packet_delivery_vs_latency.csv")}
    (args.output / "analysis_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps(manifest, sort_keys=True))


if __name__ == "__main__":
    main()
