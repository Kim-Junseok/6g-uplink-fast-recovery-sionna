#!/usr/bin/env python3
"""Execute the single frozen Fast-CB Test D cell and its B=0 regression."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import importlib.util
import io
import json
import math
import platform
import subprocess
import sys
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

import torch

from ul_access.config import load_yaml
from ul_access.protocol import Packet  # Initialize the protocol/recovery import cycle.
from ul_access.recovery import FastCbReinjectionBackoffPolicy
from ul_access.recovery.fast_cb_backoff import BACKOFF_RNG_DOMAIN

SPEC = importlib.util.spec_from_file_location(
    "commmag_p2_campaign", Path("experiments/24_commmag_priority_campaign.py"))
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("accepted campaign implementation is unavailable")
P2 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(P2)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def csv_bytes(rows: list[dict]) -> bytes:
    if not rows:
        raise RuntimeError("empty Test D output table")
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode()


def write_gzip(path: Path, payload: bytes) -> None:
    with path.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, filename="", mode="wb", mtime=0) as output:
            output.write(payload)


def rows(path: Path) -> list[dict[str, str]]:
    with gzip.open(path, "rt", newline="") as stream:
        return list(csv.DictReader(stream))


def verify_b0(directory: Path, reference: Path) -> dict:
    """Require exact protocol outputs; bound CPU PHY diagnostic roundoff."""
    metadata = {"scenario_id", "config_hash", "code_commit", "scheme"}
    numeric_diagnostics = {"post_lmmse_sinr_min_db",
                           "post_lmmse_sinr_mean_db",
                           "post_lmmse_sinr_max_db", "eesm_sinr_db"}
    max_abs_db = {name: 0.0 for name in numeric_diagnostics}
    row_counts = {}
    for name in ("packets", "attempts", "slot_prb", "recovery_timing"):
        old, new = rows(reference / f"{name}.csv.gz"), rows(directory / f"{name}.csv.gz")
        if len(old) != len(new):
            raise RuntimeError(f"B=0 regression row-count mismatch in {name}")
        row_counts[name] = len(old)
        if not old:
            continue
        common = set(old[0]) & set(new[0]) - metadata
        for index, (before, after) in enumerate(zip(old, new)):
            for key in common:
                if before[key] == after[key]:
                    continue
                if key in numeric_diagnostics:
                    x, y = float(before[key]), float(after[key])
                    error = abs(x - y)
                    if not math.isfinite(error) or error > 0.001:
                        raise RuntimeError(
                            f"B=0 PHY diagnostic exceeds 0.001 dB in "
                            f"{name} row {index} field {key}: {error}")
                    max_abs_db[key] = max(max_abs_db[key], error)
                else:
                    raise RuntimeError(
                        f"B=0 protocol/output mismatch in {name} row {index} "
                        f"field {key}: {before[key]!r} != {after[key]!r}")
    return {"status": "PROTOCOL-EXACT-PHY-DIAGNOSTIC-BOUNDED",
            "row_counts": row_counts,
            "maximum_absolute_phy_diagnostic_difference_db": max_abs_db,
            "phy_diagnostic_tolerance_db": 0.001}


def frozen_inputs(config_path: Path) -> tuple[dict, dict]:
    spec = load_yaml(config_path)
    baseline_path = Path(spec["baseline_config"])
    if sha256(baseline_path) != spec["baseline_config_sha256"]:
        raise RuntimeError("accepted V0.5 configuration hash changed")
    baseline = load_yaml(baseline_path)
    scenario = spec["baseline_scenario"]
    if (spec["seeds"] != list(range(9101, 9109)) or
            spec["additional_eligible_cb_opportunities"] != [0, 1, 2, 3] or
            spec["backoff_rng_domain"] != BACKOFF_RNG_DOMAIN or
            spec["cb_opportunity_period_slots"] != 1 or
            spec["rho"] != 0.90 or spec["channel"] != "Medium" or
            spec["traffic"] != "independent_per_ue_poisson" or
            scenario != "V0_5_RHO090_FAST_CB" or
            baseline["scenarios"][scenario]["scheme"] != "Fast-CB" or
            baseline["channel"]["operating_point"] != "Medium"):
        raise RuntimeError("Test D frozen contract mismatch")
    return spec, baseline


def execute(config_path: Path, output_root: Path, seed: int, *,
            forced_b_zero: bool = False, allow_dirty: bool = False,
            check_b0: bool = False) -> dict:
    spec, baseline = frozen_inputs(config_path)
    if seed not in spec["seeds"]:
        raise RuntimeError("seed outside Test D campaign")
    P2.validate_p2_contract(baseline, spec["baseline_scenario"], seed)
    dirty = P2.git_dirty()
    if dirty and not allow_dirty:
        raise RuntimeError("Test D production requires a clean committed SHA")
    if forced_b_zero and not allow_dirty:
        raise RuntimeError("forced B=0 is an engineering regression mode only")
    if check_b0 and not forced_b_zero:
        raise RuntimeError("baseline comparison requires forced B=0")
    if not forced_b_zero and allow_dirty:
        raise RuntimeError("dirty development mode is limited to forced B=0")
    scenario = spec["scenario_id"]
    directory = output_root / scenario / f"seed_{seed}"
    if directory.exists():
        raise FileExistsError(f"refusing to overwrite Test D artifact: {directory}")
    cfg = deepcopy(baseline)
    cfg["scenarios"][scenario] = deepcopy(cfg["scenarios"][spec["baseline_scenario"]])
    policy = FastCbReinjectionBackoffPolicy(
        failure_indication_delay_slots=int(cfg["recovery"]["fast_arq_delay_slots"]),
        seed=seed,
        opportunity_period_slots=int(spec["cb_opportunity_period_slots"]),
        forced_backoff=0 if forced_b_zero else None)
    started = datetime.now(timezone.utc).isoformat()
    result, timing, traffic = P2.p2_build_run(
        cfg, scenario, seed, recovery_override=policy)
    commit = P2.git_sha()
    resolved = {"baseline_config_sha256": spec["baseline_config_sha256"],
                "test_d_config_sha256": sha256(config_path),
                "scenario_id": scenario, "seed": seed,
                "forced_b_zero": forced_b_zero}
    config_hash = hashlib.sha256(P2.canonical(resolved)).hexdigest()
    packets, attempts, slots, _ = P2.p2_output_rows(
        result, cfg, scenario, seed, config_hash, commit)
    if (len(packets) != 2500 or result["packets"]["unfinished_after_drain"] or
            not all(row["pool_invariant"] for row in slots)):
        raise RuntimeError("Test D packet count, drain, or resource invariant failed")
    delivered = sum(row["delivered_correctly"] for row in packets)
    dropped = sum(row["oracle_terminal_status"] == "protocol_drop"
                  for row in packets)
    undetected = sum(row["undetected_error"] for row in packets)
    if delivered + dropped + undetected != 2500:
        raise RuntimeError("Test D terminal outcome partition failed")
    rv_sequence = (0, 2, 3, 1)
    for attempt in attempts:
        idx = int(attempt["attempt_index"])
        if (idx not in range(1, 5) or int(attempt["rv"]) != rv_sequence[idx - 1]
                or attempt["access"] != "grant_free"):
            raise RuntimeError("Test D HARQ RV or resource-domain invariant failed")
    first_tx = {}
    for event in result["event_trace"]:
        if (event["event"] == "phy_transmission" and
                event["harq_attempt"] == 1 and
                event["rlc_retx_index"] > 0):
            key = (event["payload_id"], event["rlc_retx_index"])
            if key in first_tx:
                raise RuntimeError("duplicate fresh RV0 in Test D event trace")
            first_tx[key] = event
    decision_rows = []
    for decision in policy.decisions:
        key = (decision.payload_id, decision.recovery_index)
        event = first_tx.get(key)
        if event is None or event["slot"] != decision.target_opportunity:
            raise RuntimeError(f"Test D opportunity timing mismatch: {key}")
        if event["redundancy_version"] != 0:
            raise RuntimeError(f"Test D reinjection is not fresh RV0: {key}")
        decision_rows.append({**asdict(decision),
                              "actual_first_rv0_slot": event["slot"],
                              "measurement_cohort": decision.payload_id >= 500})
    if len(decision_rows) != len(first_tx):
        raise RuntimeError("Test D reinjection decision/PHY trace count mismatch")
    summary = P2.p2_summary(packets, attempts, slots, result, timing, traffic, cfg)
    summary["test_d"] = {
        "forced_b_zero": forced_b_zero,
        "reinjection_decisions_all_packets": len(decision_rows),
        "reinjection_decisions_measured_packets": sum(
            row["measurement_cohort"] for row in decision_rows),
        "backoff_histogram_all_packets": {str(b): sum(
            row["additional_opportunities_skipped"] == b
            for row in decision_rows) for b in range(4)}}
    directory.mkdir(parents=True)
    tables = {"packets.csv.gz": P2.p2_csv_bytes(packets),
              "attempts.csv.gz": P2.p2_csv_bytes(attempts),
              "slot_prb.csv.gz": P2.p2_csv_bytes(slots),
              "reinjection_decisions.csv.gz": csv_bytes(decision_rows)}
    # V0.5 Fast-CB timing table is empty; retain it for baseline compatibility.
    write_gzip(directory / "recovery_timing.csv.gz", b"")
    for name, payload in tables.items():
        write_gzip(directory / name, payload)
    (directory / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n")
    b0_report = None
    if check_b0:
        reference = Path(spec["reference_raw_root"]) / f"seed_{seed}"
        b0_report = verify_b0(directory, reference)
    file_hashes = {path.name: sha256(path) for path in directory.iterdir()
                   if path.name != "run_manifest.json"}
    manifest = {
        "schema_version": 1, "milestone": spec["milestone"],
        "scenario_id": scenario, "seed": seed,
        "implementation_sha": commit, "dirty_worktree": dirty,
        "production": not forced_b_zero,
        "config_path": str(config_path), "config_sha256": sha256(config_path),
        "baseline_config_path": spec["baseline_config"],
        "baseline_config_sha256": spec["baseline_config_sha256"],
        "configuration_hash": config_hash,
        "backoff_rng_domain": BACKOFF_RNG_DOMAIN,
        "forced_b_zero": forced_b_zero,
        "environment": {"python_executable": sys.executable,
                        "python": platform.python_version(),
                        "sionna": version("sionna"),
                        "torch": torch.__version__,
                        "device": "cpu", "worker_count": 1,
                        "deterministic_algorithms": True,
                        "torch_num_threads": 1},
        "started_utc": started,
        "ended_utc": datetime.now(timezone.utc).isoformat(),
        "runtime": timing, "output_sha256": file_hashes,
        "b0_reference_comparison_pass": check_b0,
        "b0_reference_comparison": b0_report}
    (directory / "run_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path,
                        default=Path("configs/fastcb_reinjection_backoff_v0_8.yaml"))
    parser.add_argument("--output-root", type=Path,
                        default=Path("results/raw_v0_8_test_d"))
    parser.add_argument("--all-main-seeds", action="store_true")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--forced-b-zero", action="store_true")
    parser.add_argument("--allow-dirty-development", action="store_true")
    parser.add_argument("--check-b0-against-reference", action="store_true")
    args = parser.parse_args()
    spec, _ = frozen_inputs(args.config)
    if args.all_main_seeds == (args.seed is not None):
        parser.error("select exactly one of --all-main-seeds and --seed")
    if args.forced_b_zero and args.all_main_seeds:
        parser.error("B=0 engineering check is limited to one selected seed")
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)
    chosen = spec["seeds"] if args.all_main_seeds else [args.seed]
    for seed in chosen:
        manifest = execute(
            args.config, args.output_root, seed,
            forced_b_zero=args.forced_b_zero,
            allow_dirty=args.allow_dirty_development,
            check_b0=args.check_b0_against_reference)
        print(json.dumps({"seed": seed, "implementation_sha":
                          manifest["implementation_sha"],
                          "output": str(args.output_root / spec["scenario_id"] /
                                        f"seed_{seed}"),
                          "b0_reference_comparison_pass":
                          manifest["b0_reference_comparison_pass"]},
                         sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
