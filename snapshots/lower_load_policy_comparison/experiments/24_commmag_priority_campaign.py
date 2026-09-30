#!/usr/bin/env python3
"""Run one V0.4g P1 engineering scenario or the five-scenario validation set."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import platform
import resource
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

import torch

from ul_access.config import load_yaml
from ul_access.phy import (ActiveCompactedFrequencySelectivePrbReceiver,
                           DirectLlsHarqBackend, FrequencySelectivePrbReceiver,
                           RNG_CONTRACT, audit_controlled_pusch_geometry)
from ul_access.protocol import (CountedPoissonTrafficGenerator,
    EventAddressedBernoulliTraffic,
    EventAddressedGrantFreeAccess, SimulationParameters, SlotSimulator)
from ul_access.recovery import (FastArqPolicy, HarqController,
    LegacyRlcScheduledPolicy, PollRetransmitPolicy, ScheduledRescueConfig,
    ScheduledRescueMode)
from ul_access.resource import scheduled_resource_policy_from_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path,
                        default=Path("configs/commmag_priority_v0_4g.yaml"))
    parser.add_argument("--list-scenarios", action="store_true")
    parser.add_argument("--scenario-id")
    parser.add_argument("--seed", type=int, default=240401)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--resolved-config", action="store_true")
    parser.add_argument("--verify-replay", action="store_true",
                        help="rerun selected scenarios and compare raw outputs")
    parser.add_argument("--group", choices=(
        "P2", "P2.5", "LEGACY-TIMING", "V0.5-RB", "V0.6-P3",
        "V0.6-K3", "V0.6-F3", "V0.7-RHO070"))
    parser.add_argument("--all-main-seeds", action="store_true")
    parser.add_argument("--allow-dirty-development", action="store_true")
    return parser.parse_args()


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_sha() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True).strip()


def git_dirty() -> bool:
    return bool(subprocess.check_output(
        ["git", "status", "--porcelain"], text=True).strip())


def deterministic_gzip(path: Path, payload: bytes) -> None:
    with path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as stream:
            stream.write(payload)


def csv_bytes(rows: list[dict]) -> bytes:
    fields = ["run_id", "scenario_id", "seed", "scheme", "ue_id",
              "payload_id", "generation_slot", "first_tx_slot",
              "completion_slot", "completion_latency_slots", "delivered",
              "drop_reason", "number_of_phy_attempts",
              "number_of_harq_episodes", "number_of_rlc_retransmissions",
              "fast_arq_triggered", "fast_rescue_triggered",
              "scheduled_queue_delay_slots", "scheduled_transmission_count"]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n",
                            extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode()


def build_run(cfg: dict, scenario_id: str, seed: int) -> tuple[dict, dict]:
    p1, channel = cfg["p1"], cfg["channel"]
    scenario = cfg["scenarios"][scenario_id]
    num_ues = int(p1["num_ues"])
    receiver = FrequencySelectivePrbReceiver({
        "num_ues": num_ues,
        "num_prbs": int(cfg["resource_pool"]["num_prbs"]),
        "subcarrier_spacing_hz": cfg["resource_pool"]["subcarrier_spacing_hz"],
        "num_bs_receive_antennas": channel["num_bs_receive_antennas"],
        "noise_power_w": channel["noise_power_w"],
        "carrier_frequency_hz": channel["carrier_frequency_hz"],
        "delay_spread_s": channel["delay_spread_s"],
    })
    backend = DirectLlsHarqBackend(
        receiver, run_seed=seed,
        decoder_iterations=int(cfg["phy"]["decoder_iterations"]))
    traffic = EventAddressedBernoulliTraffic(
        num_ues=num_ues,
        arrival_probability_per_ue=float(p1["arrival_probability_per_ue_per_slot"]),
        packet_size_bits=160, seed=seed)
    access = EventAddressedGrantFreeAccess(
        seed=seed, resource_pool_size=int(cfg["resource_pool"]["num_prbs"]),
        opportunity_period_slots=1, max_retries=3,
        retry_backoff_slots=int(p1["cb_retry_backoff_slots"]))
    recovery = (PollRetransmitPolicy(
        poll_retransmit_slots=int(p1["poll_retransmit_slots"]))
        if scenario["recovery"] == "legacy" else
        FastArqPolicy(failure_indication_delay_slots=int(p1["fast_arq_delay_slots"])))
    rescue = None
    scheduled_rlc_recovery = scenario["scheduled_rescue"] == "higher_layer_fresh_tb"
    if scenario["scheduled_rescue"] is not None:
        mode = (ScheduledRescueMode.HARQ_PRESERVING
                if scenario["scheduled_rescue"] == "harq_preserving"
                else ScheduledRescueMode.FRESH_TB)
        rescue = ScheduledRescueConfig(
            mode=mode,
            request_delay_slots=int(p1["rescue_request_delay_slots"]),
            scheduled_grant_delay_slots=int(p1["rescue_grant_delay_slots"]),
            trigger_after_attempts=(5 if scheduled_rlc_recovery else
                                    int(p1["rescue_trigger_after_attempts"])),
            maximum_physical_attempts_per_recovery_episode=4)
    started = time.perf_counter()
    result = SlotSimulator(
        parameters=SimulationParameters(
            duration_slots=int(p1["duration_slots"]), num_ues=num_ues,
            seed=seed, tx_power_dbm=(float(p1["tx_power_dbm"]),) * num_ues,
            mcs_index=(10,) * num_ues,
            max_drain_slots=int(p1["max_drain_slots"])),
        traffic=traffic, access=access, phy_backend=backend,
        harq=HarqController(max_attempts=4,
                            feedback_delay_slots=int(p1["feedback_delay_slots"])),
        recovery=recovery,
        max_rlc_retransmissions=int(p1["maximum_rlc_retransmissions"]),
        scheduled_rescue=rescue,
        scheduled_rlc_recovery=scheduled_rlc_recovery,
        extended_outputs=True,
        event_addressed_scheduler_ties=True).run()
    wall = time.perf_counter() - started
    attempts = int(result["recovery"]["total_phy_transmissions"])
    lls = backend.timing_seconds
    radio = receiver.timing_seconds
    protocol = max(0.0, wall - radio - lls)
    timing = {"wall_seconds": wall, "phy_attempts": attempts,
              "phy_attempts_per_second": attempts / wall if wall else None,
              "projected_wall_seconds_per_1000_attempts":
                  1000 * wall / attempts if attempts else None,
              "projected_wall_seconds_per_10000_attempts":
                  10000 * wall / attempts if attempts else None,
              "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
              "fractions": {"channel_lmmse_eesm": radio / wall,
                            "direct_sionna_lls": lls / wall,
                            "protocol_event_simulation": protocol / wall}}
    return result, timing


def write_run(cfg: dict, scenario_id: str, seed: int,
              root: Path) -> dict:
    started_utc = datetime.now(timezone.utc).isoformat()
    resolved = {"scenario_id": scenario_id, "seed": seed,
                "base": cfg, "scenario": cfg["scenarios"][scenario_id]}
    config_hash = hashlib.sha256(canonical(resolved)).hexdigest()
    run_id = f"{scenario_id}-seed-{seed}"
    directory = root / scenario_id / str(seed)
    directory.mkdir(parents=True, exist_ok=True)
    result, timing = build_run(cfg, scenario_id, seed)
    rows = [{"run_id": run_id, "scenario_id": scenario_id, "seed": seed,
             "scheme": cfg["scenarios"][scenario_id]["scheme"], **row}
            for row in result["packet_rows"]]
    packet_path = directory / "packets.csv.gz"
    trace_path = directory / "events.jsonl.gz"
    result_path = directory / "result.json"
    manifest_path = directory / "manifest.json"
    deterministic_gzip(packet_path, csv_bytes(rows))
    deterministic_gzip(trace_path, b"".join(
        canonical(event) + b"\n" for event in result["event_trace"]))
    serializable_result = {key: value for key, value in result.items()
                           if key not in {"event_trace", "packet_rows"}}
    result_path.write_text(json.dumps(serializable_result, indent=2,
                                      sort_keys=True) + "\n")
    serialization_started = time.perf_counter()
    output_hashes = {path.name: sha256(path)
                     for path in (packet_path, trace_path, result_path)}
    serialization = time.perf_counter() - serialization_started
    total = timing["wall_seconds"] + serialization
    component_seconds = {
        "channel_lmmse_eesm": timing["fractions"]["channel_lmmse_eesm"] *
            timing["wall_seconds"],
        "direct_sionna_lls": timing["fractions"]["direct_sionna_lls"] *
            timing["wall_seconds"],
        "protocol_event_simulation": timing["fractions"]["protocol_event_simulation"] *
            timing["wall_seconds"],
        "output_serialization": serialization}
    timing["fractions"] = {name: seconds / total
                           for name, seconds in component_seconds.items()}
    manifest = {"schema_version": 1, "milestone": "V0.4g-P1",
        "scenario_id": scenario_id, "seed": seed, "run_id": run_id,
        "git_sha": git_sha(), "resolved_configuration": resolved,
        "configuration_hash": config_hash, "rng_contract": RNG_CONTRACT,
        "environment": {"python_executable": sys.executable,
            "python": platform.python_version(), "sionna": version("sionna"),
            "torch": torch.__version__, "device": "cpu",
            "deterministic_algorithms": True, "torch_num_threads": 1},
        "packet_count": len(rows),
        "phy_attempt_count": timing["phy_attempts"],
        "started_utc": started_utc,
        "ended_utc": datetime.now(timezone.utc).isoformat(),
        "output_sha256": output_hashes, "runtime": timing}
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def validation_markdown(artifact: dict) -> str:
    lines = ["# V0.4g direct-LLS SLS validation", "",
        "This P0/P1 run checks the integrated mechanism. It does not report paper-level protocol performance.", "",
        f"Classification: `{artifact['classification']}`", "",
        "|Scenario|Packets|PHY attempts|Attempts/s|CB PRBs|Scheduled PRBs|",
        "|:--|--:|--:|--:|--:|--:|"]
    for row in artifact["runs"]:
        lines.append(f"|{row['scenario_id']}|{row['packet_count']}|"
                     f"{row['phy_attempt_count']}|"
                     f"{row['runtime']['phy_attempts_per_second']:.3f}|"
                     f"{row['resource_accounting']['cb_prb_use']}|"
                     f"{row['resource_accounting']['scheduled_prb_use']}|")
    lines.extend(["", "The runtime projection scales the measured integrated attempt rate. It is an engineering estimate, not a completed priority-campaign result.", ""])
    replay = artifact.get("deterministic_replay")
    if replay is not None:
        lines.extend([f"Deterministic raw-output replay: `{replay['status']}`.", ""])
    lines.extend(["Trace audits: " + ", ".join(
        f"{name}={str(value).lower()}" for name, value in
        artifact.get("trace_audits", {}).items()), ""])
    return "\n".join(lines)


def load_events(root: Path, scenario: str, seed: int) -> list[dict]:
    path = root / scenario / str(seed) / "events.jsonl.gz"
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return [json.loads(line) for line in stream]


def trace_audits(root: Path, seed: int) -> dict[str, bool]:
    by_scenario = {name: load_events(root, name, seed) for name in (
        "p1-legacy-cb", "p1-fast-cb", "p1-b", "p1-h", "p1-f")}
    legacy = by_scenario["p1-legacy-cb"]
    fast = by_scenario["p1-fast-cb"]
    b_events = by_scenario["p1-b"]
    h_events = by_scenario["p1-h"]
    f_events = by_scenario["p1-f"]

    def transmissions(events: list[dict]) -> list[dict]:
        return [event for event in events if event["event"] == "phy_transmission"]

    def episode_sequences(events: list[dict]) -> dict[str, list[dict]]:
        output: dict[str, list[dict]] = {}
        for event in transmissions(events):
            output.setdefault(event["harq_episode_id"], []).append(event)
        return output

    b_sequences = episode_sequences(b_events)
    b_cb = any([e["redundancy_version"] for e in rows] == [0, 2, 3, 1]
               and all(e["transmission_access"] == "grant_free" for e in rows)
               for rows in b_sequences.values())
    b_scheduled = any(rows[0]["redundancy_version"] == 0 and
                      rows[0]["transmission_access"] == "scheduled_rescue"
                      for rows in b_sequences.values())
    h_preserve = any(
        [e["redundancy_version"] for e in rows] == [0, 2, 3, 1] and
        [e["transmission_access"] for e in rows] ==
            ["grant_free", "grant_free", "scheduled_rescue", "scheduled_rescue"]
        for rows in episode_sequences(h_events).values())
    f_reset = any(e["transmission_access"] == "scheduled_rescue" and
                  e["rv_history"] == [0] for e in transmissions(f_events)) and any(
        e["event"] == "harq_episode_abandoned_for_rescue" for e in f_events)
    conditioned = True
    for events in by_scenario.values():
        acked: set[str] = set()
        last_attempt: dict[str, int] = {}
        for event in events:
            episode = event.get("harq_episode_id")
            if event["event"] == "harq_ack":
                acked.add(episode)
            elif event["event"] == "phy_transmission":
                if episode in acked:
                    conditioned = False
                attempt = len(event["rv_history"])
                if attempt != last_attempt.get(episode, 0) + 1:
                    conditioned = False
                last_attempt[episode] = attempt
    audits = {
        "legacy_timer_trace": any(e["event"] == "legacy_poll_retransmit_expiry"
                                  for e in legacy),
        "fast_cb_trace": any(e["event"] == "fast_arq_failure_indication"
                             for e in fast),
        "b_cb_then_scheduled_fresh": b_cb and b_scheduled and any(
            e["event"] == "scheduled_rlc_recovery_ready" for e in b_events),
        "h_episode_and_llr_preserved": h_preserve,
        "f_episode_and_llr_reset": f_reset,
        "sequential_prior_nack_conditioning": conditioned,
    }
    if not all(audits.values()):
        raise RuntimeError(f"P1 trace audit failed: {audits}")
    return audits


def replay_check(cfg: dict, scenarios: tuple[str, ...], seed: int,
                 primary_root: Path) -> dict:
    checked = []
    with tempfile.TemporaryDirectory(prefix="v04g-replay-", dir="/tmp") as raw:
        replay_root = Path(raw)
        for scenario in scenarios:
            write_run(cfg, scenario, seed, replay_root)
            for filename in ("packets.csv.gz", "events.jsonl.gz"):
                first = primary_root / scenario / str(seed) / filename
                second = replay_root / scenario / str(seed) / filename
                if first.read_bytes() != second.read_bytes():
                    raise RuntimeError(f"deterministic replay mismatch: {scenario}/{filename}")
                checked.append({"scenario_id": scenario, "file": filename,
                                "sha256": sha256(first)})
    return {"status": "BYTE-IDENTICAL", "files": checked}


def p2_csv_bytes(rows: list[dict]) -> bytes:
    if not rows:
        return b""
    fields = list(rows[0])
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode()


def percentile(values: list[int], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (position - lower) * (ordered[upper] - ordered[lower])


def scheduled_queue_summary(slots: list[dict], packets: list[dict]) -> dict:
    """Summarize explicit queue state and first-service waiting time."""
    queue_lengths = [int(row["scheduled_queue_length_before_service"])
                     for row in slots]
    waiting = [
        int(row["scheduled_first_tx_slot"]) -
        int(row["scheduled_queue_entry_slot"])
        for row in packets
        if row.get("scheduled_first_tx_slot") is not None and
        row.get("scheduled_queue_entry_slot") is not None]
    services = [int(row["scheduled_service_count"]) for row in slots]
    capacities = [int(row["scheduled_service_capacity"]) for row in slots]
    requests = sum(int(row["scheduled_requests_in_slot"]) for row in slots)
    residual_counts = {
        str(prbs): sum(int(row["cb_prbs_available"]) == prbs for row in slots)
        for prbs in range(7)}
    return {
        "population": "all recorded slots including terminal drain",
        "queue_length": {
            "mean": statistics.fmean(queue_lengths) if queue_lengths else None,
            "median": statistics.median(queue_lengths) if queue_lengths else None,
            "p95": percentile(queue_lengths, 0.95),
            "maximum": max(queue_lengths, default=None),
        },
        "first_service_wait_slots": {
            "mean": statistics.fmean(waiting) if waiting else None,
            "p95": percentile(waiting, 0.95),
            "maximum": max(waiting, default=None),
        },
        "scheduled_requests_per_slot": requests / len(slots) if slots else None,
        "scheduled_service_per_slot": (
            sum(services) / len(slots) if slots else None),
        "fraction_slots_with_scheduled_backlog": (
            sum(value > 0 for value in queue_lengths) / len(slots)
            if slots else None),
        "fraction_slots_at_scheduled_service_capacity": (
            sum(service == capacity for service, capacity in
                zip(services, capacities)) / len(slots) if slots else None),
        "residual_cb_prb_counts": residual_counts,
        "residual_cb_prb_fractions": {
            key: value / len(slots) if slots else None
            for key, value in residual_counts.items()},
    }


def legacy_timing_summary(packets: list[dict]) -> dict:
    """Summarize initial Legacy-CB recovery timing for measured packets."""
    rows = [row for row in packets
            if row.get("legacy_recovery_eligible_slot") is not None]

    def distribution(key: str) -> dict:
        values = [int(row[key]) for row in rows if row.get(key) is not None]
        return {
            "count": len(values),
            "mean": statistics.fmean(values) if values else None,
            "median": statistics.median(values) if values else None,
            "p95": percentile(values, 0.95),
            "maximum": max(values, default=None),
        }

    timer_binding = [
        int(row["legacy_timer_expiry_slot"]) >
        int(row["initial_harq_final_failure_slot"])
        for row in rows]
    return {
        "population": "measurement-cohort packets entering Legacy-CB recovery",
        "packets": len(rows),
        "timer_binding_fraction": (
            sum(timer_binding) / len(timer_binding) if timer_binding else None),
        "effective_final_harq_to_recovery_gap": distribution(
            "effective_final_harq_to_recovery_gap"),
        "timer_wait_slots": distribution("legacy_timer_wait_slots"),
        "cb_resource_wait_slots": distribution("cb_resource_wait_slots"),
        "first_recovery_harq_duration_slots": distribution(
            "first_recovery_harq_duration_slots"),
    }


def p2_recovery_policy(recovery_cfg: dict, scenario: dict):
    """Build recovery while keeping the legacy proxy outside fast paths."""
    if scenario["scheme"] == "Legacy-RLC/SB":
        timer = int(scenario.get(
            "legacy_poll_retransmit_slots",
            recovery_cfg["legacy_poll_retransmit_slots"]))
        return LegacyRlcScheduledPolicy(
            poll_retransmit_slots=timer,
            sr_period_slots=int(recovery_cfg["legacy_sr_period_slots"]),
            sr_control_processing_slots=int(
                recovery_cfg["sr_control_processing_slots"]),
            scheduled_pusch_k2_slots=int(
                recovery_cfg["scheduled_pusch_k2_slots"]),
            sr_phase_domain=str(recovery_cfg["legacy_sr_phase_domain"]))
    if scenario["scheme"] == "Legacy-CB":
        timer = int(scenario.get(
            "legacy_poll_retransmit_slots",
            recovery_cfg["legacy_poll_retransmit_slots"]))
        return PollRetransmitPolicy(poll_retransmit_slots=timer)
    return FastArqPolicy(
        failure_indication_delay_slots=int(recovery_cfg["fast_arq_delay_slots"]))


def p2_build_run(cfg: dict, scenario_id: str, seed: int) -> tuple[dict, dict, object]:
    scenario = cfg["scenarios"][scenario_id]
    channel, traffic_cfg = cfg["channel"], cfg["traffic"]
    recovery_cfg, stopping = cfg["recovery"], cfg["stopping"]
    power = channel.get("transmit_power_dbm")
    if power is None:
        raise RuntimeError("Medium transmit power is not frozen")
    num_ues = int(traffic_cfg["num_ues"])
    rho_key = f"{float(scenario['rho']):.2f}"
    traffic = CountedPoissonTrafficGenerator(
        num_ues=num_ues,
        rate_per_ue_per_slot=float(
            traffic_cfg["per_ue_arrival_rate_per_slot"][rho_key]),
        warmup_packets=int(stopping["warmup_new_packets"]),
        measured_packets=int(stopping["measured_new_packets"]),
        packet_size_bits=int(cfg["phy"]["information_bits"]), seed=seed)
    receiver = ActiveCompactedFrequencySelectivePrbReceiver({
        "num_ues": num_ues,
        "num_prbs": int(cfg["resource_pool"]["num_prbs"]),
        "subcarrier_spacing_hz": float(cfg["resource_pool"]["subcarrier_spacing_hz"]),
        "num_bs_receive_antennas": int(channel["num_bs_receive_antennas"]),
        "noise_power_w": float(channel["noise_power_w"]),
        "carrier_frequency_hz": float(channel["carrier_frequency_hz"]),
        "delay_spread_s": float(channel["delay_spread_s"]),
    })
    backend = DirectLlsHarqBackend(
        receiver, run_seed=seed,
        decoder_iterations=int(cfg["phy"]["decoder_iterations"]))
    access = EventAddressedGrantFreeAccess(
        seed=seed, resource_pool_size=int(cfg["resource_pool"]["num_prbs"]),
        opportunity_period_slots=1, max_retries=3,
        retry_backoff_slots=int(recovery_cfg["cb_retry_backoff_slots"]))
    is_scheduled = scenario["scheme"] in {
        "Legacy-RLC/SB", "B/Fast-SB", "H", "F", "H(K=3)", "F(K=3)"}
    direct_timing = cfg.get("milestone") in {
        "V0.5-RB", "V0.6-P3", "V0.6-K3", "V0.6-F3", "V0.7-RHO070"}
    resource_policy_name = scenario.get("resource_policy")
    resource_policy_config = (
        cfg.get("resource_policies", {}).get(resource_policy_name)
        if resource_policy_name is not None else None)
    resource_policy = scheduled_resource_policy_from_config(
        resource_policy_config,
        base_cb_prbs=int(cfg["resource_pool"]["num_prbs"]))
    recovery = p2_recovery_policy(recovery_cfg, scenario)
    rescue_mode = ScheduledRescueMode(
        scenario.get("rescue_mode", ScheduledRescueMode.FRESH_TB.value))
    rescue = (ScheduledRescueConfig(
        mode=rescue_mode,
        request_delay_slots=int(recovery_cfg.get(
            "scheduled_request_delay_slots", 0)),
        scheduled_grant_delay_slots=int(recovery_cfg.get(
            "scheduled_grant_delay_slots", 0)),
        trigger_after_attempts=int(scenario.get("trigger_after_attempts", 5)),
        maximum_physical_attempts_per_recovery_episode=int(scenario.get(
            "maximum_physical_attempts_per_recovery_episode", 4)))
        if is_scheduled else None)
    started = time.perf_counter()
    result = SlotSimulator(
        parameters=SimulationParameters(
            duration_slots=traffic.duration_slots, num_ues=num_ues, seed=seed,
            tx_power_dbm=(float(power),) * num_ues,
            mcs_index=(int(cfg["phy"]["mcs_index"]),) * num_ues,
            max_drain_slots=int(stopping["hard_max_drain_slots"]),
            warmup_slots=0),
        traffic=traffic, access=access, phy_backend=backend,
        harq=HarqController(
            max_attempts=int(cfg["phy"]["maximum_attempts"]),
            feedback_delay_slots=int(recovery_cfg["feedback_delay_slots"]),
            pusch_spacing_slots=int(recovery_cfg.get(
                "harq_pusch_spacing_slots",
                recovery_cfg["feedback_delay_slots"]))),
        recovery=recovery,
        max_rlc_retransmissions=int(recovery_cfg["maximum_rlc_retransmissions"]),
        scheduled_rescue=rescue, scheduled_rlc_recovery=is_scheduled,
        extended_outputs=True, event_addressed_scheduler_ties=True,
        uniform_available_cb_selection=True,
        scheduled_rlc_uses_request_grant=is_scheduled and not direct_timing,
        direct_scheduled_k2_slots=(
            int(recovery_cfg["direct_scheduled_k2_slots"])
            if direct_timing and is_scheduled else None),
        scheduled_resource_policy=resource_policy,
        retain_per_re_trace=False, enhanced_attempt_trace=True).run()
    wall = time.perf_counter() - started
    attempts = int(result["recovery"]["total_phy_transmissions"])
    timing = {"wall_seconds": wall, "phy_attempts": attempts,
              "phy_attempts_per_second": attempts / wall if wall else None,
              "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
              "channel_lmmse_eesm_seconds": receiver.timing_seconds,
              "direct_sionna_lls_seconds": backend.timing_seconds,
              "worker_count": 1}
    return result, timing, traffic


def validate_p2_contract(cfg: dict, scenario_id: str, seed: int) -> None:
    expected = {
        ("phy", "information_bits"): 160,
        ("phy", "tb_crc_bits"): 16,
        ("phy", "mcs_index"): 10,
        ("phy", "modulation"): "16-QAM",
        ("phy", "coded_bits"): 504,
        ("phy", "data_re"): 126,
        ("phy", "maximum_attempts"): 4,
        ("channel", "carrier_frequency_hz"): 700000000.0,
        ("channel", "delay_spread_s"): 3.0e-7,
        ("channel", "num_bs_receive_antennas"): 2,
        ("channel", "num_ue_transmit_antennas"): 1,
        ("channel", "model"): "TDL-C",
        ("channel", "receiver"): "joint_LMMSE",
        ("channel", "csi"): "perfect",
        ("channel", "activity"): "known",
        ("channel", "transmit_power_dbm"): -8.0,
        ("resource_pool", "num_prbs"): 6,
        ("resource_pool", "subcarrier_spacing_hz"): 15000.0,
        ("traffic", "num_ues"): 3000,
        ("traffic", "model"): "independent_per_ue_poisson",
        ("stopping", "warmup_new_packets"): 500,
        ("stopping", "measured_new_packets"): 2500,
        ("recovery", "fast_arq_delay_slots"): 1,
    }
    mismatches = {".".join(path): (cfg[path[0]][path[1]], value)
                  for path, value in expected.items()
                  if cfg[path[0]][path[1]] != value}
    if list(cfg["phy"]["rv_sequence"]) != [0, 2, 3, 1]:
        mismatches["phy.rv_sequence"] = (cfg["phy"]["rv_sequence"], [0, 2, 3, 1])
    if scenario_id not in cfg["scenarios"]:
        mismatches["scenario_id"] = (scenario_id, "one frozen P2 scenario")
    if int(seed) not in tuple(int(value) for value in cfg["main_seeds"]):
        mismatches["seed"] = (seed, cfg["main_seeds"])
    if cfg.get("milestone") in {
            "V0.5-RB", "V0.6-P3", "V0.6-K3", "V0.6-F3",
            "V0.7-RHO070"}:
        v05_expected = {
            "feedback_delay_slots": 1,
            "harq_pusch_spacing_slots": 2,
            "maximum_rlc_retransmissions": 6,
            "direct_scheduled_k2_slots": 1,
            "legacy_sr_period_slots": 10,
            "legacy_sr_phase_domain": "v0.5-sr-phase",
            "sr_control_processing_slots": 1,
            "scheduled_pusch_k2_slots": 1,
        }
        for key, value in v05_expected.items():
            if cfg["recovery"].get(key) != value:
                mismatches[f"recovery.{key}"] = (
                    cfg["recovery"].get(key), value)
        scenario = cfg["scenarios"].get(scenario_id, {})
        if cfg.get("milestone") == "V0.5-RB":
            allowed = {
                ("Legacy-RLC/SB", 20), ("Legacy-RLC/SB", 15),
                ("Fast-CB", None), ("B/Fast-SB", None)}
            pair = (scenario.get("scheme"),
                    scenario.get("legacy_poll_retransmit_slots"))
            if float(scenario.get("rho", -1)) != 0.90 or pair not in allowed:
                mismatches["scenario"] = (scenario, "one frozen V0.5 cell")
        elif cfg.get("milestone") == "V0.6-P3":
            allowed = {
                ("H", "harq_preserving", 2, 4),
                ("F", "fresh_tb", 2, 6),
            }
            cell = (scenario.get("scheme"), scenario.get("rescue_mode"),
                    scenario.get("trigger_after_attempts"),
                    scenario.get("maximum_physical_attempts_per_recovery_episode"))
            if float(scenario.get("rho", -1)) != 0.90 or cell not in allowed:
                mismatches["scenario"] = (
                    scenario, "one frozen V0.6 P3 H/F cell")
        elif cfg.get("milestone") == "V0.6-K3":
            cell = (scenario.get("scheme"), scenario.get("rescue_mode"),
                    scenario.get("trigger_after_attempts"),
                    scenario.get("maximum_physical_attempts_per_recovery_episode"))
            if float(scenario.get("rho", -1)) != 0.90 or cell != (
                    "H(K=3)", "harq_preserving", 3, 4):
                mismatches["scenario"] = (
                    scenario, "the frozen V0.6 K3 cell")
        elif cfg.get("milestone") == "V0.6-F3":
            cell = (scenario.get("scheme"), scenario.get("rescue_mode"),
                    scenario.get("trigger_after_attempts"),
                    scenario.get("maximum_physical_attempts_per_recovery_episode"))
            if float(scenario.get("rho", -1)) != 0.90 or cell != (
                    "F(K=3)", "fresh_tb", 3, 7):
                mismatches["scenario"] = (
                    scenario, "the frozen V0.6 F3 cell")
        else:
            allowed = {
                ("Legacy-RLC/SB", None, None, None, 20),
                ("Legacy-RLC/SB", None, None, None, 15),
                ("Fast-CB", None, None, None, None),
                ("B/Fast-SB", None, None, None, None),
                ("H", "harq_preserving", 2, 4, None),
                ("F", "fresh_tb", 2, 6, None),
                ("H(K=3)", "harq_preserving", 3, 4, None),
                ("F(K=3)", "fresh_tb", 3, 7, None),
            }
            cell = (scenario.get("scheme"), scenario.get("rescue_mode"),
                    scenario.get("trigger_after_attempts"),
                    scenario.get("maximum_physical_attempts_per_recovery_episode"),
                    scenario.get("legacy_poll_retransmit_slots"))
            traffic = cfg["traffic"]
            if float(scenario.get("rho", -1)) != 0.70 or cell not in allowed:
                mismatches["scenario"] = (
                    scenario, "one frozen V0.7 rho=0.70 cell")
            if (traffic.get("rho_values") != [0.70] or
                    traffic.get("aggregate_arrival_rate_per_slot", {}).get(
                        "0.70") != 4.2 or
                    traffic.get("per_ue_arrival_rate_per_slot", {}).get(
                        "0.70") != 0.0014 or
                    traffic.get("per_ue_arrival_rate_per_second", {}).get(
                        "0.70") != 1.4):
                mismatches["traffic.rho070"] = (
                    traffic, "rho 0.70, aggregate 4.2/slot, 0.0014/UE/slot")
        if scenario.get("resource_policy") != "shared_cap2":
            mismatches["scenario.resource_policy"] = (
                scenario.get("resource_policy"), "shared_cap2")
        policy = cfg.get("resource_policies", {}).get("shared_cap2", {})
        if (policy.get("scheduled_capacity_prbs") != 2 or
                policy.get("minimum_cb_prbs") != 4):
            mismatches["resource_policies.shared_cap2"] = (
                policy, "scheduled capacity 2, minimum CB 4")
    else:
        legacy_expected = {
            "maximum_rlc_retransmissions": 2,
            "scheduled_request_delay_slots": 1,
            "scheduled_grant_delay_slots": 2,
        }
        for key, value in legacy_expected.items():
            if cfg["recovery"].get(key) != value:
                mismatches[f"recovery.{key}"] = (
                    cfg["recovery"].get(key), value)
    if cfg.get("milestone") == "V0.4g-P2.5":
        scenario = cfg["scenarios"].get(scenario_id, {})
        if float(scenario.get("rho", -1)) != 0.90:
            mismatches["scenario.rho"] = (scenario.get("rho"), 0.90)
        policy_name = scenario.get("resource_policy")
        policy = cfg.get("resource_policies", {}).get(policy_name)
        if policy_name not in {"shared_cap2", "dedicated_sb2"} or policy is None:
            mismatches["scenario.resource_policy"] = (
                policy_name, "shared_cap2 or dedicated_sb2")
    if cfg.get("milestone") == "V0.4g-LEGACY-TIMING-SENSITIVITY":
        scenario = cfg["scenarios"].get(scenario_id, {})
        if scenario.get("scheme") != "Legacy-CB" or float(
                scenario.get("rho", -1)) != 0.90:
            mismatches["scenario"] = (scenario, "Legacy-CB at rho 0.90")
        if scenario.get("legacy_poll_retransmit_slots") not in {5, 10, 15}:
            mismatches["scenario.legacy_poll_retransmit_slots"] = (
                scenario.get("legacy_poll_retransmit_slots"), "5, 10, or 15")
    if mismatches:
        raise RuntimeError(f"P2 frozen-contract mismatch: {mismatches}")


def p2_output_rows(result: dict, cfg: dict, scenario_id: str, seed: int,
                   config_hash: str, commit: str) -> tuple[
                       list[dict], list[dict], list[dict], list[dict]]:
    scenario = cfg["scenarios"][scenario_id]
    common = {"scenario_id": scenario_id, "config_hash": config_hash,
              "code_commit": commit, "seed": seed,
              "scheme": scenario["scheme"], "rho_or_event_id": scenario["rho"],
              "resource_policy": scenario.get(
                  "resource_policy", "shared_uncapped")}
    packets = []
    for row in sorted(result["packet_rows"], key=lambda value: value["payload_id"]):
        packets.append({**common, "ue_id": row["ue_id"],
            "global_ue_id": row["ue_id"],
            "payload_id": row["payload_id"], "arrival_slot": row["generation_slot"],
            "first_tx_slot": row["first_tx_slot"],
            "harq_termination_slot": row["harq_termination_slot"],
            "completion_slot": row["completion_slot"],
            "final_observation_slot": row["final_observation_slot"],
            "delivered": row["delivered"],
            "final_status": "delivered" if row["delivered"] else "dropped",
            "drop_reason": row["drop_reason"],
            "completion_latency_slots": row["completion_latency_slots"],
            "completion_latency_ms": row["completion_latency_slots"],
            "num_harq_episodes": row["number_of_harq_episodes"],
            "total_phy_attempts": row["number_of_phy_attempts"],
            "rlc_retx_count": row["number_of_rlc_retransmissions"],
            "terminal_reason": row.get("terminal_reason"),
            "t_poll_slots": row.get("t_poll_slots"),
            "sr_period_slots": row.get("sr_period_slots"),
            "sr_phase": row.get("sr_phase"),
            "fast_arq_triggered": row["fast_arq_triggered"],
            "fast_rescue_triggered": row["fast_rescue_triggered"],
            "scheduled_attempt_count": row["scheduled_transmission_count"],
            "receiver_accepted": row["receiver_accepted"],
            "payload_correct": row["payload_correct"],
            "undetected_error": row["undetected_error"],
            "delivered_correctly": row["delivered_correctly"],
            "oracle_terminal_status": row["terminal_status"]})
        packets[-1].update({
            "correct_delivery": row["delivered_correctly"],
            "terminal_failure": row["terminal_status"] == "protocol_drop",
            "harq_episode_count": row["number_of_harq_episodes"],
            "early_rescue_triggered": row.get("early_rescue_triggered", False),
            "early_rescue_trigger_slot": row.get("early_rescue_trigger_slot"),
            "rv2_tx_slot": row.get("rv2_tx_slot"),
            "rv2_nack_slot": row.get("rv2_nack_slot"),
            "rv3_tx_slot": row.get("rv3_tx_slot"),
            "rv3_nack_slot": row.get("rv3_nack_slot"),
            "rescue_trigger_rv": row.get("rescue_trigger_rv"),
            "rescue_trigger_to_tx_delay": row.get("rescue_trigger_to_tx_delay"),
        })
        packets[-1].update({key: row.get(key) for key in (
            "initial_harq_final_failure_slot", "fast_arq_trigger_slot",
            "scheduled_request_slot", "grant_ready_slot",
            "scheduled_queue_entry_slot", "scheduled_first_tx_slot",
            "scheduled_completion_slot", "scheduled_queue_wait_slots",
            "scheduled_harq_attempts", "recovery_phase_latency_slots",
            "correct_recovery_phase_latency_slots",
            "scheduled_grant_dci_slot",
            "legacy_timer_expiry_slot", "legacy_recovery_eligible_slot",
            "next_cb_recovery_tx_slot",
            "effective_final_harq_to_recovery_gap",
            "legacy_timer_wait_slots", "cb_resource_wait_slots",
            "first_recovery_harq_duration_slots")})
    measured_ids = {row["payload_id"] for row in packets}
    ue_by_payload = {row["payload_id"]: row["ue_id"] for row in packets}
    attempts = [{**common, "slot": event["slot"],
        "ue_id": ue_by_payload[event["payload_id"]],
        "payload_id": event["payload_id"],
        "mac_tb_id": event["tb_id"],
        "harq_episode_id": event["harq_episode_id"],
        "rlc_retx_index": event.get("rlc_retx_index", 0),
        "attempt_index": event["harq_attempt"],
        "recovery_episode_physical_attempt": event["recovery_episode_physical_attempt"],
        "rv": event["redundancy_version"], "prb": event["resource_id"],
        "same_prb_occupancy": event["same_prb_occupancy"],
        "post_lmmse_sinr_min_db": event["post_equalization_sinr_min_db"],
        "post_lmmse_sinr_mean_db": event["post_equalization_sinr_mean_db"],
        "post_lmmse_sinr_max_db": event["post_equalization_sinr_max_db"],
        "eesm_sinr_db": event["effective_sinr_db"],
        "ack": event["phy_feedback"] == 1,
        "crc_pass": event["crc_pass"],
        "payload_correct": event["payload_correct"],
        "undetected_error": event["undetected_error"],
        "payload_hamming_distance": event["payload_hamming_distance"],
        "resource_domain": event.get("resource_domain", "base"),
        "phy_resource_id": event.get("phy_resource_id", event["resource_id"]),
        "access": event["transmission_access"],
        "actual_tx_slot": event.get("actual_tx_slot", event["slot"]),
        "feedback_available_slot": event.get("feedback_available_slot"),
        "scheduled_queue_wait": event.get("scheduled_queue_wait"),
        "scheduled_grant_dci_slot": event.get("scheduled_grant_dci_slot"),
        "llr_observation_count": (
            event.get("harq_information_state") or {}).get("llr_vector_count"),
        "rv_history": json.dumps(event.get("rv_history")),
        "effective_sinr_history_db": json.dumps(
            event.get("effective_sinr_history_db"))}
        for event in result["event_trace"]
        if event["event"] == "phy_transmission" and
        event["payload_id"] in measured_ids]
    slots = [{**common, **row} for row in result["slot_resource_rows"]]
    grouped: dict[str, list[dict]] = {}
    for row in attempts:
        grouped.setdefault(row["harq_episode_id"], []).append(row)
    episodes = []
    for episode_id, group in sorted(grouped.items()):
        ordered = sorted(group, key=lambda row: int(row["actual_tx_slot"]))
        accesses = {row["access"] for row in ordered}
        episodes.append({**common,
            "payload_id": ordered[0]["payload_id"],
            "rlc_retx_index": ordered[0]["rlc_retx_index"],
            "mac_tb_id": ordered[0]["mac_tb_id"],
            "harq_episode_id": episode_id,
            "episode_start_slot": ordered[0]["actual_tx_slot"],
            "episode_end_slot": ordered[-1]["feedback_available_slot"],
            "preserved_from_previous_cb_episode": (
                scenario["scheme"] in {"H", "H(K=3)"} and
                accesses == {"grant_free", "scheduled_rescue"}),
            "attempt_count": len(ordered),
            "rv_history": json.dumps([int(row["rv"]) for row in ordered]),
            "access_history": json.dumps([row["access"] for row in ordered])})
    return packets, attempts, slots, episodes


def p2_recovery_timing_rows(result: dict, cfg: dict, scenario_id: str,
                            seed: int, config_hash: str,
                            commit: str) -> list[dict]:
    scenario = cfg["scenarios"][scenario_id]
    common = {"scenario_id": scenario_id, "config_hash": config_hash,
              "code_commit": commit, "seed": seed,
              "scheme": scenario["scheme"],
              "rho_or_event_id": scenario["rho"],
              "resource_policy": scenario.get(
                  "resource_policy", "shared_uncapped")}
    measured_ids = {row["payload_id"] for row in result["packet_rows"]}
    return [{**common, **row} for row in result.get(
        "legacy_timing_rows", []) if row["payload_id"] in measured_ids]


def p2_summary(packets: list[dict], attempts: list[dict], slots: list[dict],
               result: dict, timing: dict, traffic: object, cfg: dict) -> dict:
    accepted = [row for row in packets if row["receiver_accepted"]]
    correctly_delivered = [row for row in packets if row["delivered_correctly"]]
    dropped = [row for row in packets if not row["receiver_accepted"]]
    undetected = [row for row in packets if row["undetected_error"]]
    latencies = [int(row["completion_latency_slots"])
                 for row in correctly_delivered]
    conditional_ecdf = [{"latency_slots": value, "probability": (index + 1) / len(latencies)}
                        for index, value in enumerate(sorted(latencies))]
    completion_curve = [{"latency_slots": value,
                         "probability_of_measured_arrival": (index + 1) / len(packets)}
                        for index, value in enumerate(sorted(latencies))]
    by_rv: dict[str, dict[str, int | float | None]] = {}
    for rv in (0, 2, 3, 1):
        rows = [row for row in attempts if row["rv"] == rv]
        successes = sum(row["ack"] for row in rows)
        by_rv[str(rv)] = {"attempts": len(rows), "acks": successes,
                          "nacks": len(rows) - successes,
                          "conditional_success": successes / len(rows) if rows else None}
    cb_attempts = [row for row in attempts if row["access"] == "grant_free"]
    cb_prb_slots = len({(row["slot"], row["prb"]) for row in cb_attempts})
    scheduled_prb_slots = sum(
        row["access"] == "scheduled_rescue" and
        row["resource_domain"] == "base" for row in attempts)
    dedicated_sb_resources = sum(
        row["access"] == "scheduled_rescue" and
        row["resource_domain"] == "dedicated_sb" for row in attempts)
    final_failure_payloads = {event["payload_id"] for event in result["event_trace"]
                              if event["event"] == "final_harq_failure" and
                              event["payload_id"] in {row["payload_id"] for row in packets}}
    final_failure_events = [
        event for event in result["event_trace"]
        if event["event"] == "final_harq_failure" and
        event["payload_id"] in {row["payload_id"] for row in packets}]
    max_rlc = int(cfg["recovery"]["maximum_rlc_retransmissions"])
    rlc_histogram = {
        str(index): sum(int(row["rlc_retx_count"]) == index
                        for row in packets)
        for index in range(max_rlc + 1)}
    measured_payload_ids = {row["payload_id"] for row in packets}
    legacy_rows = [row for row in result.get("legacy_timing_rows", [])
                   if row["payload_id"] in measured_payload_ids]
    def timing_distribution(key: str) -> dict:
        values = [int(row[key]) for row in legacy_rows
                  if row.get(key) is not None]
        return {"samples": len(values),
                "mean": statistics.fmean(values) if values else None,
                "median": statistics.median(values) if values else None,
                "p95": percentile(values, 0.95),
                "minimum": min(values, default=None),
                "maximum": max(values, default=None)}
    undetected_events = [{key: event[key] for key in (
        "slot", "ue_id", "payload_id", "harq_episode_id", "harq_attempt",
        "redundancy_version", "resource_id", "same_prb_occupancy",
        "post_equalization_sinr_min_db", "post_equalization_sinr_mean_db",
        "post_equalization_sinr_max_db", "effective_sinr_db",
        "payload_hamming_distance")}
        for event in result["event_trace"]
        if event["event"] == "phy_transmission" and event["undetected_error"]]
    return {
        "packets": {"generated_total": (int(cfg["stopping"]["warmup_new_packets"]) +
                                         int(cfg["stopping"]["measured_new_packets"])),
                    "warmup": int(cfg["stopping"]["warmup_new_packets"]),
                    "measured": len(packets), "delivered": len(accepted),
                    "dropped": len(dropped),
                    "delivery_probability": len(accepted) / len(packets),
                    "drop_probability": len(dropped) / len(packets),
                    "unfinished_after_drain": result["packets"]["unfinished_after_drain"]},
        "oracle_reliability": {
                    "receiver_acceptances": len(accepted),
                    "correct_deliveries": len(correctly_delivered),
                    "undetected_errors": len(undetected),
                    "protocol_drops": len(dropped),
                    "receiver_acceptance_probability": len(accepted) / len(packets),
                    "correct_delivery_probability": len(correctly_delivered) / len(packets),
                    "undetected_error_probability": len(undetected) / len(packets),
                    "protocol_drop_probability": len(dropped) / len(packets),
                    "receiver_acceptance_plus_drop":
                        (len(accepted) + len(dropped)) / len(packets),
                    "correct_plus_undetected_plus_drop":
                        (len(correctly_delivered) + len(undetected) + len(dropped)) /
                        len(packets)},
        "latency_slots": {"mean": sum(latencies) / len(latencies) if latencies else None,
                          "median": statistics.median(latencies) if latencies else None,
                          "p95": percentile(latencies, 0.95),
                          "p99": percentile(latencies, 0.99),
                          "conditional_delivered_ecdf": conditional_ecdf,
                          "delivery_aware_completion_curve": completion_curve},
        "phy": {"attempts": len(attempts),
                "harq_episodes": sum(int(row["num_harq_episodes"])
                                      for row in packets),
                "final_harq_failure_events": len(final_failure_events),
                "attempts_per_delivered_packet": len(attempts) / len(accepted) if accepted else None,
                "ack_nack_by_rv": by_rv,
                "fraction_cb_attempts_occupancy_ge_2":
                    sum(row["same_prb_occupancy"] >= 2 for row in cb_attempts) /
                    len(cb_attempts) if cb_attempts else None,
                "final_harq_failure_payload_probability":
                    len(final_failure_payloads) / len(packets),
                "undetected_error_events": undetected_events},
        "resources": {"cb_prb_slots": cb_prb_slots,
                      "scheduled_prb_slots": scheduled_prb_slots,
                      "dedicated_sb_resource_use": dedicated_sb_resources,
                      "base_six_prb_use": cb_prb_slots + scheduled_prb_slots,
                      "combined_physical_resource_use": (
                          cb_prb_slots + scheduled_prb_slots +
                          dedicated_sb_resources),
                      "total_prb_slots": (cb_prb_slots + scheduled_prb_slots +
                                          dedicated_sb_resources),
                      "cb_prb_slots_per_delivered": cb_prb_slots / len(accepted) if accepted else None,
                      "scheduled_prb_slots_per_delivered": scheduled_prb_slots / len(accepted) if accepted else None,
                      "total_prb_slots_per_delivered": (
                          cb_prb_slots + scheduled_prb_slots +
                          dedicated_sb_resources) / len(accepted) if accepted else None,
                      "scheduled_fraction_of_six_prb_budget": scheduled_prb_slots /
                          (6 * len(slots)) if slots else None,
                      "fraction_slots_zero_cb_prbs": sum(row["zero_cb_prbs"] for row in slots) /
                          len(slots) if slots else None,
                      "pool_invariant_all_slots": all(row["pool_invariant"] for row in slots)},
        "legacy_timing": legacy_timing_summary(packets),
        "v0_5_legacy_timing": {
            "population": "RLC retransmission episodes",
            "episodes": len(legacy_rows),
            "sr_wait_slots": timing_distribution("sr_wait_slots"),
            "sr_to_scheduled_tx_slots": timing_distribution(
                "sr_to_scheduled_tx_slots"),
            "scheduled_queue_wait_slots": timing_distribution(
                "scheduled_queue_wait_slots")},
        "rlc": {
            "maximum_retransmission_episodes": max_rlc,
            "retransmission_count_histogram": rlc_histogram,
            "fraction_reaching_retransmission": {
                str(index): sum(int(row["rlc_retx_count"]) >= index
                                for row in packets) / len(packets)
                for index in range(1, max_rlc + 1)},
            "terminal_failures_after_threshold": len(dropped)},
        "scheduled_queue": scheduled_queue_summary(slots, packets),
        "recovery": result["rescue"], "runtime": timing,
        "traffic": {"measurement_start_time_slots": traffic.measurement_start_time_slots,
                    "arrival_stop_time_slots": traffic.arrival_stop_time_slots,
                    "arrival_horizon_slots": traffic.duration_slots}}


def p2_write_run(cfg: dict, config_path: Path, scenario_id: str, seed: int,
                 root: Path, *, allow_dirty: bool = False) -> dict:
    if not allow_dirty:
        validate_p2_contract(cfg, scenario_id, seed)
    dirty = git_dirty()
    if dirty and not allow_dirty:
        raise RuntimeError("production P2 run requires a clean working tree")
    commit = git_sha()
    scenario = cfg["scenarios"][scenario_id]
    resolved = {"scenario_id": scenario_id, "seed": seed,
                "common_contract": cfg, "scenario": scenario}
    config_hash = hashlib.sha256(canonical(resolved)).hexdigest()
    directory = root / scenario_id / f"seed_{seed}"
    if (cfg.get("milestone") in {
            "V0.4g-P2.5", "V0.4g-LEGACY-TIMING-SENSITIVITY", "V0.5-RB",
            "V0.6-P3", "V0.6-K3", "V0.6-F3", "V0.7-RHO070"} and
            directory.exists() and
            any(directory.iterdir())):
        raise FileExistsError(f"refusing to overwrite artifact: {directory}")
    directory.mkdir(parents=True, exist_ok=True)
    started_utc = datetime.now(timezone.utc).isoformat()
    result, timing, traffic = p2_build_run(cfg, scenario_id, seed)
    packets, attempts, slots, episodes = p2_output_rows(
        result, cfg, scenario_id, seed, config_hash, commit)
    recovery_timing = p2_recovery_timing_rows(
        result, cfg, scenario_id, seed, config_hash, commit)
    if len(packets) != int(cfg["stopping"]["measured_new_packets"]):
        raise RuntimeError(f"measured packet count mismatch: {len(packets)}")
    if result["packets"]["unfinished_after_drain"] or not all(
            row["pool_invariant"] for row in slots):
        raise RuntimeError("terminal drain or six-PRB invariant failed")
    summary = p2_summary(packets, attempts, slots, result, timing, traffic, cfg)
    output_paths = {
        "packets.csv.gz": p2_csv_bytes(packets),
        "attempts.csv.gz": p2_csv_bytes(attempts),
        "slot_prb.csv.gz": p2_csv_bytes(slots)}
    if cfg.get("milestone") in {
            "V0.6-P3", "V0.6-K3", "V0.6-F3", "V0.7-RHO070"}:
        output_paths["harq_episodes.csv.gz"] = p2_csv_bytes(episodes)
    if cfg.get("milestone") in {"V0.5-RB", "V0.7-RHO070"}:
        output_paths["recovery_timing.csv.gz"] = p2_csv_bytes(
            recovery_timing)
    for filename, payload in output_paths.items():
        deterministic_gzip(directory / filename, payload)
    summary_path = directory / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    output_hashes = {name: sha256(directory / name) for name in output_paths}
    output_hashes[summary_path.name] = sha256(summary_path)
    manifest = {"schema_version": (4 if cfg.get("milestone") in {
        "V0.6-P3", "V0.6-K3", "V0.6-F3", "V0.7-RHO070"} else 3),
        "milestone": cfg["milestone"],
        "scenario_id": scenario_id, "seed": seed,
        "started_utc": started_utc,
        "ended_utc": datetime.now(timezone.utc).isoformat(),
        "git_sha": commit, "dirty_worktree": dirty,
        "development_override": bool(allow_dirty),
        "config_path": str(config_path), "config_file_sha256": sha256(config_path),
        "configuration_hash": config_hash, "resolved_configuration": resolved,
        "rng_contract": RNG_CONTRACT,
        "environment": {"python_executable": sys.executable,
            "python": platform.python_version(), "sionna": version("sionna"),
            "torch": torch.__version__, "device": "cpu",
            "deterministic_algorithms": True, "torch_num_threads": 1,
            "worker_count": 1},
        "output_sha256": output_hashes, "runtime": timing}
    (directory / "run_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def p2_replay_check(cfg: dict, config_path: Path, scenario_id: str, seed: int,
                    primary_root: Path) -> dict:
    filenames = ("packets.csv.gz", "attempts.csv.gz", "slot_prb.csv.gz")
    if cfg.get("milestone") in {
            "V0.6-P3", "V0.6-K3", "V0.6-F3", "V0.7-RHO070"}:
        filenames += ("harq_episodes.csv.gz",)
    if cfg.get("milestone") in {"V0.5-RB", "V0.7-RHO070"}:
        filenames += ("recovery_timing.csv.gz",)
    with tempfile.TemporaryDirectory(prefix="v04g-p2-replay-", dir="/tmp") as raw:
        replay_root = Path(raw)
        p2_write_run(cfg, config_path, scenario_id, seed, replay_root,
                     allow_dirty=git_dirty())
        primary = primary_root / scenario_id / f"seed_{seed}"
        replay = replay_root / scenario_id / f"seed_{seed}"
        checks = []
        for filename in filenames:
            if (primary / filename).read_bytes() != (replay / filename).read_bytes():
                raise RuntimeError(f"P2 deterministic replay mismatch: {filename}")
            checks.append({"file": filename, "sha256": sha256(primary / filename)})
    status = {"status": "BYTE-IDENTICAL", "files": checks}
    manifest_path = primary / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["deterministic_replay"] = status
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return status


def main() -> None:
    args = parse_args()
    cfg = load_yaml(args.config)
    if args.output_root is None:
        args.output_root = (
            Path("results/raw") if cfg.get("milestone") == "V0.4g-P2" else
            Path("results/raw_p2_5") if cfg.get("milestone") == "V0.4g-P2.5" else
            Path("results/raw_legacy_timing")
            if cfg.get("milestone") == "V0.4g-LEGACY-TIMING-SENSITIVITY" else
            Path("results/raw_v0_5")
            if cfg.get("milestone") == "V0.5-RB" else
            Path("results/raw_v0_6_p3")
            if cfg.get("milestone") == "V0.6-P3" else
            Path("results/raw_v0_6_k3")
            if cfg.get("milestone") == "V0.6-K3" else
            Path("results/raw_v0_6_f3")
            if cfg.get("milestone") == "V0.6-F3" else
            Path("results/raw_v0_7_rho070")
            if cfg.get("milestone") == "V0.7-RHO070" else
            Path("results/v0_4g_p1_runs"))
    scenarios = tuple(cfg["scenarios"])
    if args.list_scenarios:
        suffix = (("P2",) if cfg.get("milestone") == "V0.4g-P2" else
                  ("P2.5",) if cfg.get("milestone") == "V0.4g-P2.5" else
                  ("V0.6-P3",) if cfg.get("milestone") == "V0.6-P3" else
                  ("V0.6-K3",) if cfg.get("milestone") == "V0.6-K3" else
                  ("V0.6-F3",) if cfg.get("milestone") == "V0.6-F3" else
                  ("V0.7-RHO070",)
                  if cfg.get("milestone") == "V0.7-RHO070" else
                  ("p1-all",))
        print("\n".join((*scenarios, *suffix)))
        return
    if cfg.get("milestone") in {
            "V0.4g-P2", "V0.4g-P2.5",
            "V0.4g-LEGACY-TIMING-SENSITIVITY", "V0.5-RB", "V0.6-P3",
            "V0.6-K3", "V0.6-F3", "V0.7-RHO070"}:
        if args.group is not None:
            if not args.all_main_seeds:
                raise SystemExit("--group requires --all-main-seeds")
            chosen = tuple((scenario, int(seed)) for scenario in scenarios
                           for seed in cfg["main_seeds"])
        else:
            if args.scenario_id is None:
                raise SystemExit("--scenario-id is required")
            if args.scenario_id not in scenarios:
                raise SystemExit(f"unknown scenario: {args.scenario_id}")
            if args.all_main_seeds:
                raise SystemExit("--all-main-seeds is valid only with --group P2")
            chosen = ((args.scenario_id, args.seed),)
        if args.resolved_config:
            print(json.dumps({"runs": [{"scenario_id": scenario, "seed": seed}
                  for scenario, seed in chosen], "config": cfg},
                  indent=2, sort_keys=True))
            return
        torch.set_num_threads(1)
        torch.use_deterministic_algorithms(True)
        manifests = [p2_write_run(
            cfg, args.config, scenario, seed, args.output_root,
            allow_dirty=args.allow_dirty_development)
            for scenario, seed in chosen]
        if args.verify_replay:
            for manifest in manifests:
                manifest["deterministic_replay"] = p2_replay_check(
                    cfg, args.config, manifest["scenario_id"],
                    manifest["seed"], args.output_root)
        print(json.dumps(manifests, indent=2, sort_keys=True))
        return
    if args.scenario_id is None:
        raise SystemExit("--scenario-id is required unless --list-scenarios is used")
    if args.resolved_config:
        print(json.dumps({"scenario_id": args.scenario_id, "seed": args.seed,
                          "config": cfg}, indent=2, sort_keys=True))
        return
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)
    chosen = scenarios if args.scenario_id == "p1-all" else (args.scenario_id,)
    if any(value not in scenarios for value in chosen):
        raise SystemExit(f"unknown scenario: {args.scenario_id}")
    manifests = [write_run(cfg, scenario, args.seed, args.output_root)
                 for scenario in chosen]
    if args.scenario_id == "p1-all":
        runs = []
        for manifest in manifests:
            result_path = (args.output_root / manifest["scenario_id"] /
                           str(args.seed) / "result.json")
            result = json.loads(result_path.read_text())
            runs.append({"scenario_id": manifest["scenario_id"],
                "packet_count": manifest["packet_count"],
                "phy_attempt_count": manifest["phy_attempt_count"],
                "runtime": manifest["runtime"],
                "resource_accounting": {key: result["resources"][key] for key in
                    ("cb_prb_use", "scheduled_prb_use",
                     "prbs_unavailable_to_cb_due_to_scheduled_use")}})
        artifact = {"schema_version": 1, "milestone": "V0.4g-P0/P1",
            "classification": "P0/P1-READY-WITH-LIMITATIONS",
            "geometry_audit": audit_controlled_pusch_geometry(),
            "seed": args.seed, "configuration_hashes":
                [value["configuration_hash"] for value in manifests],
            "runs": runs,
            "scope_guard": "No Q1/Q2/Q5/Q6 campaign or final B/H/F comparison was run."}
        artifact["trace_audits"] = trace_audits(args.output_root, args.seed)
        artifact["deterministic_replay"] = (replay_check(
            cfg, chosen, args.seed, args.output_root)
            if args.verify_replay else {"status": "NOT_REQUESTED", "files": []})
        output = Path("results/direct_lls_sls_validation_v0_4g.json")
        output.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
        Path("results/direct_lls_sls_validation_v0_4g.md").write_text(
            validation_markdown(artifact))
        print(json.dumps({"validation": str(output), "runs": runs}, indent=2))
    else:
        print(json.dumps(manifests[0], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
