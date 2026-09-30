"""P0 checks for the V0.4g frequency-selective direct-LLS path."""

from __future__ import annotations

import json
import unittest

import torch

from ul_access.phy import (DirectLlsHarqBackend, FrequencySelectivePrbReceiver,
                           audit_controlled_pusch_geometry)
from ul_access.phy.direct_lls import classify_crc_oracle_outcome
from ul_access.protocol import (EventAddressedGrantFreeAccess, Packet,
    SimulationParameters, SlotSimulator)
from ul_access.recovery import (FastArqPolicy, HarqController,
    ScheduledRescueConfig, ScheduledRescueMode)
from ul_access.resource import ResourceAllocation, ResourceAssignment


def receiver(num_ues: int = 3) -> FrequencySelectivePrbReceiver:
    return FrequencySelectivePrbReceiver({
        "num_ues": num_ues, "num_prbs": 6,
        "num_bs_receive_antennas": 2, "noise_power_w": 1e-5,
        "subcarrier_spacing_hz": 15_000.0,
        "delay_spread_s": 300e-9, "carrier_frequency_hz": 3.5e9})


def allocation(assignments, num_ues=3):
    return ResourceAllocation.from_assignments(
        assignments, num_ofdm_symbols=1, num_subcarriers=6,
        num_ues=num_ues)


class OnePacket:
    def arrivals(self, slot):
        return (Packet(0, 0, 0, 160, next_eligible_slot=0),) if slot == 0 else ()


def rescue_run(mode):
    base = receiver(1)
    backend = DirectLlsHarqBackend(base, run_seed=1)
    return SlotSimulator(
        parameters=SimulationParameters(
            12, 1, 1, (-15.0,), (10,), max_drain_slots=12),
        traffic=OnePacket(),
        access=EventAddressedGrantFreeAccess(
            seed=1, resource_pool_size=6, opportunity_period_slots=1,
            max_retries=3, retry_backoff_slots=0),
        phy_backend=backend,
        harq=HarqController(max_attempts=4, feedback_delay_slots=1,
                            pusch_spacing_slots=2),
        recovery=FastArqPolicy(failure_indication_delay_slots=1),
        scheduled_rescue=ScheduledRescueConfig(
            mode, 0, 0, trigger_after_attempts=2,
            maximum_physical_attempts_per_recovery_episode=4),
        direct_scheduled_k2_slots=1,
        extended_outputs=True,
        event_addressed_scheduler_ties=True).run()


class DirectLlsV04gTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        torch.set_num_threads(1)
        torch.use_deterministic_algorithms(True)

    def test_public_nr_geometry_gate(self):
        geometry = audit_controlled_pusch_geometry()
        self.assertEqual(geometry["tbs_bits"], 160)
        self.assertEqual(geometry["tb_crc_bits"], 16)
        self.assertEqual(geometry["ldpc_input_bits"], 176)
        self.assertEqual(geometry["data_re"], 126)
        self.assertEqual(geometry["coded_bits"], 504)
        self.assertEqual(geometry["qam_symbols"], 126)
        self.assertEqual(geometry["ldpc_systematic_bits"], 300)
        self.assertEqual(geometry["ldpc_mother_code_bits"], 1560)
        self.assertEqual(geometry["ldpc_filler_bits"], 124)
        self.assertEqual(geometry["ldpc_compressed_buffer_bits"], 1376)
        self.assertEqual(geometry["rv_sequence"], [0, 2, 3, 1])

    def test_tdl_frequency_selectivity_prb_extraction_and_replay(self):
        base = receiver()
        a = allocation([ResourceAssignment(0, 2)])
        first = base.evaluate(a, tx_power_dbm=(0.0,) * 3,
                              mcs_index=(10,) * 3, realization_seed=81)
        first_channel = base.last_channel.clone()
        second = base.evaluate(a, tx_power_dbm=(0.0,) * 3,
                               mcs_index=(10,) * 3, realization_seed=81)
        self.assertEqual(first, second)
        self.assertTrue(torch.equal(first_channel, base.last_channel))
        self.assertEqual(len(first.post_equalization_sinr_active_db[0]), 126)
        frequency_slice = torch.abs(first_channel[0, 0, 0, 0, 0, 0, 24:36])
        self.assertGreater(float(torch.var(frequency_slice)), 0.0)

    def test_overlap_changes_sinr_but_different_prbs_do_not(self):
        base = receiver()
        kwargs = {"tx_power_dbm": (0.0,) * 3, "mcs_index": (10,) * 3,
                  "realization_seed": 123}
        isolated = base.evaluate(allocation([ResourceAssignment(0, 0)]), **kwargs)
        orthogonal = base.evaluate(allocation([
            ResourceAssignment(0, 0), ResourceAssignment(1, 1)]), **kwargs)
        two = base.evaluate(allocation([
            ResourceAssignment(0, 0), ResourceAssignment(1, 0)]), **kwargs)
        three = base.evaluate(allocation([
            ResourceAssignment(0, 0), ResourceAssignment(1, 0),
            ResourceAssignment(2, 0)]), **kwargs)
        self.assertEqual(isolated.effective_sinr_db[0],
                         orthogonal.effective_sinr_db[0])
        self.assertLess(two.effective_sinr_db[0], isolated.effective_sinr_db[0])
        self.assertLess(three.effective_sinr_db[0], two.effective_sinr_db[0])
        self.assertEqual(len(two.feedback), 3)

    def test_direct_lls_retains_real_llrs_for_prior_nack_entrants(self):
        base = receiver(1)
        backend = DirectLlsHarqBackend(base, run_seed=1)
        state = backend.new_episode_state(
            mac_tb_id="tb", harq_episode_id="episode", mcs_index=10)
        a = allocation([ResourceAssignment(0, 0)], num_ues=1)
        observed = []
        for attempt, rv in enumerate((0, 2, 3, 1)):
            outcome = backend.evaluate_harq_history(
                a, tx_power_dbm=(-15.0,), mcs_index=(10,),
                realization_seed=attempt + 1, states=(state,), rvs=(rv,))
            backend.append_episode_state(
                state, rv=rv, effective_sinr_db=outcome.effective_sinr_db[0])
            observed.append(rv)
            self.assertEqual(len(state.llr_history), len(observed))
            self.assertTrue(all(value.shape[-1] == 504
                                for value in state.llr_history))
            if outcome.feedback[0] == 1:
                break
        self.assertEqual(list(state.rv_history), observed)
        self.assertGreaterEqual(len(observed), 2)

    def test_crc_feedback_and_oracle_correctness_are_separate(self):
        original = torch.zeros((1, 160), dtype=torch.float32)
        incorrect = original.clone()
        incorrect[0, 0] = 1
        expected = {
            (True, True): (1, True, False, 0),
            (True, False): (1, False, True, 1),
            (False, True): (0, True, False, 0),
            (False, False): (0, False, False, 1),
        }
        for crc_pass, payload_correct in expected:
            decoded = original if payload_correct else incorrect
            self.assertEqual(
                classify_crc_oracle_outcome(crc_pass, decoded, original),
                expected[(crc_pass, payload_correct)])

    def test_forensic_crc_undetected_fixture_returns_ack(self):
        decoded_tb_bits = (
            "0011101000101101111000111110110110111100011110100010100000011001"
            "1100100000001000111100000100010010001110101000101100110100001111"
            "110001100001110100001010000000111110101011111000")

        class FixedDecoder(torch.nn.Module):
            def forward(self, llr, *, rv):
                del llr, rv
                return torch.tensor(
                    [[int(value) for value in decoded_tb_bits]],
                    dtype=torch.float32)

        backend = DirectLlsHarqBackend(receiver(1), run_seed=9101)
        backend.decoder = FixedDecoder()
        state = backend.new_episode_state(
            mac_tb_id="tb-655-0", harq_episode_id="harq-655-0",
            mcs_index=10)
        outcome = backend.evaluate_harq_history(
            allocation([ResourceAssignment(0, 0)], num_ues=1),
            tx_power_dbm=(-8.0,), mcs_index=(10,), realization_seed=1,
            states=(state,), rvs=(0,))
        self.assertEqual(outcome.feedback, (1,))
        self.assertEqual(outcome.crc_pass, (True,))
        self.assertEqual(outcome.payload_correct, (False,))
        self.assertEqual(outcome.undetected_error, (True,))
        self.assertEqual(outcome.payload_hamming_distance, (49,))

    def test_h_preserves_and_f_resets_llr_episode(self):
        h = rescue_run(ScheduledRescueMode.HARQ_PRESERVING)
        f = rescue_run(ScheduledRescueMode.FRESH_TB)
        h_tx = [e for e in h["event_trace"] if e["event"] == "phy_transmission"]
        f_tx = [e for e in f["event_trace"] if e["event"] == "phy_transmission"]
        self.assertEqual([e["slot"] for e in h_tx[:3]], [0, 2, 4])
        self.assertEqual([e["feedback_available_slot"] for e in h_tx[:3]],
                         [1, 3, 5])
        self.assertEqual([e["redundancy_version"] for e in h_tx[:3]], [0, 2, 3])
        self.assertEqual([e["redundancy_version"] for e in f_tx[:3]], [0, 2, 0])
        self.assertEqual(len({e["harq_episode_id"] for e in h_tx[:3]}), 1)
        self.assertNotEqual(f_tx[1]["harq_episode_id"],
                            f_tx[2]["harq_episode_id"])
        self.assertEqual(f_tx[2]["rv_history"], [0])
        stored = [e for e in f["event_trace"]
                  if e["event"] in {"harq_combining_state_stored",
                                    "harq_combining_state_extended"}]
        self.assertEqual(stored[0]["harq_information_state"]["payload_sha256"],
                         stored[2]["harq_information_state"]["payload_sha256"])

    def test_full_event_trace_replays_byte_identically(self):
        first = rescue_run(ScheduledRescueMode.FRESH_TB)
        second = rescue_run(ScheduledRescueMode.FRESH_TB)
        self.assertEqual(json.dumps(first["event_trace"], sort_keys=True),
                         json.dumps(second["event_trace"], sort_keys=True))
        self.assertEqual(first["packet_rows"], second["packet_rows"])


if __name__ == "__main__":
    unittest.main()
