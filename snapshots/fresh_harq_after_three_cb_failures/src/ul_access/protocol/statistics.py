"""Run-level aggregation and Student-t confidence intervals."""

from __future__ import annotations

import math
import statistics
from collections import Counter
from collections.abc import Sequence
from typing import Any

from ul_access.protocol.simulator import _percentile


_T_975 = {
    1: 12.706,
    2: 4.303,
    3: 3.182,
    4: 2.776,
    5: 2.571,
    6: 2.447,
    7: 2.365,
    8: 2.306,
    9: 2.262,
    10: 2.228,
    11: 2.201,
    12: 2.179,
    13: 2.160,
    14: 2.145,
    15: 2.131,
    16: 2.120,
    17: 2.110,
    18: 2.101,
    19: 2.093,
    20: 2.086,
    21: 2.080,
    22: 2.074,
    23: 2.069,
    24: 2.064,
    25: 2.060,
    26: 2.056,
    27: 2.052,
    28: 2.048,
    29: 2.045,
    30: 2.042,
}


def student_t_confidence_interval_95(
    samples: Sequence[float],
) -> dict[str, float | int | str | None]:
    """Two-sided 95% CI for a mean, treating each value as one independent run."""
    values = [float(value) for value in samples]
    if not values:
        return {
            "confidence_level": 0.95,
            "method": "two-sided Student-t across independent seed-level values",
            "sample_count": 0,
            "mean": None,
            "sample_standard_deviation": None,
            "lower": None,
            "upper": None,
            "half_width": None,
        }
    mean = statistics.fmean(values)
    if len(values) < 2:
        return {
            "confidence_level": 0.95,
            "method": "two-sided Student-t across independent seed-level values",
            "sample_count": 1,
            "mean": mean,
            "sample_standard_deviation": None,
            "lower": None,
            "upper": None,
            "half_width": None,
        }
    deviation = statistics.stdev(values)
    critical = _T_975.get(len(values) - 1, 1.96)
    half_width = critical * deviation / math.sqrt(len(values))
    return {
        "confidence_level": 0.95,
        "method": "two-sided Student-t across independent seed-level values",
        "sample_count": len(values),
        "mean": mean,
        "sample_standard_deviation": deviation,
        "lower": mean - half_width,
        "upper": mean + half_width,
        "half_width": half_width,
    }


def aggregate_calibration_runs(runs: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate independent seed runs without treating packets as independent."""
    if not runs:
        raise ValueError("at least one run is required")
    statuses = [run["stability_status"] for run in runs]
    ci_eligible = all(status != "overloaded" for status in statuses)
    paths = {
        "mean_packet_latency_slots": (
            "latency_slots_measurement_arrivals",
            "mean",
        ),
        "packet_delivery_ratio_after_drain": (
            "packets",
            "delivery_ratio_after_drain",
        ),
        "resource_efficiency_bits_per_total_available": (
            "resources_measurement",
            "delivered_bits_per_total_available_physical_resource",
        ),
        "transmission_failure_rate": (
            "reliability_measurement",
            "transmission_failure_rate",
        ),
    }
    primary = {}
    for name, path in paths.items():
        values = [run[path[0]][path[1]] for run in runs]
        numeric = [float(value) for value in values if value is not None]
        primary[name] = {
            "per_seed_values": values,
            "aggregate_mean": statistics.fmean(numeric) if numeric else None,
            "aggregate_sample_standard_deviation": (
                statistics.stdev(numeric) if len(numeric) >= 2 else None
            ),
            "confidence_interval_95": (
                student_t_confidence_interval_95(numeric)
                if ci_eligible and len(numeric) == len(runs)
                else None
            ),
        }

    pooled_latency = [
        value
        for run in runs
        for value in run["latency_slots_measurement_arrivals"]["samples"]
    ]
    per_seed_percentiles = {
        key: [
            run["latency_slots_measurement_arrivals"][key] for run in runs
        ]
        for key in ("p50", "p95", "p99")
    }
    return {
        "independent_sampling_unit": "seed-level simulation run",
        "confidence_intervals_reported": ci_eligible,
        "confidence_interval_exclusion_reason": (
            None
            if ci_eligible
            else "at least one seed run was classified overloaded"
        ),
        "stability_status_counts": dict(Counter(statuses)),
        "drain_timeout_runs": sum(
            run["phases"]["drain_timeout_reached"] for run in runs
        ),
        "primary_metrics": primary,
        "tail_latency": {
            "pooled_packet_sample_count": len(pooled_latency),
            "pooled_packet_p50": _percentile(pooled_latency, 50.0),
            "pooled_packet_p95": _percentile(pooled_latency, 95.0),
            "pooled_packet_p99": _percentile(pooled_latency, 99.0),
            "per_seed_percentiles": per_seed_percentiles,
            "mean_of_per_seed_percentiles": {
                key: statistics.fmean(values)
                for key, values in per_seed_percentiles.items()
                if all(value is not None for value in values)
            },
            "pooled_packet_latency_samples": pooled_latency,
        },
    }

