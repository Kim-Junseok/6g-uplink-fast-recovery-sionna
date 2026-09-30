"""Deterministic V0.5 timing, recovery, and instrumentation gates."""

from __future__ import annotations

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
from ul_access.recovery import (
    FastArqPolicy,
    HarqController,
    LegacyRlcScheduledPolicy,
    ScheduledRescueConfig,
    ScheduledRescueMode,
)
from ul_access.resource import ScheduledResourceMode, ScheduledResourcePolicy


class FixedTraffic:
    def __init__(self, num_ues: int = 1):
        self.num_ues = num_ues

    def arrivals(self, slot):
        if slot:
            return ()
        return tuple(Packet(index, index, 0, 160,
                            measurement_cohort=True,
                            measurement_cohort_locked=True)
                     for index in range(self.num_ues))


class AlwaysNackBackend(PhyBackend):
    def __init__(self, num_ues: int):
        self._num_ues = num_ues

    @property
    def num_ues(self):
        return self._num_ues

    @property
    def num_subcarriers(self):
        return 6

    def evaluate(self, allocation, *, tx_power_dbm, mcs_index,
                 realization_seed):
        del allocation, tx_power_dbm, mcs_index, realization_seed
        count = self._num_ues
        return PhyOutcome(
            effective_sinr_db=(-20.0,) * count,
            tbler=(1.0,) * count, feedback=(0,) * count,
            decoded_bits=(0,) * count,
            post_equalization_sinr_active_db=((-20.0,),) * count,
            tx_power_per_active_re_w=(1e-4,) * count,
            bler=(1.0,) * count, crc_pass=(False,) * count,
            payload_correct=(False,) * count,
            undetected_error=(False,) * count,
            payload_hamming_distance=(1,) * count)


def simulate(*, num_ues=1, recovery=None, maximum_rlc=0,
             scheduled=False, poll=20, max_attempts=4,
             scheduled_capacity=2, traffic=None):
    if recovery is None:
        recovery = LegacyRlcScheduledPolicy(
            poll_retransmit_slots=poll, sr_period_slots=10,
            sr_control_processing_slots=1,
            scheduled_pusch_k2_slots=1)
    rescue = (ScheduledRescueConfig(
        mode=ScheduledRescueMode.FRESH_TB,
        request_delay_slots=0, scheduled_grant_delay_slots=0,
        trigger_after_attempts=5,
        maximum_physical_attempts_per_recovery_episode=max_attempts)
        if scheduled else None)
    return SlotSimulator(
        parameters=SimulationParameters(
            duration_slots=1, num_ues=num_ues, seed=9101,
            tx_power_dbm=(-8.0,) * num_ues,
            mcs_index=(10,) * num_ues, max_drain_slots=1000),
        traffic=traffic or FixedTraffic(num_ues),
        access=EventAddressedGrantFreeAccess(
            seed=9101, resource_pool_size=6,
            opportunity_period_slots=1, max_retries=3,
            retry_backoff_slots=0),
        phy_backend=AlwaysNackBackend(num_ues),
        harq=HarqController(
            max_attempts=max_attempts, feedback_delay_slots=1,
            pusch_spacing_slots=2),
        recovery=recovery, max_rlc_retransmissions=maximum_rlc,
        scheduled_rescue=rescue, scheduled_rlc_recovery=scheduled,
        direct_scheduled_k2_slots=1 if scheduled else None,
        scheduled_resource_policy=ScheduledResourcePolicy(
            f"shared_cap{scheduled_capacity}", ScheduledResourceMode.SHARED,
            6, scheduled_capacity),
        event_addressed_scheduler_ties=True,
        uniform_available_cb_selection=True,
        extended_outputs=True, enhanced_attempt_trace=True).run()


def events(result, name, payload=0):
    return [row for row in result["event_trace"]
            if row["event"] == name and row["payload_id"] == payload]


class V05TimingTest(unittest.TestCase):
    def test_frozen_config_contains_only_four_high_load_conditions(self):
        cfg = load_yaml(Path("configs/timing_rebaseline_v0_5.yaml"))
        self.assertEqual(cfg["milestone"], "V0.5-RB")
        self.assertEqual(len(cfg["scenarios"]), 4)
        self.assertTrue(all(row["rho"] == 0.90 and
                            row["resource_policy"] == "shared_cap2"
                            for row in cfg["scenarios"].values()))
        self.assertEqual(cfg["recovery"]["harq_pusch_spacing_slots"], 2)
        self.assertEqual(cfg["recovery"]["maximum_rlc_retransmissions"], 6)
        self.assertEqual(cfg["main_seeds"], list(range(9101, 9109)))
        self.assertEqual(cfg["phy"]["rv_sequence"], [0, 2, 3, 1])
        self.assertEqual(cfg["phy"]["maximum_attempts"], 4)
        self.assertEqual(cfg["traffic"]["num_ues"], 3000)
        self.assertEqual(cfg["stopping"]["warmup_new_packets"], 500)
        self.assertEqual(cfg["stopping"]["measured_new_packets"], 2500)

    def test_cb_harq_timeline_and_actual_feedback_slots(self):
        result = simulate(recovery=FastArqPolicy(
            failure_indication_delay_slots=1))
        attempts = events(result, "phy_transmission")
        feedback = [row for row in result["event_trace"]
                    if row["event"] == "harq_nack"]
        self.assertEqual([row["slot"] for row in attempts], [0, 2, 4, 6])
        self.assertEqual([row["feedback_available_slot"] for row in attempts],
                         [1, 3, 5, 7])
        self.assertEqual([row["slot"] for row in feedback], [1, 3, 5, 7])

    def test_legacy_15_and_20_timers_sr_control_k2_are_harq_independent(self):
        for poll in (15, 20):
            with self.subTest(poll=poll):
                result = simulate(scheduled=True, poll=poll, maximum_rlc=1,
                                  max_attempts=100)
                timing = result["legacy_timing_rows"][0]
                self.assertEqual(timing["rlc_timer_start_slot"], 0)
                self.assertEqual(timing["rlc_timer_expiry_slot"], poll)
                self.assertEqual(timing["sr_wait_slots"],
                                 (timing["sr_phase"] - poll) % 10)
                self.assertEqual(timing["grant_ready_slot"],
                                 timing["actual_sr_slot"] + 1)
                self.assertEqual(timing["scheduled_first_tx_slot"],
                                 timing["actual_sr_slot"] + 2)
                initial_failures = [
                    row for row in events(result, "final_harq_failure")
                    if row["slot"] <= poll]
                self.assertEqual(initial_failures, [])

    def test_sr_phase_is_global_ue_stable_and_wait_is_bounded(self):
        first = LegacyRlcScheduledPolicy(poll_retransmit_slots=20)
        second = LegacyRlcScheduledPolicy(poll_retransmit_slots=15)
        for ue_id in range(100):
            self.assertEqual(first.sr_phase(ue_id), second.sr_phase(ue_id))
            for eligible in range(10):
                wait = first.next_sr_slot(eligible, ue_id) - eligible
                self.assertIn(wait, range(10))

    def test_fast_paths_do_not_consult_legacy_timer_or_sr(self):
        first = simulate(
            recovery=FastArqPolicy(failure_indication_delay_slots=1),
            maximum_rlc=1)
        second = simulate(
            recovery=FastArqPolicy(failure_indication_delay_slots=1),
            maximum_rlc=1)
        self.assertEqual(first, second)
        self.assertFalse(any(row["event"].startswith("legacy_")
                             for row in first["event_trace"]))

        scheduled = simulate(
            recovery=FastArqPolicy(failure_indication_delay_slots=1),
            maximum_rlc=1, scheduled=True)
        scheduled_tx = [row for row in events(scheduled, "phy_transmission")
                        if row["transmission_access"] == "scheduled_rescue"]
        self.assertEqual(scheduled_tx[0]["slot"], 8)
        self.assertFalse(any(row["event"].startswith("legacy_")
                             for row in scheduled["event_trace"]))

    def test_shared_cap_queue_delay_uses_actual_tx_for_feedback(self):
        result = simulate(
            num_ues=3,
            recovery=FastArqPolicy(failure_indication_delay_slots=1),
            maximum_rlc=1, scheduled=True)
        recovery_tx = [row for row in result["event_trace"]
                       if row["event"] == "phy_transmission" and
                       row["rlc_retx_index"] == 1]
        first = {}
        for row in recovery_tx:
            first.setdefault(row["payload_id"], row)
        self.assertEqual(sorted(row["slot"] for row in first.values()),
                         [8, 8, 9])
        self.assertTrue(all(row["feedback_available_slot"] == row["slot"] + 1
                            for row in recovery_tx))
        self.assertTrue(all(row["scheduled_grant_dci_slot"] == row["slot"] - 1
                            for row in recovery_tx))
        by_episode = {}
        for row in recovery_tx:
            by_episode.setdefault(row["harq_episode_id"], []).append(row)
        self.assertTrue(all(
            all(later["slot"] - earlier["slot"] >= 2
                for earlier, later in zip(ordered, ordered[1:]))
            for ordered in (sorted(group, key=lambda row: row["harq_attempt"])
                            for group in by_episode.values())))
        self.assertTrue(all(row["scheduled_service_count"] <= 2 and
                            row["cb_prbs_available"] >= 4 and
                            row["pool_invariant"]
                            for row in result["slot_resource_rows"]))
        scheduled_resources = {
            (row["slot"], row["resource_id"]) for row in recovery_tx}
        cb_resources = {
            (row["slot"], row["resource_id"])
            for row in result["event_trace"]
            if row["event"] == "phy_transmission" and
            row["transmission_access"] == "grant_free"}
        self.assertFalse(scheduled_resources & cb_resources)

    def test_resource_delayed_cb_feedback_uses_actual_tx_slot(self):
        class StaggeredTraffic:
            num_ues = 7

            def arrivals(self, slot):
                if slot == 0:
                    packets = [Packet(
                        index, index, 0, 160, measurement_cohort=True,
                        measurement_cohort_locked=True) for index in range(6)]
                    packets.append(Packet(
                        6, 6, 0, 160, next_eligible_slot=2,
                        measurement_cohort=True,
                        measurement_cohort_locked=True))
                    return tuple(packets)
                return ()

        result = simulate(
            num_ues=7,
            recovery=FastArqPolicy(failure_indication_delay_slots=1),
            maximum_rlc=1, scheduled=True, scheduled_capacity=6,
            traffic=StaggeredTraffic())
        late = [row for row in events(result, "phy_transmission", payload=6)
                if row["rlc_retx_index"] == 0]
        self.assertEqual([row["slot"] for row in late], [2, 4, 6, 9])
        self.assertEqual(late[-1]["feedback_available_slot"], 10)

    def test_v0_5_instrumentation_is_complete_and_replays(self):
        first = simulate(scheduled=True, poll=20, maximum_rlc=1)
        second = simulate(scheduled=True, poll=20, maximum_rlc=1)
        self.assertEqual(first, second)
        packet_required = {
            "t_poll_slots", "sr_period_slots", "sr_phase",
            "number_of_rlc_retransmissions", "terminal_reason",
            "receiver_accepted", "payload_correct", "undetected_error",
            "completion_slot"}
        attempt_required = {
            "payload_id", "rlc_retx_index", "harq_episode_id",
            "redundancy_version", "transmission_access", "actual_tx_slot",
            "feedback_available_slot", "resource_id", "scheduled_queue_wait"}
        timing_required = {
            "rlc_timer_start_slot", "rlc_timer_expiry_slot",
            "sr_eligible_slot", "actual_sr_slot", "sr_wait_slots",
            "grant_ready_slot", "scheduled_request_queue_entry",
            "scheduled_grant_dci_slot", "scheduled_first_tx_slot"}
        slot_required = {
            "scheduled_ready_demand", "scheduled_queue_length",
            "scheduled_attempts_admitted", "scheduled_prbs_used",
            "cb_prbs_available", "cb_prbs_occupied", "cb_attempt_count"}
        self.assertTrue(packet_required <= first["packet_rows"][0].keys())
        self.assertTrue(attempt_required <= next(
            row for row in first["event_trace"]
            if row["event"] == "phy_transmission").keys())
        self.assertTrue(timing_required <= first["legacy_timing_rows"][0].keys())
        self.assertTrue(slot_required <= first["slot_resource_rows"][0].keys())

    def test_t6_boundary_has_no_seventh_retransmission(self):
        result = simulate(scheduled=True, poll=5, maximum_rlc=6)
        packet = result["packet_rows"][0]
        self.assertEqual(packet["number_of_rlc_retransmissions"], 6)
        self.assertEqual(packet["drop_reason"], "rlc_retransmission_limit")
        indices = {row["rlc_retx_index"] for row in result["event_trace"]
                   if row["event"] == "phy_transmission"}
        self.assertEqual(indices, set(range(7)))


if __name__ == "__main__":
    unittest.main()
