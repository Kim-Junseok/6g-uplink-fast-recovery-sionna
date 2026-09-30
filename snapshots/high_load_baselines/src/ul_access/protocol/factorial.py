"""Paired 2x2 Access x Recovery experiment construction and statistics."""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from typing import Any

from ul_access.protocol.statistics import student_t_confidence_interval_95

TREATMENTS = {
    "A": ("scheduled", "legacy_arq"),
    "B": ("grant_free", "legacy_arq"),
    "C": ("scheduled", "fast_arq"),
    "D": ("grant_free", "fast_arq"),
}


def scalar_metrics(run: Mapping[str, Any]) -> dict[str, float | None]:
    """Extract scientifically named run-level scalar metrics."""
    latency = run["latency_slots"]
    packets = run["packets"]
    traffic = run["measurement_traffic"]
    resources = run["resources"]
    components = run["latency_components_slots"]
    completed = packets["completed"]
    component_mean = lambda name: (statistics.fmean(components[name]) if components[name] else None)
    per_packet = lambda value: value / packets["arrived"] if packets["arrived"] else None
    metrics = {
        "mean_latency_slots": latency["mean"], "p50_latency_slots": latency["median"],
        "p95_latency_slots": latency["p95"], "p99_latency_slots": latency["p99"],
        "delivery_ratio": packets["delivery_ratio_after_drain"],
        "drop_ratio": packets["drop_ratio_after_drain"],
        "unfinished_after_drain": float(packets["unfinished_after_drain"]),
        "phy_nack_rate": (traffic["nacks"] / (traffic["acks"] + traffic["nacks"]) if traffic["acks"] + traffic["nacks"] else None),
        "harq_retransmissions_per_packet": per_packet(traffic["harq_retx"]),
        "final_harq_failures_per_packet": per_packet(traffic["final_failures"]),
        "rlc_retransmissions_per_packet": per_packet(traffic["rlc_retx_tx"]),
        "rlc_retransmissions_per_slot": traffic["rlc_retransmissions_per_measurement_slot"],
        "total_phy_attempts_per_slot": traffic["total_phy_attempts_per_measurement_slot"],
        "queue_access_wait_slots": component_mean("queue_and_access_wait"),
        "harq_feedback_wait_slots": component_mean("harq_feedback_wait"),
        "harq_retx_access_wait_slots": component_mean("harq_retransmission_wait"),
        "rlc_recovery_wait_slots": component_mean("rlc_recovery_wait"),
        "overlap_rate": resources["overlap_measurement"]["events"] / resources["overlap_measurement"]["occupied"] if resources["overlap_measurement"]["occupied"] else 0.0,
        "overlap_recovery_fraction": resources["overlap_measurement"]["fraction_overlap_events_involving_recovery"],
        "bits_per_total_resource": resources["successful_bits_per_total_available_resource"],
        "bits_per_occupied_resource": resources["successful_bits_per_occupied_resource"],
        "bits_per_ue_attempt": resources["successful_bits_per_ue_attempt"],
        "recovery_peak_per_slot": float(run["recovery_pressure"]["peak_recovery_originated_transmissions_per_slot"]),
        "recovery_p95_per_slot": run["recovery_pressure"]["p95_recovery_originated_transmissions_per_slot"],
        "recovery_slot_fraction": run["recovery_pressure"]["fraction_slots_with_recovery_traffic"],
        "completed_packets": float(completed),
    }
    for series_name, summary in run.get("traffic_diagnostics", {}).get("summaries", {}).items():
        for statistic_name, value in summary.items():
            metrics[f"{series_name}_{statistic_name}_per_slot"] = value
    return metrics


def seed_effects(treatments: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, float | None]]:
    """Calculate signed paired main/access effects and difference-in-differences."""
    metrics = {key: scalar_metrics(value) for key, value in treatments.items()}
    if set(metrics) != set(TREATMENTS):
        raise ValueError("paired record must contain exactly treatments A/B/C/D")
    output: dict[str, dict[str, float | None]] = {}
    for metric in metrics["A"]:
        a, b, c, d = (metrics[key][metric] for key in "ABCD")
        output[metric] = {
            "A": a, "B": b, "C": c, "D": d,
            "fast_effect_scheduled_C_minus_A": None if a is None or c is None else c - a,
            "fast_effect_grant_free_D_minus_B": None if b is None or d is None else d - b,
            "grant_free_effect_legacy_B_minus_A": None if a is None or b is None else b - a,
            "grant_free_effect_fast_D_minus_C": None if c is None or d is None else d - c,
            "interaction_D_minus_B_minus_C_plus_A": None if any(x is None for x in (a,b,c,d)) else d - b - c + a,
        }
    return output


def aggregate_paired_effects(
    records: Sequence[Mapping[str, Any]], *, report_confidence_intervals: bool = True
) -> dict[str, Any]:
    """Aggregate paired runs, suppressing CIs when stability invalidates them."""
    if not records:
        raise ValueError("at least one paired seed record is required")
    names = records[0]["effects"].keys()
    result = {}
    for name in names:
        summaries = {}
        for effect in ("fast_effect_scheduled_C_minus_A", "fast_effect_grant_free_D_minus_B",
                       "grant_free_effect_legacy_B_minus_A", "grant_free_effect_fast_D_minus_C",
                       "interaction_D_minus_B_minus_C_plus_A"):
            values = [record["effects"][name][effect] for record in records]
            numeric = [float(v) for v in values if v is not None]
            summaries[effect] = {"per_seed_values": values,
                "mean": statistics.fmean(numeric) if numeric else None,
                "confidence_interval_95": (
                    student_t_confidence_interval_95(numeric)
                    if report_confidence_intervals and len(numeric) == len(values)
                    else None
                )}
        result[name] = summaries
    return result
