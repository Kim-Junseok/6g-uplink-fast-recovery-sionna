"""Empirical distribution curves with equal seed weighting."""

from __future__ import annotations

import bisect
from collections.abc import Mapping, Sequence

from ul_access.analysis.statistics import mean_student_t_ci95


def evaluate_ecdf(samples: Sequence[float], grid: Sequence[float],
                  denominator: int | None = None) -> list[float]:
    """Evaluate an ECDF; an external denominator yields a completion curve."""
    ordered = sorted(float(value) for value in samples)
    normalizer = len(ordered) if denominator is None else int(denominator)
    if normalizer <= 0 or len(ordered) > normalizer:
        raise ValueError("the curve denominator must cover all samples")
    return [bisect.bisect_right(ordered, float(point)) / normalizer for point in grid]


def seed_averaged_curve(samples_by_seed: Mapping[int, Sequence[float]],
                        grid: Sequence[float], *, denominators: Mapping[int, int] | None = None,
                        include_seed_curves: bool = True) -> list[dict]:
    """Average seed curves equally and add a pointwise Student-t 95% band."""
    if not samples_by_seed:
        raise ValueError("at least one seed is required")
    seeds = sorted(samples_by_seed)
    curves = {seed: evaluate_ecdf(samples_by_seed[seed], grid,
              None if denominators is None else denominators[seed]) for seed in seeds}
    output = []
    for index, point in enumerate(grid):
        values = [curves[seed][index] for seed in seeds]
        interval = mean_student_t_ci95(values)
        row = {"latency_slots": float(point), "seed_mean": interval["mean"],
               "pointwise_ci95_lower": max(0.0, interval["ci95_lower"]),
               "pointwise_ci95_upper": min(1.0, interval["ci95_upper"]),
               "seed_count": len(seeds)}
        if include_seed_curves:
            row.update({f"seed_{seed}": curves[seed][index] for seed in seeds})
        output.append(row)
    return output
