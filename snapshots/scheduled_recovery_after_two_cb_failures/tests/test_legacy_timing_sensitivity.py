"""Deterministic gates for Legacy-CB recovery-delay sensitivity."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

from ul_access.config import load_yaml
from ul_access.phy import PhyBackend, PhyOutcome
from ul_access.protocol import (
    EventAddressedGrantFreeAccess,
    Packet,
    SimulationParameters,
    SlotSimulator,
)
from ul_access.recovery import HarqController, PollRetransmitPolicy


class OnePacketTraffic:
    def arrivals(self, slot):
        return ((Packet(0, 0, 0, 160, measurement_cohort=True,
                        measurement_cohort_locked=True),)
                if slot == 0 else ())


class AlwaysNackBackend(PhyBackend):
    @property
    def num_ues(self):
        return 1

    @property
    def num_subcarriers(self):
        return 6

    def evaluate(self, allocation, *, tx_power_dbm, mcs_index,
                 realization_seed):
        del allocation, tx_power_dbm, mcs_index, realization_seed
        return PhyOutcome(
            effective_sinr_db=(-20.0,), tbler=(1.0,), feedback=(0,),
            decoded_bits=(0,), post_equalization_sinr_active_db=((-20.0,),),
            tx_power_per_active_re_w=(1e-4,), bler=(1.0,),
            crc_pass=(False,), payload_correct=(False,),
            undetected_error=(False,), payload_hamming_distance=(1,))


def simulate(timer: int):
    return SlotSimulator(
        parameters=SimulationParameters(
            duration_slots=1, num_ues=1, seed=9101,
            tx_power_dbm=(-8.0,), mcs_index=(10,), max_drain_slots=100),
        traffic=OnePacketTraffic(),
        access=EventAddressedGrantFreeAccess(
            seed=9101, resource_pool_size=6, opportunity_period_slots=1,
            max_retries=3, retry_backoff_slots=0),
        phy_backend=AlwaysNackBackend(),
        harq=HarqController(max_attempts=4, feedback_delay_slots=1),
        recovery=PollRetransmitPolicy(poll_retransmit_slots=timer),
        max_rlc_retransmissions=1, extended_outputs=True,
        enhanced_attempt_trace=True,
    ).run()


def campaign_module():
    path = Path("experiments/24_commmag_priority_campaign.py")
    spec = importlib.util.spec_from_file_location("priority_campaign", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class LegacyTimingSensitivityTest(unittest.TestCase):
    def test_max_boundary_and_candidate_distinctness(self):
        packet = Packet(0, 0, 0, 160, first_tx_slot=0)
        expected = {5: (5, 6), 10: (10, 11),
                    15: (15, 16), 20: (20, 21)}
        for timer, (expiry, eligible) in expected.items():
            with self.subTest(timer=timer):
                decision = PollRetransmitPolicy(
                    poll_retransmit_slots=timer).on_final_harq_failure(
                        packet, current_slot=4)
                self.assertEqual(decision.timer_expiry_slot, expiry)
                self.assertEqual(decision.eligible_slot, eligible)
        harq_bound = PollRetransmitPolicy(
            poll_retransmit_slots=4).on_final_harq_failure(packet, 4)
        self.assertEqual(harq_bound.eligible_slot, 5)

    def test_effective_gap_instrumentation_and_replay(self):
        first = simulate(5)
        second = simulate(5)
        self.assertEqual(first, second)
        row = first["packet_rows"][0]
        self.assertEqual(row["initial_harq_final_failure_slot"], 4)
        self.assertEqual(row["legacy_timer_expiry_slot"], 5)
        self.assertEqual(row["legacy_recovery_eligible_slot"], 6)
        self.assertEqual(row["next_cb_recovery_tx_slot"], 6)
        self.assertEqual(row["effective_final_harq_to_recovery_gap"], 2)
        self.assertEqual(row["legacy_timer_wait_slots"], 2)
        self.assertEqual(row["cb_resource_wait_slots"], 0)
        self.assertEqual(row["first_recovery_harq_duration_slots"], 4)
        summary = campaign_module().legacy_timing_summary(
            [{"legacy_recovery_eligible_slot":
              row["legacy_recovery_eligible_slot"],
              "legacy_timer_expiry_slot": row["legacy_timer_expiry_slot"],
              "initial_harq_final_failure_slot":
              row["initial_harq_final_failure_slot"],
              "effective_final_harq_to_recovery_gap":
              row["effective_final_harq_to_recovery_gap"],
              "legacy_timer_wait_slots": row["legacy_timer_wait_slots"],
              "cb_resource_wait_slots": row["cb_resource_wait_slots"],
              "first_recovery_harq_duration_slots":
              row["first_recovery_harq_duration_slots"]}])
        self.assertEqual(summary["timer_binding_fraction"], 1.0)
        self.assertEqual(
            summary["effective_final_harq_to_recovery_gap"]["mean"], 2.0)

    def test_fast_recovery_factory_does_not_read_legacy_proxy(self):
        module = campaign_module()
        base = {
            "legacy_poll_retransmit_slots": 20,
            "fast_arq_delay_slots": 1,
        }
        changed = {**base, "legacy_poll_retransmit_slots": 5}
        fast = {"scheme": "Fast-CB", "rho": 0.9}
        scheduled = {"scheme": "B/Fast-SB", "rho": 0.9,
                     "resource_policy": "shared_cap2"}
        for scenario in (fast, scheduled):
            first = module.p2_recovery_policy(base, scenario)
            second = module.p2_recovery_policy(changed, scenario)
            self.assertEqual(type(first), type(second))
            self.assertEqual(first.__dict__, second.__dict__)
            packet = Packet(0, 0, 0, 160, first_tx_slot=0)
            self.assertEqual(
                first.on_final_harq_failure(packet, 4),
                second.on_final_harq_failure(packet, 4))

    def test_sensitivity_config_changes_only_legacy_timer_by_scenario(self):
        cfg = load_yaml(Path(
            "configs/legacy_recovery_delay_sensitivity_v0_4g.yaml"))
        scenarios = list(cfg["scenarios"].values())
        self.assertEqual(
            {row["legacy_poll_retransmit_slots"] for row in scenarios},
            {5, 10, 15})
        common = [{key: value for key, value in row.items()
                   if key != "legacy_poll_retransmit_slots"}
                  for row in scenarios]
        self.assertTrue(all(row == common[0] for row in common))
        self.assertTrue(all(row["scheme"] == "Legacy-CB" and
                            row["rho"] == 0.9 for row in scenarios))
        module = campaign_module()
        for scenario_id in cfg["scenarios"]:
            module.validate_p2_contract(cfg, scenario_id, 9101)


if __name__ == "__main__":
    unittest.main()
