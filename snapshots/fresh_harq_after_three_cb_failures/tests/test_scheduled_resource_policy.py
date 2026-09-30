"""Deterministic tests for scheduled-recovery resource coupling policies."""

from __future__ import annotations

import unittest
from pathlib import Path

from ul_access.config import load_yaml
from ul_access.phy import (
    ActiveCompactedFrequencySelectivePrbReceiver,
    DirectLlsHarqBackend,
    PhyBackend,
    PhyOutcome,
)
from ul_access.protocol import (
    EventAddressedGrantFreeAccess,
    Packet,
    SimulationParameters,
    SlotSimulator,
)
from ul_access.recovery import (
    FastArqPolicy,
    HarqController,
    ScheduledRescueConfig,
    ScheduledRescueMode,
)
from ul_access.resource import (
    ScheduledResourceMode,
    ScheduledResourcePolicy,
    shared_uncapped_policy,
)


class FourPacketTraffic:
    def arrivals(self, slot):
        if slot:
            return ()
        return tuple(Packet(
            ue, ue, 0, 160, measurement_cohort=True,
            measurement_cohort_locked=True) for ue in range(4))


class AlwaysNackBackend(PhyBackend):
    def __init__(self):
        self._num_ues = 4
        self._num_subcarriers = 6

    @property
    def num_ues(self):
        return self._num_ues

    @property
    def num_subcarriers(self):
        return self._num_subcarriers

    def evaluate(self, allocation, *, tx_power_dbm, mcs_index,
                 realization_seed):
        del tx_power_dbm, mcs_index, realization_seed
        active = allocation.allocated_re_per_ue()[0].tolist()
        return PhyOutcome(
            effective_sinr_db=tuple(-20.0 if count else None for count in active),
            tbler=tuple(1.0 if count else None for count in active),
            feedback=tuple(0 if count else -1 for count in active),
            decoded_bits=(0,) * 4,
            post_equalization_sinr_active_db=tuple(
                (-20.0,) if count else () for count in active),
            tx_power_per_active_re_w=tuple(
                1e-4 if count else None for count in active),
            bler=tuple(1.0 if count else None for count in active),
            crc_pass=tuple(False if count else None for count in active),
            payload_correct=tuple(False if count else None for count in active),
            undetected_error=tuple(False if count else None for count in active),
            payload_hamming_distance=tuple(1 if count else None for count in active),
        )


def simulate(policy=None):
    return SlotSimulator(
        parameters=SimulationParameters(
            duration_slots=1, num_ues=4, seed=9101,
            tx_power_dbm=(-8.0,) * 4, mcs_index=(10,) * 4,
            max_drain_slots=100),
        traffic=FourPacketTraffic(),
        access=EventAddressedGrantFreeAccess(
            seed=9101, resource_pool_size=6, opportunity_period_slots=1,
            max_retries=3, retry_backoff_slots=0),
        phy_backend=AlwaysNackBackend(),
        harq=HarqController(max_attempts=4, feedback_delay_slots=1),
        recovery=FastArqPolicy(failure_indication_delay_slots=1),
        max_rlc_retransmissions=1,
        scheduled_rescue=ScheduledRescueConfig(
            mode=ScheduledRescueMode.FRESH_TB,
            request_delay_slots=1, scheduled_grant_delay_slots=2,
            trigger_after_attempts=5,
            maximum_physical_attempts_per_recovery_episode=4),
        scheduled_rlc_recovery=True,
        scheduled_rlc_uses_request_grant=True,
        scheduled_resource_policy=policy,
        uniform_available_cb_selection=True,
        event_addressed_scheduler_ties=True,
        extended_outputs=True,
        enhanced_attempt_trace=True,
    ).run()


class ScheduledResourcePolicyTest(unittest.TestCase):
    def test_p2_5_scenarios_differ_only_by_resource_policy(self):
        cfg = load_yaml(Path("configs/scheduled_resource_coupling_v0_4g.yaml"))
        scenarios = list(cfg["scenarios"].values())
        first = {key: value for key, value in scenarios[0].items()
                 if key != "resource_policy"}
        second = {key: value for key, value in scenarios[1].items()
                  if key != "resource_policy"}
        self.assertEqual(first, second)
        self.assertEqual(
            {row["resource_policy"] for row in scenarios},
            {"shared_cap2", "dedicated_sb2"})

    def test_shared_cap_enforces_capacity_and_preserves_excess_queue(self):
        policy = ScheduledResourcePolicy(
            "shared_cap2", ScheduledResourceMode.SHARED, 6, 2)
        result = simulate(policy)
        rows = result["slot_resource_rows"]
        self.assertTrue(all(row["scheduled_prbs_used"] <= 2 for row in rows))
        self.assertTrue(all(row["cb_prbs_available"] >= 4 for row in rows))
        self.assertTrue(all(row["total_base_prbs_occupied"] <= 6 for row in rows))
        self.assertTrue(all(row["cb_attempt_count"] >= row["cb_prbs_occupied"]
                            for row in rows))
        self.assertEqual(sum(row["scheduled_requests_in_slot"] for row in rows),
                         result["rescue"]["requests"])
        congested = [row for row in rows if row["scheduled_ready_demand"] > 2]
        self.assertTrue(congested)
        self.assertTrue(all(row["scheduled_service_count"] == 2
                            for row in congested))

    def test_dedicated_sb_is_orthogonal_and_keeps_six_cb_prbs(self):
        policy = ScheduledResourcePolicy(
            "dedicated_sb2", ScheduledResourceMode.DEDICATED, 6, 2)
        result = simulate(policy)
        rows = result["slot_resource_rows"]
        self.assertTrue(all(row["cb_prbs_available"] == 6 for row in rows))
        self.assertTrue(all(row["scheduled_prbs_used"] == 0 for row in rows))
        self.assertTrue(all(row["dedicated_sb_resources_used"] <= 2
                            for row in rows))
        self.assertEqual(sum(row["scheduled_requests_in_slot"] for row in rows),
                         result["rescue"]["requests"])
        scheduled = [event for event in result["event_trace"]
                     if event["event"] == "phy_transmission" and
                     event["transmission_access"] == "scheduled_rescue"]
        self.assertTrue(scheduled)
        self.assertTrue(all(event["resource_domain"] == "dedicated_sb"
                            for event in scheduled))
        self.assertTrue(all(event["resource_id"] in {6, 7}
                            and event["phy_resource_id"] in {0, 1}
                            for event in scheduled))
        self.assertEqual(
            result["resources"]["combined_physical_resource_use"],
            result["resources"]["cb_prb_use"] +
            result["resources"]["dedicated_sb_resource_use"])

    def test_default_and_explicit_uncapped_policy_are_identical(self):
        implicit = simulate()
        explicit = simulate(shared_uncapped_policy(6))
        self.assertEqual(implicit, explicit)

    def test_dedicated_replay_and_instrumentation_are_deterministic(self):
        policy = ScheduledResourcePolicy(
            "dedicated_sb2", ScheduledResourceMode.DEDICATED, 6, 2)
        first = simulate(policy)
        second = simulate(policy)
        self.assertEqual(first, second)
        packets = first["packet_rows"]
        required = {
            "initial_harq_final_failure_slot", "fast_arq_trigger_slot",
            "scheduled_request_slot", "grant_ready_slot",
            "scheduled_queue_entry_slot", "scheduled_first_tx_slot",
            "scheduled_completion_slot", "scheduled_queue_wait_slots",
            "scheduled_harq_attempts",
        }
        self.assertTrue(all(required <= row.keys() for row in packets))
        self.assertTrue(all(row["initial_harq_final_failure_slot"] is not None
                            for row in packets))

    def test_public_sionna_backend_runs_on_dedicated_domain(self):
        class OnePacketTraffic:
            def arrivals(self, slot):
                return ((Packet(0, 0, 0, 160, measurement_cohort=True,
                                measurement_cohort_locked=True),)
                        if slot == 0 else ())

        receiver = ActiveCompactedFrequencySelectivePrbReceiver({
            "num_ues": 1, "num_prbs": 6,
            "num_bs_receive_antennas": 2, "noise_power_w": 1e-5,
            "subcarrier_spacing_hz": 15_000.0,
            "delay_spread_s": 300e-9, "carrier_frequency_hz": 700e6})
        result = SlotSimulator(
            parameters=SimulationParameters(
                duration_slots=1, num_ues=1, seed=71,
                tx_power_dbm=(-100.0,), mcs_index=(10,),
                max_drain_slots=100),
            traffic=OnePacketTraffic(),
            access=EventAddressedGrantFreeAccess(
                seed=71, resource_pool_size=6, opportunity_period_slots=1,
                max_retries=3, retry_backoff_slots=0),
            phy_backend=DirectLlsHarqBackend(receiver, run_seed=71),
            harq=HarqController(max_attempts=4, feedback_delay_slots=1),
            recovery=FastArqPolicy(failure_indication_delay_slots=1),
            max_rlc_retransmissions=1,
            scheduled_rescue=ScheduledRescueConfig(
                mode=ScheduledRescueMode.FRESH_TB,
                request_delay_slots=1, scheduled_grant_delay_slots=2,
                trigger_after_attempts=5,
                maximum_physical_attempts_per_recovery_episode=4),
            scheduled_rlc_recovery=True,
            scheduled_rlc_uses_request_grant=True,
            scheduled_resource_policy=ScheduledResourcePolicy(
                "dedicated_sb2", ScheduledResourceMode.DEDICATED, 6, 2),
            uniform_available_cb_selection=True,
            event_addressed_scheduler_ties=True,
            extended_outputs=True,
            retain_per_re_trace=False,
            enhanced_attempt_trace=True,
        ).run()
        scheduled = [event for event in result["event_trace"]
                     if event["event"] == "phy_transmission" and
                     event["resource_domain"] == "dedicated_sb"]
        self.assertTrue(scheduled)
        self.assertTrue(all(event["resource_id"] == 6 and
                            event["phy_resource_id"] == 0
                            for event in scheduled))


if __name__ == "__main__":
    unittest.main()
