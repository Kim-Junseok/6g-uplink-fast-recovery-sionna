"""Deterministic stage-conditioned HARQ-history support accounting."""

from __future__ import annotations

from enum import Enum
import math
from typing import Iterable, Mapping, Sequence


class HarqSupportClass(str, Enum):
    """Mutually exclusive relation between an SLS history and LLS support."""

    IN_VALIDATED_SUPPORT = "IN_VALIDATED_SUPPORT"
    INTERPOLATION = "INTERPOLATION"
    BOUNDARY_CLIPPED = "BOUNDARY_CLIPPED"
    OUTSIDE_VALIDATED_SUPPORT = "OUTSIDE_VALIDATED_SUPPORT"


def classify_history(
    history_db: Sequence[float], *,
    validated_points_db: Iterable[Sequence[float]],
    bounds_db: Sequence[Sequence[float]],
    global_guard_db: Sequence[float],
    exact_tolerance_db: float = 1e-12,
) -> HarqSupportClass:
    """Classify one history using declared, deterministic V0.4c rules.

    An exact LLS calibration/validation point is ``IN_VALIDATED_SUPPORT``.
    Other finite points in the declared per-coordinate box require
    interpolation. Points outside that box but inside the global numerical
    guard are boundary-clipped. Non-finite values, wrong dimensions, and
    values outside the global guard are outside validated support.
    """
    values = tuple(float(value) for value in history_db)
    bounds = tuple((float(pair[0]), float(pair[1])) for pair in bounds_db)
    guard = (float(global_guard_db[0]), float(global_guard_db[1]))
    if len(values) != len(bounds) or len(guard) != 2 or guard[0] >= guard[1]:
        return HarqSupportClass.OUTSIDE_VALIDATED_SUPPORT
    if any(not math.isfinite(value) or not guard[0] <= value <= guard[1]
           for value in values):
        return HarqSupportClass.OUTSIDE_VALIDATED_SUPPORT
    points = [tuple(float(value) for value in point)
              for point in validated_points_db if len(point) == len(values)]
    if any(all(abs(left - right) <= exact_tolerance_db
               for left, right in zip(values, point)) for point in points):
        return HarqSupportClass.IN_VALIDATED_SUPPORT
    if all(low <= value <= high
           for value, (low, high) in zip(values, bounds)):
        return HarqSupportClass.INTERPOLATION
    return HarqSupportClass.BOUNDARY_CLIPPED


def collect_stage_conditioned_histories(
    event_trace: Iterable[Mapping], *, scheme: str,
    start_slot: int = 0, stop_slot: int | None = None,
) -> list[dict]:
    """Collect complete histories and verify prior-NACK conditioning.

    PHY records are ordered in simulator time. Conditioning is episode-local,
    which correctly treats F's scheduled RV0 as attempt one of a fresh HARQ
    episode while retaining its physical attempt index for interpretation.
    """
    feedback_by_episode: dict[str, list[int]] = {}
    records: list[dict] = []
    for event in event_trace:
        if event.get("event") != "phy_transmission":
            continue
        slot = int(event["slot"])
        episode = event.get("harq_episode_id")
        rv_history = event.get("rv_history")
        sinr_history = event.get("effective_sinr_history_db")
        if episode is None or rv_history is None or sinr_history is None:
            continue
        prior = feedback_by_episode.setdefault(str(episode), [])
        episode_attempt = len(rv_history)
        if episode_attempt != len(sinr_history):
            raise ValueError("RV and SINR history lengths differ")
        if len(prior) != episode_attempt - 1 or any(value != 0 for value in prior):
            raise ValueError("history reached an attempt without all prior NACKs")
        record = {
            "payload_id": event.get("payload_id"),
            "mac_tb_id": event.get("tb_id"),
            "harq_episode_id": episode,
            "scheme": str(scheme),
            "access_mode": ("SB" if event.get("transmission_access") ==
                            "scheduled_rescue" else "CB"),
            "physical_attempt_index": int(
                event["recovery_episode_physical_attempt"]),
            "attempt_index": episode_attempt,
            "rv": int(event["redundancy_version"]),
            "effective_sinr_db": float(event["effective_sinr_db"]),
            "ack": int(event["phy_feedback"]),
            "rv_history": [int(value) for value in rv_history],
            "effective_sinr_history_db": [float(value)
                                           for value in sinr_history],
            "modeled_success_probability": 1.0 - float(event["modeled_tbler"]),
        }
        prior.append(record["ack"])
        if slot >= start_slot and (stop_slot is None or slot < stop_slot):
            records.append(record)
    return records


def support_coverage(records: Iterable[Mapping]) -> dict:
    """Return exclusive category counts/fractions and validated coverage."""
    values = list(records)
    counts = {item.value: 0 for item in HarqSupportClass}
    for record in values:
        category = record["support_class"]
        key = category.value if isinstance(category, HarqSupportClass) else str(category)
        counts[key] += 1
    total = len(values)
    fractions = {key: value / total if total else None
                 for key, value in counts.items()}
    supported = (counts[HarqSupportClass.IN_VALIDATED_SUPPORT.value] +
                 counts[HarqSupportClass.INTERPOLATION.value])
    return {
        "count": total, "counts": counts, "fractions": fractions,
        "validated_support_fraction": supported / total if total else None,
        "clipped_fraction": (
            counts[HarqSupportClass.BOUNDARY_CLIPPED.value] / total
            if total else None),
    }
