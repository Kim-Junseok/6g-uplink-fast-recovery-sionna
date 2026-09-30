"""Regression tests for fixed-cohort event-relative response diagnostics."""

from __future__ import annotations

import importlib.util
import pathlib
import unittest

from ul_access.protocol import EventSkeleton


ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_experiment(name: str):
    path = ROOT / "experiments" / name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


V03D = load_experiment("06_traffic_sensitivity.py")
V03E = load_experiment("07_traffic_revalidation.py")


def diagnostic_run(length: int = 10) -> dict:
    names = ("generated_arrivals", "grant_free_attempts", "overlapping_ues",
             "nacks", "fast_arq_triggers", "rlc_retransmission_entries",
             "successful_completions")
    return {"traffic_diagnostics": {"measurement_slot_origin": 0,
            "series": {name: list(range(length)) for name in names}}}


class EventResponseCohortTest(unittest.TestCase):
    def test_v03d_excludes_events_without_the_complete_tail(self):
        response = V03D.event_response(diagnostic_run(), (1.2, 8.1), tail=2)

        self.assertEqual(response["event_count"], 1)
        self.assertEqual(response["mean_intensity"]["generated_arrivals"],
                         [1.0, 2.0, 3.0])

    def test_v03e_excludes_events_without_the_complete_window_and_tail(self):
        skeleton = EventSkeleton(start_slot=0, duration_slots=10,
                                 event_times=(1.2, 6.1), activations=())
        response = V03E.event_response(diagnostic_run(), skeleton,
                                       window=2, tail=2)

        self.assertEqual(response["unwrapped_event_count"], 1)
        self.assertEqual(response["mean_intensity"]["generated_arrivals"],
                         [1.0, 2.0, 3.0, 4.0, 5.0])


if __name__ == "__main__":
    unittest.main()
