"""Deterministic V0.6 P3 HARQ-state and timing gates."""

from __future__ import annotations

import unittest
from pathlib import Path

import torch

from ul_access.config import load_yaml
from ul_access.phy import (ActiveCompactedFrequencySelectivePrbReceiver,
                           DirectLlsHarqBackend)
from ul_access.protocol import (EventAddressedGrantFreeAccess, Packet,
                                SimulationParameters, SlotSimulator)
from ul_access.recovery import (FastArqPolicy, HarqController,
                                ScheduledRescueConfig, ScheduledRescueMode)
from ul_access.resource import ScheduledResourceMode, ScheduledResourcePolicy


class OnePacket:
    num_ues = 1

    def arrivals(self, slot):
        return ((Packet(0, 0, 0, 160, measurement_cohort=True,
                        measurement_cohort_locked=True),)
                if slot == 0 else ())


def run(mode: ScheduledRescueMode, *, trigger=2, physical_limit=4,
        max_rlc_retransmissions=0):
    receiver = ActiveCompactedFrequencySelectivePrbReceiver({
        "num_ues": 1, "num_prbs": 6, "num_bs_receive_antennas": 2,
        "noise_power_w": 1e-5, "subcarrier_spacing_hz": 15_000.0,
        "delay_spread_s": 300e-9, "carrier_frequency_hz": 700e6})
    backend = DirectLlsHarqBackend(receiver, run_seed=9101,
                                   audit_enabled=True)
    result = SlotSimulator(
        parameters=SimulationParameters(
            1, 1, 9101, (-100.0,), (10,), max_drain_slots=200),
        traffic=OnePacket(),
        access=EventAddressedGrantFreeAccess(
            seed=9101, resource_pool_size=6, opportunity_period_slots=1,
            max_retries=3, retry_backoff_slots=0),
        phy_backend=backend,
        harq=HarqController(max_attempts=4, feedback_delay_slots=1,
                            pusch_spacing_slots=2),
        recovery=FastArqPolicy(failure_indication_delay_slots=1),
        max_rlc_retransmissions=max_rlc_retransmissions,
        scheduled_rescue=ScheduledRescueConfig(
            mode=mode, request_delay_slots=0, scheduled_grant_delay_slots=0,
            trigger_after_attempts=trigger,
            maximum_physical_attempts_per_recovery_episode=physical_limit),
        scheduled_rlc_recovery=True, direct_scheduled_k2_slots=1,
        scheduled_resource_policy=ScheduledResourcePolicy(
            "shared_cap2", ScheduledResourceMode.SHARED, 6, 2),
        event_addressed_scheduler_ties=True,
        uniform_available_cb_selection=True, extended_outputs=True,
        enhanced_attempt_trace=True).run()
    attempts = [row for row in result["event_trace"]
                if row["event"] == "phy_transmission"]
    return result, backend, attempts


class V06P3Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        torch.set_num_threads(1)
        torch.use_deterministic_algorithms(True)

    def test_frozen_config(self):
        cfg = load_yaml(Path("configs/p3_bhf_v0_6.yaml"))
        self.assertEqual(cfg["milestone"], "V0.6-P3")
        self.assertEqual(cfg["main_seeds"], list(range(9101, 9109)))
        self.assertEqual(cfg["traffic"]["rho_values"], [0.90])
        self.assertEqual(cfg["phy"]["rv_sequence"], [0, 2, 3, 1])
        self.assertEqual(cfg["recovery"]["maximum_rlc_retransmissions"], 6)
        self.assertEqual({row["scheme"] for row in cfg["scenarios"].values()},
                         {"H", "F"})

    def test_h_preserves_episode_llrs_and_timeline(self):
        result, backend, attempts = run(ScheduledRescueMode.HARQ_PRESERVING)
        self.assertEqual([row["slot"] for row in attempts], [0, 2, 4, 6])
        self.assertEqual([row["redundancy_version"] for row in attempts],
                         [0, 2, 3, 1])
        self.assertEqual(len({row["tb_id"] for row in attempts}), 1)
        self.assertEqual(len({row["harq_episode_id"] for row in attempts}), 1)
        self.assertEqual([row["transmission_access"] for row in attempts],
                         ["grant_free", "grant_free",
                          "scheduled_rescue", "scheduled_rescue"])
        self.assertEqual([row["llr_observation_shapes"] for row in
                          backend.audit_records],
                         [[[1, 504]], [[1, 504], [1, 504]],
                          [[1, 504], [1, 504], [1, 504]],
                          [[1, 504], [1, 504], [1, 504], [1, 504]]])
        trigger = next(row for row in result["event_trace"]
                       if row["event"] == "scheduled_rescue_triggered")
        self.assertEqual(trigger["slot"], 3)

    def test_h_k3_triggers_only_after_cb_rv3_and_schedules_rv1(self):
        result, backend, attempts = run(
            ScheduledRescueMode.HARQ_PRESERVING, trigger=3)
        self.assertEqual([row["slot"] for row in attempts], [0, 2, 4, 6])
        self.assertEqual([row["redundancy_version"] for row in attempts],
                         [0, 2, 3, 1])
        self.assertEqual([row["transmission_access"] for row in attempts],
                         ["grant_free", "grant_free", "grant_free",
                          "scheduled_rescue"])
        trigger = next(row for row in result["event_trace"]
                       if row["event"] == "scheduled_rescue_triggered")
        self.assertEqual(trigger["slot"], 5)
        self.assertFalse(any(row["event"] == "scheduled_rescue_triggered" and
                             row["slot"] == 3 for row in result["event_trace"]))
        self.assertEqual(len({row["tb_id"] for row in attempts}), 1)
        self.assertEqual(len({row["harq_episode_id"] for row in attempts}), 1)
        self.assertEqual(backend.audit_records[-1]["rv_history"], [0, 2, 3, 1])
        self.assertEqual(backend.audit_records[-1]["decoder_input_shape"],
                         [1, 4, 504])
        packet = result["packet_rows"][0]
        self.assertEqual(packet["rv3_tx_slot"], 4)
        self.assertEqual(packet["rv3_nack_slot"], 5)
        self.assertEqual(packet["rescue_trigger_rv"], 3)
        self.assertEqual(packet["rescue_trigger_to_tx_delay"], 1)
        self.assertFalse(any(row["event"].startswith("legacy_")
                             for row in result["event_trace"]))

    def test_k3_config_changes_only_triggered_h_condition(self):
        cfg = load_yaml(Path("configs/p3_hk3_v0_6.yaml"))
        self.assertEqual(cfg["milestone"], "V0.6-K3")
        self.assertEqual(len(cfg["scenarios"]), 1)
        scenario = next(iter(cfg["scenarios"].values()))
        self.assertEqual(scenario, {
            "scheme": "H(K=3)", "rho": 0.90,
            "resource_policy": "shared_cap2",
            "rescue_mode": "harq_preserving",
            "trigger_after_attempts": 3,
            "maximum_physical_attempts_per_recovery_episode": 4})
        accepted = load_yaml(Path("configs/p3_bhf_v0_6.yaml"))
        for section in ("phy", "resource_pool", "resource_policies", "channel",
                        "traffic", "stopping", "recovery", "rng", "main_seeds"):
            self.assertEqual(cfg[section], accepted[section])

    def test_f3_config_changes_only_harq_state_semantics(self):
        cfg = load_yaml(Path("configs/p3_fk3_v0_6.yaml"))
        self.assertEqual(cfg["milestone"], "V0.6-F3")
        self.assertEqual(len(cfg["scenarios"]), 1)
        scenario = next(iter(cfg["scenarios"].values()))
        self.assertEqual(scenario, {
            "scheme": "F(K=3)", "rho": 0.90,
            "resource_policy": "shared_cap2",
            "rescue_mode": "fresh_tb",
            "trigger_after_attempts": 3,
            "maximum_physical_attempts_per_recovery_episode": 7})
        h3 = load_yaml(Path("configs/p3_hk3_v0_6.yaml"))
        for section in ("phy", "resource_pool", "resource_policies", "channel",
                        "traffic", "stopping", "recovery", "rng", "main_seeds"):
            self.assertEqual(cfg[section], h3[section])

    def test_f3_matches_h3_before_trigger_then_starts_fresh_rv0(self):
        h_result, h_backend, h_attempts = run(
            ScheduledRescueMode.HARQ_PRESERVING, trigger=3,
            physical_limit=4)
        f_result, f_backend, f_attempts = run(
            ScheduledRescueMode.FRESH_TB, trigger=3, physical_limit=7)
        fields = ("slot", "payload_id", "ue_id", "tb_id", "harq_episode_id",
                  "resource_id", "redundancy_version", "effective_sinr_db",
                  "phy_feedback", "crc_pass", "payload_correct")
        self.assertEqual(
            [{key: row[key] for key in fields} for row in h_attempts[:3]],
            [{key: row[key] for key in fields} for row in f_attempts[:3]])
        self.assertEqual([row["slot"] for row in f_attempts],
                         [0, 2, 4, 6, 8, 10, 12])
        self.assertEqual([row["redundancy_version"] for row in f_attempts],
                         [0, 2, 3, 0, 2, 3, 1])
        self.assertEqual([row["transmission_access"] for row in f_attempts],
                         ["grant_free"] * 3 + ["scheduled_rescue"] * 4)
        self.assertEqual(h_attempts[3]["redundancy_version"], 1)
        self.assertEqual(h_attempts[3]["harq_episode_id"],
                         h_attempts[2]["harq_episode_id"])
        self.assertEqual(f_attempts[3]["redundancy_version"], 0)
        self.assertEqual(f_attempts[3]["payload_id"], f_attempts[2]["payload_id"])
        self.assertNotEqual(f_attempts[3]["tb_id"], f_attempts[2]["tb_id"])
        self.assertNotEqual(f_attempts[3]["harq_episode_id"],
                            f_attempts[2]["harq_episode_id"])
        fresh_episode = f_attempts[3]["harq_episode_id"]
        fresh_records = [row for row in f_backend.audit_records
                         if row["harq_episode_id"] == fresh_episode]
        self.assertEqual([row["rv_history"] for row in fresh_records],
                         [[0], [0, 2], [0, 2, 3], [0, 2, 3, 1]])
        self.assertEqual(fresh_records[0]["decoder_input_shape"], [1, 504])
        h_trigger = next(row for row in h_result["event_trace"]
                         if row["event"] == "scheduled_rescue_triggered")
        f_trigger = next(row for row in f_result["event_trace"]
                         if row["event"] == "scheduled_rescue_triggered")
        self.assertEqual((h_trigger["slot"], f_trigger["slot"]), (5, 5))
        abandoned = next(row for row in f_result["event_trace"]
                         if row["event"] ==
                         "harq_episode_abandoned_for_rescue")
        self.assertEqual(abandoned["slot"], 5)
        self.assertFalse(any(row["event"].startswith("legacy_")
                             for row in f_result["event_trace"]))
        self.assertTrue(all(row["feedback_available_slot"] == row["slot"] + 1
                            for row in f_attempts))

    def test_f_resets_episode_llrs_and_uses_fresh_four_attempts(self):
        result, backend, attempts = run(
            ScheduledRescueMode.FRESH_TB, physical_limit=6)
        self.assertEqual([row["slot"] for row in attempts], [0, 2, 4, 6, 8, 10])
        self.assertEqual([row["redundancy_version"] for row in attempts],
                         [0, 2, 0, 2, 3, 1])
        self.assertEqual(attempts[0]["payload_id"], attempts[2]["payload_id"])
        self.assertNotEqual(attempts[1]["tb_id"], attempts[2]["tb_id"])
        self.assertNotEqual(attempts[1]["harq_episode_id"],
                            attempts[2]["harq_episode_id"])
        fresh = [row for row in backend.audit_records
                 if row["harq_episode_id"] == attempts[2]["harq_episode_id"]]
        self.assertEqual(fresh[0]["decoder_input_shape"], [1, 504])
        self.assertEqual(fresh[0]["rv_history"], [0])
        self.assertEqual(next(row for row in result["event_trace"]
                              if row["event"] == "scheduled_rescue_triggered")["slot"], 3)

    def test_b_is_post_harq_fresh_and_fast_paths_have_no_sr(self):
        result, backend, attempts = run(
            ScheduledRescueMode.FRESH_TB, trigger=5, physical_limit=4,
            max_rlc_retransmissions=1)
        self.assertEqual([row["redundancy_version"] for row in attempts],
                         [0, 2, 3, 1, 0, 2, 3, 1])
        self.assertEqual([row["slot"] for row in attempts[:5]],
                         [0, 2, 4, 6, 8])
        self.assertNotEqual(attempts[3]["harq_episode_id"],
                            attempts[4]["harq_episode_id"])
        self.assertEqual(attempts[4]["transmission_access"],
                         "scheduled_rescue")
        self.assertEqual(backend.audit_records[4]["decoder_input_shape"],
                         [1, 504])
        self.assertFalse(any(row["event"].startswith("legacy_")
                             for row in result["event_trace"]))
        self.assertTrue(all(row["feedback_available_slot"] == row["slot"] + 1
                            for row in attempts))
        self.assertTrue(all(row["scheduled_service_count"] <= 2 and
                            row["cb_prbs_available"] >= 4 and
                            row["pool_invariant"]
                            for row in result["slot_resource_rows"]))


if __name__ == "__main__":
    unittest.main()
