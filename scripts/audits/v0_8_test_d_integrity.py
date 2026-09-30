#!/usr/bin/env python3
"""Reconstruct Test D packet, HARQ, resource, and reinjection invariants."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

SEEDS = range(9101, 9109)
RV_SEQUENCE = (0, 2, 3, 1)
BASE = "V0_5_RHO090_FAST_CB"
TEST = "V0_8_RHO090_FAST_CB_U0_3"


def rows(path: Path) -> list[dict[str, str]]:
    with gzip.open(path, "rt", newline="") as stream:
        return list(csv.DictReader(stream))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check(directory: Path, seed: int, *, treatment: bool) -> dict:
    manifest = json.loads((directory / "run_manifest.json").read_text())
    summary = json.loads((directory / "summary.json").read_text())
    for name, expected in manifest["output_sha256"].items():
        if sha256(directory / name) != expected:
            raise RuntimeError(f"output hash mismatch: {directory}/{name}")
    packets = rows(directory / "packets.csv.gz")
    attempts = rows(directory / "attempts.csv.gz")
    slots = rows(directory / "slot_prb.csv.gz")
    if len(packets) != 2500 or summary["packets"]["unfinished_after_drain"]:
        raise RuntimeError(f"packet/drain count mismatch: {directory}")
    if not all(row["pool_invariant"] == "True" for row in slots):
        raise RuntimeError(f"PRB budget violation: {directory}")
    if any(int(row["scheduled_attempts_admitted"]) != 0 or
           int(row["scheduled_prbs_used"]) != 0 or
           int(row["cb_prbs_available"]) != 6 for row in slots):
        raise RuntimeError(f"Fast-CB resource-domain violation: {directory}")
    by_payload = defaultdict(list)
    by_episode = defaultdict(list)
    measured_per_slot = Counter()
    for row in attempts:
        payload = int(row["payload_id"])
        slot = int(row["actual_tx_slot"])
        feedback = int(row["feedback_available_slot"])
        prb = int(row["prb"])
        if (row["access"] != "grant_free" or not 0 <= prb < 6 or
                feedback != slot + 1 or int(row["slot"]) != slot or
                int(row["same_prb_occupancy"]) < 1):
            raise RuntimeError(f"attempt timing/CB domain mismatch: {directory}")
        by_payload[payload].append(row)
        by_episode[(payload, row["harq_episode_id"])].append(row)
        measured_per_slot[slot] += 1
    for packet in packets:
        payload = int(packet["payload_id"])
        group = by_payload[payload]
        rlc = int(packet["rlc_retx_count"])
        if (rlc not in range(7) or
                int(packet["num_harq_episodes"]) != rlc + 1 or
                int(packet["total_phy_attempts"]) != len(group) or
                any(int(row["actual_tx_slot"]) >
                    int(packet["completion_slot"]) for row in group)):
            raise RuntimeError(f"packet episode/count mismatch: {directory}, {payload}")
        correct = packet["delivered_correctly"] == "True"
        drop = packet["oracle_terminal_status"] == "protocol_drop"
        undetected = packet["undetected_error"] == "True"
        if sum((correct, drop, undetected)) != 1:
            raise RuntimeError(f"terminal status partition: {directory}, {payload}")
    for key, group in by_episode.items():
        ordered = sorted(group, key=lambda row: int(row["actual_tx_slot"]))
        if len(ordered) > 4 or [int(row["attempt_index"]) for row in ordered] != list(
                range(1, len(ordered) + 1)) or [int(row["rv"]) for row in ordered] != list(
                RV_SEQUENCE[:len(ordered)]):
            raise RuntimeError(f"HARQ attempt/RV mismatch: {directory}, {key}")
        if any(int(curr["actual_tx_slot"]) - int(prev["actual_tx_slot"]) < 2
               for prev, curr in zip(ordered, ordered[1:])):
            raise RuntimeError(f"HARQ PUSCH spacing mismatch: {directory}, {key}")
        if any(int(row["rlc_retx_index"]) != int(ordered[0]["rlc_retx_index"])
               for row in ordered):
            raise RuntimeError(f"RLC index changed within HARQ episode: {key}")
    for row in slots:
        slot = int(row["slot"])
        if measured_per_slot[slot] > int(row["cb_attempt_count"]):
            raise RuntimeError(f"measured attempts exceed total slot work: {directory}")
    decisions = rows(directory / "reinjection_decisions.csv.gz") if treatment else []
    if treatment:
        observed = {(int(row["payload_id"]), int(row["recovery_index"])):
                    int(row["actual_first_rv0_slot"]) for row in decisions}
        if len(observed) != len(decisions):
            raise RuntimeError(f"duplicate reinjection decision: {directory}")
        for row in decisions:
            b = int(row["additional_opportunities_skipped"])
            first = int(row["original_first_opportunity"])
            target = int(row["target_opportunity"])
            if (b not in range(4) or target != first + b or
                    observed[(int(row["payload_id"]),
                              int(row["recovery_index"]))] != target):
                raise RuntimeError(f"backoff opportunity mismatch: {directory}")
        measured_decisions = sum(row["measurement_cohort"] == "True"
                                 for row in decisions)
        if measured_decisions != sum(int(row["rlc_retx_count"])
                                     for row in packets):
            raise RuntimeError(f"measured recovery count mismatch: {directory}")
        if (manifest["implementation_sha"] !=
                "c3119e74f4cb5db8c9d8523b80b489549063287d" or
                manifest["dirty_worktree"] or not manifest["production"]):
            raise RuntimeError(f"production provenance mismatch: {directory}")
    return {"seed": seed, "policy": "backoff" if treatment else "reference",
            "packets_checked": len(packets),
            "attempts_checked": len(attempts),
            "harq_episodes_checked": len(by_episode),
            "slots_checked": len(slots),
            "reinjection_decisions_checked": len(decisions),
            "invariant_violations": 0}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-root", type=Path,
                        default=Path("results/raw_v0_5"))
    parser.add_argument("--test-root", type=Path,
                        default=Path("results/raw_v0_8_test_d"))
    parser.add_argument("--output", type=Path,
                        default=Path("results/v0_8_test_d/integrity_audit.csv"))
    args = parser.parse_args()
    output = [check(args.reference_root / BASE / f"seed_{seed}", seed,
                    treatment=False) for seed in SEEDS]
    output.extend(check(args.test_root / TEST / f"seed_{seed}", seed,
                        treatment=True) for seed in SEEDS)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(output[0]),
                                lineterminator="\n")
        writer.writeheader()
        writer.writerows(output)
    print(json.dumps({"classification": "TEST-D-INTEGRITY-PASS",
                      "runs_checked": len(output),
                      "new_production_runs": sum(row["policy"] == "backoff"
                                                 for row in output),
                      "violations": 0,
                      "output_sha256": sha256(args.output)}, sort_keys=True))


if __name__ == "__main__":
    main()
