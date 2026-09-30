"""Frozen configuration checks for the V0.7 rho=0.70 campaign."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

from ul_access.config import load_yaml


def load_runner():
    path = Path("experiments/24_commmag_priority_campaign.py")
    spec = importlib.util.spec_from_file_location("campaign_v07", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class V07LoadRobustnessTest(unittest.TestCase):
    def setUp(self):
        self.cfg = load_yaml(Path("configs/load_robustness_v0_7.yaml"))

    def test_only_common_scientific_change_is_traffic_load(self):
        accepted = load_yaml(Path("configs/p3_fk3_v0_6.yaml"))
        for section in ("phy", "resource_pool", "resource_policies", "channel",
                        "stopping", "recovery", "rng", "main_seeds"):
            self.assertEqual(self.cfg[section], accepted[section])
        self.assertEqual(self.cfg["traffic"], {
            "model": "independent_per_ue_poisson", "num_ues": 3000,
            "resource_capacity_attempts_per_slot": 6,
            "rho_values": [0.70],
            "aggregate_arrival_rate_per_slot": {"0.70": 4.2},
            "per_ue_arrival_rate_per_slot": {"0.70": 0.0014},
            "per_ue_arrival_rate_per_second": {"0.70": 1.4}})

    def test_exact_eight_frozen_schemes(self):
        cells = {(row["scheme"], row.get("rescue_mode"),
                  row.get("trigger_after_attempts"),
                  row.get("maximum_physical_attempts_per_recovery_episode"),
                  row.get("legacy_poll_retransmit_slots"))
                 for row in self.cfg["scenarios"].values()}
        self.assertEqual(cells, {
            ("Legacy-RLC/SB", None, None, None, 20),
            ("Legacy-RLC/SB", None, None, None, 15),
            ("Fast-CB", None, None, None, None),
            ("B/Fast-SB", None, None, None, None),
            ("H", "harq_preserving", 2, 4, None),
            ("F", "fresh_tb", 2, 6, None),
            ("H(K=3)", "harq_preserving", 3, 4, None),
            ("F(K=3)", "fresh_tb", 3, 7, None)})
        self.assertTrue(all(row["rho"] == 0.70 and
                            row["resource_policy"] == "shared_cap2"
                            for row in self.cfg["scenarios"].values()))

    def test_runner_contract_accepts_all_64_cells(self):
        runner = load_runner()
        for scenario in self.cfg["scenarios"]:
            for seed in self.cfg["main_seeds"]:
                runner.validate_p2_contract(self.cfg, scenario, seed)

    def test_runner_rejects_unapproved_load(self):
        runner = load_runner()
        scenario = next(iter(self.cfg["scenarios"]))
        self.cfg["scenarios"][scenario]["rho"] = 0.90
        with self.assertRaisesRegex(RuntimeError, "frozen-contract mismatch"):
            runner.validate_p2_contract(self.cfg, scenario, 9101)


if __name__ == "__main__":
    unittest.main()
