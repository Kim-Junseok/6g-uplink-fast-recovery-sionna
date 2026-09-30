"""V0.4d stage-conditioned HARQ support accounting tests."""

from __future__ import annotations

import math
import hashlib
import json
from pathlib import Path
import unittest

from ul_access.config import load_yaml

from ul_access.analysis import (
    HarqSupportClass,
    classify_history,
    collect_stage_conditioned_histories,
    support_coverage,
)


def tx(episode: str, attempt: int, history: list[float], feedback: int,
       *, slot: int | None = None, rv_history: list[int] | None = None,
       access: str = "grant_free", physical: int | None = None) -> dict:
    rvs = rv_history or [0, 2, 3, 1][:attempt]
    return {
        "event": "phy_transmission", "slot": attempt if slot is None else slot,
        "payload_id": "p", "tb_id": f"tb-{episode}",
        "harq_episode_id": episode,
        "recovery_episode_physical_attempt": physical or attempt,
        "transmission_access": access, "redundancy_version": rvs[-1],
        "rv_history": rvs, "effective_sinr_history_db": history,
        "effective_sinr_db": history[-1], "modeled_tbler": 0.25,
        "phy_feedback": feedback,
    }


class HarqSupportTest(unittest.TestCase):
    def test_four_exclusive_support_classes_and_probability_accounting(self):
        kwargs = dict(validated_points_db=[[-4.0], [0.0]],
                      bounds_db=[[-4.0, 6.0]], global_guard_db=[-30.0, 30.0])
        expected = [
            ([-4.0], HarqSupportClass.IN_VALIDATED_SUPPORT),
            ([1.0], HarqSupportClass.INTERPOLATION),
            ([7.0], HarqSupportClass.BOUNDARY_CLIPPED),
            ([31.0], HarqSupportClass.OUTSIDE_VALIDATED_SUPPORT),
            ([math.nan], HarqSupportClass.OUTSIDE_VALIDATED_SUPPORT),
        ]
        records = []
        for history, category in expected:
            self.assertEqual(classify_history(history, **kwargs), category)
            records.append({"support_class": category.value})
        coverage = support_coverage(records)
        self.assertEqual(coverage["count"], 5)
        self.assertEqual(coverage["validated_support_fraction"], 0.4)
        self.assertEqual(coverage["clipped_fraction"], 0.2)

    def test_collection_requires_prior_nacks_and_keeps_complete_history(self):
        events = [tx("e", 1, [-5.0], 0),
                  tx("e", 2, [-5.0, -4.0], 0),
                  tx("e", 3, [-5.0, -4.0, 18.0], 1,
                     access="scheduled_rescue")]
        records = collect_stage_conditioned_histories(events, scheme="H")
        self.assertEqual([row["attempt_index"] for row in records], [1, 2, 3])
        self.assertEqual(records[-1]["rv_history"], [0, 2, 3])
        self.assertEqual(records[-1]["effective_sinr_history_db"],
                         [-5.0, -4.0, 18.0])
        self.assertEqual(records[-1]["access_mode"], "SB")
        self.assertEqual(records[-1]["modeled_success_probability"], 0.75)
        with self.assertRaises(ValueError):
            collect_stage_conditioned_histories(
                [tx("bad", 1, [-5.0], 1),
                 tx("bad", 2, [-5.0, -4.0], 0)], scheme="R0")

    def test_window_keeps_pre_window_conditioning(self):
        events = [tx("e", 1, [-5.0], 0, slot=0),
                  tx("e", 2, [-5.0, -4.0], 0, slot=2)]
        records = collect_stage_conditioned_histories(
            events, scheme="R0", start_slot=1, stop_slot=3)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["attempt_index"], 2)

    def test_f_fresh_episode_resets_history_but_keeps_physical_attempt(self):
        events = [tx("old", 1, [-6.0], 0, physical=1),
                  tx("old", 2, [-6.0, -5.0], 0, physical=2),
                  tx("fresh", 1, [20.0], 0, physical=3,
                     access="scheduled_rescue", rv_history=[0]),
                  tx("fresh", 2, [20.0, 21.0], 1, physical=4,
                     access="scheduled_rescue", rv_history=[0, 2])]
        records = collect_stage_conditioned_histories(events, scheme="F")
        self.assertEqual([row["attempt_index"] for row in records], [1, 2, 1, 2])
        self.assertEqual([row["physical_attempt_index"] for row in records],
                         [1, 2, 3, 4])
        self.assertEqual(records[2]["rv_history"], [0])

    def test_wrong_history_dimensions_are_outside(self):
        self.assertEqual(classify_history(
            [-1.0, 0.0], validated_points_db=[[-1.0]],
            bounds_db=[[-4.0, 6.0]], global_guard_db=[-30.0, 30.0]),
            HarqSupportClass.OUTSIDE_VALIDATED_SUPPORT)

    def test_calibration_and_validation_seeds_are_deterministic_and_disjoint(self):
        config = load_yaml(Path("configs/harq_targeted_l2s_v0_4d.yaml"))
        calibration = [config["calibration"]["seeds_start"] + index
                       for index, _ in enumerate(
                           config["calibration"]["histories_db"])]
        validation = [config["validation"]["seeds_start"] + index
                      for index, _ in enumerate(
                          config["validation"]["histories_db"])]
        self.assertEqual(calibration[0], 61001)
        self.assertEqual(validation[0], 62001)
        self.assertFalse(set(calibration) & set(validation))

    def test_targeted_artifacts_reproduce_payload_hash_and_attempt4_accounting(self):
        for name in ("calibration", "validation"):
            artifact = json.loads(Path(
                f"results/harq_targeted_{name}_v0_4d.json").read_text())
            deterministic = {key: value for key, value in artifact.items()
                             if key not in {"runtime_seconds",
                                            "deterministic_payload_sha256"}}
            digest = hashlib.sha256(json.dumps(
                deterministic, sort_keys=True,
                separators=(",", ":")).encode()).hexdigest()
            self.assertEqual(digest, artifact["deterministic_payload_sha256"])
            for point in artifact["points"]:
                self.assertEqual(point["conditioned_population_entering_target"],
                                 point["target"]["trials"])
                self.assertEqual(point["target_successes"] +
                                 point["target_failures"],
                                 point["target"]["trials"])
        validation = json.loads(Path(
            "results/harq_targeted_validation_v0_4d.json").read_text())
        late = [point for point in validation["points"]
                if point["history_db"] == [-7.0, -4.0, 20.0, 20.0]][0]
        self.assertEqual(late["initial_samples"], 10000)
        self.assertEqual(late["target"]["trials"], 0)

    def test_support_artifact_reproduces_payload_hash(self):
        artifact = json.loads(Path(
            "results/harq_sys_support_v0_4d.json").read_text())
        deterministic = {key: value for key, value in artifact.items()
                         if key not in {"runtime_seconds",
                                        "deterministic_payload_sha256"}}
        digest = hashlib.sha256(json.dumps(
            deterministic, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(digest, artifact["deterministic_payload_sha256"])

    def test_old_artifacts_and_validation_regression_are_preserved(self):
        expected = {
            "configs/harq_history_l2s_v0_4c.yaml":
                "7adc7a2cefd6603a1166da4f5b382d6f43dd137a15b1d0b834d15ad8ebdffc8a",
            "results/harq_history_acceptance_validation_v0_4c.json":
                "e882d22bfa47099f06d5b9143ddadc5c77752d86cf6459894c2093a971b710e9",
            "docs/HARQ_HISTORY_L2S_V0_4C.md":
                "1ab96ae63b7552300380affea1d49a9e23b8e9e03111532c0ccf221887aa7aea",
        }
        for name, digest in expected.items():
            self.assertEqual(hashlib.sha256(Path(name).read_bytes()).hexdigest(),
                             digest)
        old = json.loads(Path(
            "results/harq_history_model_validation_v0_4c.json").read_text())
        for attempt in range(1, 5):
            self.assertLessEqual(old["candidates"]["D"]["metrics_by_attempt"]
                                 [str(attempt)]["maximum_absolute_probability_error"],
                                 0.05)

    def test_new_validation_decision_is_reproducible_and_probability_bounded(self):
        artifact = json.loads(Path(
            "results/harq_support_validation_v0_4d.json").read_text())
        self.assertIsNone(artifact["accepted_candidate"])
        self.assertFalse(artifact["all_targeted_points_sample_sufficient"])
        for candidate in artifact["candidates"].values():
            for row in candidate["validation_points"]:
                if row["model_probability"] is not None:
                    self.assertGreaterEqual(row["model_probability"], 0.0)
                    self.assertLessEqual(row["model_probability"], 1.0)


if __name__ == "__main__":
    unittest.main()
