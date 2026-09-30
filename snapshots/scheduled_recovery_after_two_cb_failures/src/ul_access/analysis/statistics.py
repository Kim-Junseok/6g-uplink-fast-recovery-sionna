"""Seed-level statistics for reusable campaign analysis."""

from __future__ import annotations

import itertools
import math
import statistics
from collections.abc import Sequence


_T_975 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
          6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
          11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131,
          16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
          21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060,
          26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042}


def mean_student_t_ci95(values: Sequence[float]) -> dict[str, float | int | None]:
    """Return a two-sided Student-t interval and every intermediate quantity."""
    samples = [float(value) for value in values]
    if not samples:
        raise ValueError("at least one seed-level value is required")
    mean = statistics.fmean(samples)
    n = len(samples)
    if n == 1:
        return {"sample_count": 1, "degrees_of_freedom": 0, "mean": mean,
                "sample_standard_deviation": None, "standard_error": None,
                "t_critical_975": None, "ci95_lower": None,
                "ci95_upper": None, "ci95_half_width": None}
    deviation = statistics.stdev(samples)
    standard_error = deviation / math.sqrt(n)
    critical = _T_975.get(n - 1, 1.96)
    half_width = critical * standard_error
    return {"sample_count": n, "degrees_of_freedom": n - 1, "mean": mean,
            "sample_standard_deviation": deviation,
            "standard_error": standard_error, "t_critical_975": critical,
            "ci95_lower": mean - half_width, "ci95_upper": mean + half_width,
            "ci95_half_width": half_width}


def paired_difference_summary(baseline: Sequence[float],
                              treatment: Sequence[float]) -> dict:
    """Summarize treatment-minus-baseline differences for paired seeds."""
    if len(baseline) != len(treatment) or not baseline:
        raise ValueError("paired samples must be non-empty and have equal length")
    differences = [float(new) - float(old) for old, new in zip(baseline, treatment)]
    return {"direction": "treatment_minus_baseline", "differences": differences,
            **mean_student_t_ci95(differences)}


def leave_one_out_means(differences: Sequence[float]) -> list[float]:
    """Return the paired mean after omitting each seed once."""
    values = [float(value) for value in differences]
    if len(values) < 2:
        raise ValueError("leave-one-out requires at least two values")
    return [statistics.fmean(values[:i] + values[i + 1:])
            for i in range(len(values))]


def exact_sign_flip_pvalue(differences: Sequence[float]) -> float:
    """Return the exact two-sided paired sign-flip randomization p-value."""
    values = [float(value) for value in differences]
    if not values:
        raise ValueError("at least one paired difference is required")
    observed = abs(statistics.fmean(values))
    randomized = [abs(statistics.fmean(sign * value for sign, value in zip(signs, values)))
                  for signs in itertools.product((-1.0, 1.0), repeat=len(values))]
    tolerance = 1e-15
    return sum(value + tolerance >= observed for value in randomized) / len(randomized)
