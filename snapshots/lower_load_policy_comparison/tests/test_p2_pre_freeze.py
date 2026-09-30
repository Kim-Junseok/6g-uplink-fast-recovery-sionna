"""Focused P2 pre-freeze gates for traffic, compaction, and HARQ state."""

from __future__ import annotations

import unittest

import torch

from ul_access.phy import (
    ActiveCompactedFrequencySelectivePrbReceiver,
    DirectLlsHarqBackend,
    FrequencySelectivePrbReceiver,
)
from ul_access.protocol import CountedPoissonTrafficGenerator
from ul_access.protocol import (EventAddressedGrantFreeAccess,
                                SimulationParameters, SlotSimulator)
from ul_access.protocol.access import Transmission
from ul_access.protocol.models import Packet, PacketState
from ul_access.recovery import (FastArqPolicy, HarqController,
                                ScheduledRescueConfig, ScheduledRescueMode)
from ul_access.resource import ResourceAllocation, ResourceAssignment


def receiver_config(num_ues: int) -> dict:
    return {"num_ues": num_ues, "num_prbs": 6,
            "num_bs_receive_antennas": 2, "noise_power_w": 1e-5,
            "subcarrier_spacing_hz": 15_000.0,
            "delay_spread_s": 300e-9, "carrier_frequency_hz": 700e6}


class P2PreFreezeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        torch.set_num_threads(1)
        torch.use_deterministic_algorithms(True)

    def test_exact_count_per_ue_poisson_trace_replays(self):
        kwargs = {"num_ues": 3000, "rate_per_ue_per_slot": 0.001,
                  "warmup_packets": 500, "measured_packets": 2500,
                  "packet_size_bits": 160, "seed": 9101}
        first = CountedPoissonTrafficGenerator(**kwargs)
        second = CountedPoissonTrafficGenerator(**kwargs)
        self.assertEqual(first.arrival_trace, second.arrival_trace)
        packets = tuple(packet for slot in range(first.duration_slots)
                        for packet in first.arrivals(slot))
        self.assertEqual(len(packets), 3000)
        self.assertEqual(sum(not p.measurement_cohort for p in packets), 500)
        self.assertEqual(sum(p.measurement_cohort for p in packets), 2500)
        self.assertTrue(all(p.measurement_cohort_locked for p in packets))
        self.assertAlmostEqual(
            first.arrival_stop_time_slots / 3000, 1 / 3, delta=0.04)

    def test_active_compaction_preserves_global_identity_and_phy_result(self):
        cfg = receiver_config(4)
        allocation = ResourceAllocation.from_assignments(
            [ResourceAssignment(1, 2), ResourceAssignment(3, 2)],
            num_ofdm_symbols=1, num_subcarriers=6, num_ues=4)
        kwargs = {"tx_power_dbm": (-10.0,) * 4,
                  "mcs_index": (10,) * 4, "realization_seed": 33}
        full = FrequencySelectivePrbReceiver(cfg).evaluate(allocation, **kwargs)
        compact_receiver = ActiveCompactedFrequencySelectivePrbReceiver(cfg)
        compact = compact_receiver.evaluate(allocation, **kwargs)
        self.assertEqual(compact_receiver.last_active_global_ue_ids, (1, 3))
        self.assertEqual(compact_receiver.last_compact_shape, (1, 1, 6, 2, 1))
        for ue in (1, 3):
            self.assertTrue(torch.allclose(
                torch.tensor(full.post_equalization_sinr_active_db[ue]),
                torch.tensor(compact.post_equalization_sinr_active_db[ue]),
                rtol=0, atol=1e-6))
            self.assertAlmostEqual(full.effective_sinr_db[ue],
                                   compact.effective_sinr_db[ue], places=4)

    def test_public_harq_rate_matching_uses_distinct_rv_positions(self):
        receiver = ActiveCompactedFrequencySelectivePrbReceiver(
            receiver_config(1))
        backend = DirectLlsHarqBackend(receiver, run_seed=17,
                                       audit_enabled=True)
        state = backend.new_episode_state(
            mac_tb_id="tb-8-0", harq_episode_id="harq-8-0", mcs_index=10)
        allocation = ResourceAllocation.from_assignments(
            [ResourceAssignment(0, 0)], num_ofdm_symbols=1,
            num_subcarriers=6, num_ues=1)
        for attempt, rv in enumerate((0, 2, 3)):
            outcome = backend.evaluate_harq_history(
                allocation, tx_power_dbm=(-20.0,), mcs_index=(10,),
                realization_seed=100 + attempt, states=(state,), rvs=(rv,))
            backend.append_episode_state(
                state, rv=rv,
                effective_sinr_db=outcome.effective_sinr_db[0])
        records = backend.audit_records
        self.assertEqual([row["decoder_input_shape"] for row in records],
                         [[1, 504], [1, 2, 504], [1, 3, 504]])
        self.assertEqual([row["rv_history"] for row in records],
                         [[0], [0, 2], [0, 2, 3]])
        self.assertEqual(records[-1]["rate_matching_start_positions"],
                         [0, 626, 1166])
        self.assertEqual([tuple(x.shape) for x in state.llr_history],
                         [(1, 504)] * 3)

    def test_fresh_episode_keeps_payload_and_has_no_old_llr(self):
        backend = DirectLlsHarqBackend(
            ActiveCompactedFrequencySelectivePrbReceiver(receiver_config(1)),
            run_seed=44)
        old = backend.new_episode_state(
            mac_tb_id="tb-9-0", harq_episode_id="harq-9-0", mcs_index=10)
        old.rv_history = (0, 2)
        old.llr_history = (torch.zeros(1, 504), torch.ones(1, 504))
        fresh = backend.new_episode_state(
            mac_tb_id="tb-9-1", harq_episode_id="harq-9-1", mcs_index=10)
        self.assertTrue(torch.equal(old.payload_bits, fresh.payload_bits))
        self.assertEqual(fresh.rv_history, ())
        self.assertEqual(fresh.llr_history, ())
        self.assertNotEqual(old.harq_episode_id, fresh.harq_episode_id)

    def test_available_prb_draw_uses_the_supplied_set_uniformly(self):
        access = EventAddressedGrantFreeAccess(
            seed=9101, resource_pool_size=6, opportunity_period_slots=1,
            max_retries=3, retry_backoff_slots=0)
        choices = []
        for ue in range(6000):
            packet = Packet(ue, ue, 0, 160)
            packet.initialize_transmission_context()
            tx = Transmission(ue, ue % 6, packet)
            choices.append(access.assign_available_resources(
                7, (tx,), (2, 3, 4, 5))[0].resource_id)
        self.assertEqual(set(choices), {2, 3, 4, 5})
        counts = [choices.count(resource) for resource in (2, 3, 4, 5)]
        self.assertLess(max(counts) - min(counts), 180)

    def test_b_uses_fresh_scheduled_rv0_after_request_and_grant(self):
        class OnePacketTraffic:
            def arrivals(self, slot):
                if slot != 0:
                    return ()
                return (Packet(0, 0, 0, 160, measurement_cohort=True,
                               measurement_cohort_locked=True),)

        receiver = ActiveCompactedFrequencySelectivePrbReceiver(receiver_config(1))
        backend = DirectLlsHarqBackend(receiver, run_seed=991, audit_enabled=True)
        access = EventAddressedGrantFreeAccess(
            seed=991, resource_pool_size=6, opportunity_period_slots=1,
            max_retries=3, retry_backoff_slots=0)
        result = SlotSimulator(
            parameters=SimulationParameters(
                duration_slots=1, num_ues=1, seed=991,
                tx_power_dbm=(-100.0,), mcs_index=(10,), max_drain_slots=100),
            traffic=OnePacketTraffic(), access=access, phy_backend=backend,
            harq=HarqController(max_attempts=4, feedback_delay_slots=1),
            recovery=FastArqPolicy(failure_indication_delay_slots=1),
            max_rlc_retransmissions=1,
            scheduled_rescue=ScheduledRescueConfig(
                mode=ScheduledRescueMode.FRESH_TB, request_delay_slots=1,
                scheduled_grant_delay_slots=2, trigger_after_attempts=5,
                maximum_physical_attempts_per_recovery_episode=4),
            scheduled_rlc_recovery=True, scheduled_rlc_uses_request_grant=True,
            uniform_available_cb_selection=True, extended_outputs=True).run()
        transmissions = [event for event in result["event_trace"]
                         if event["event"] == "phy_transmission"]
        episodes = {}
        for event in transmissions:
            episodes.setdefault(event["harq_episode_id"], []).append(event)
        ordered = list(episodes.values())
        self.assertEqual([row["redundancy_version"] for row in ordered[0]],
                         [0, 2, 3, 1])
        self.assertTrue(all(row["transmission_access"] == "grant_free"
                            for row in ordered[0]))
        self.assertEqual(ordered[1][0]["redundancy_version"], 0)
        self.assertEqual(ordered[1][0]["transmission_access"], "scheduled_rescue")
        self.assertNotEqual(ordered[0][0]["harq_episode_id"],
                            ordered[1][0]["harq_episode_id"])
        second_audit = next(row for row in backend.audit_records
                            if row["harq_episode_id"] == ordered[1][0]["harq_episode_id"])
        self.assertEqual(second_audit["decoder_input_shape"], [1, 504])
        events = result["event_trace"]
        failure = next(event for event in events
                       if event["event"] == "final_harq_failure")
        ready = next(event for event in events
                     if event["event"] == "scheduled_rlc_recovery_ready")
        request = next(event for event in events
                       if event["event"] == "scheduled_rescue_request")
        grant = next(event for event in events
                     if event["event"] == "scheduled_rescue_grant")
        self.assertEqual(ready["slot"], failure["slot"] + 1)
        self.assertEqual(request["slot"], ready["slot"] + 1)
        self.assertEqual(grant["slot"], request["slot"] + 2)
        self.assertTrue(all(row["pool_invariant"]
                            for row in result["slot_resource_rows"]))


if __name__ == "__main__":
    unittest.main()
