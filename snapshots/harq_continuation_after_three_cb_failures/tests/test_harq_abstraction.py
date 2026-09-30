"""Fast V0.4b compact-HARQ model and integration invariants."""

from __future__ import annotations

import unittest
from pathlib import Path

from ul_access.config import load_yaml
from ul_access.phy import (
    CalibratedHarqPhyBackend,
    CalibratedLogisticHarqModel,
    HarqEpisodeState,
    HarqHistoryLogisticModel,
    PhyBackend,
    PhyOutcome,
)
from ul_access.protocol import GrantFreeAccess, Packet, SimulationParameters, SlotSimulator
from ul_access.recovery import (
    FastArqPolicy,
    HarqController,
    ScheduledRescueConfig,
    ScheduledRescueMode,
)
from ul_access.resource import ResourceAllocation, ResourceAssignment


PARAMETERS = (1.6606016208750463, 1.3646177173899867, 1.2092882624365673)


def model() -> CalibratedLogisticHarqModel:
    return CalibratedLogisticHarqModel(
        parameters=PARAMETERS, gamma_1_range_db=(-1.0, 3.0),
        gamma_2_range_db=(-2.0, 4.0), mcs_index=10,
        information_bits=128, coded_bits=384, rv_sequence=(0, 2))


class FailedPhy(PhyBackend):
    """Ordinary memoryless backend with controlled in-domain SINRs."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def num_ues(self):
        return 1

    @property
    def num_subcarriers(self):
        return 1

    def evaluate(self, allocation: ResourceAllocation, *, tx_power_dbm,
                 mcs_index, realization_seed):
        del allocation, tx_power_dbm, mcs_index, realization_seed
        sinr = 3.0 if self.calls == 0 else 4.0
        self.calls += 1
        return PhyOutcome((sinr,), (1.0,), (0,), (0,), ((sinr,),),
                          (0.001,), (1.0,))


class OnePacket:
    def arrivals(self, slot):
        return (Packet(700, 0, 0, 24),) if slot == 0 else ()


def simulate(mode: ScheduledRescueMode) -> dict:
    return SlotSimulator(
        parameters=SimulationParameters(8, 1, 42, (0.0,), (10,)),
        traffic=OnePacket(),
        access=GrantFreeAccess(
            resource_pool_size=1, opportunity_period_slots=1,
            max_retries=0, retry_backoff_slots=0),
        phy_backend=CalibratedHarqPhyBackend(FailedPhy(), model()),
        harq=HarqController(max_attempts=2, feedback_delay_slots=0),
        recovery=FastArqPolicy(failure_indication_delay_slots=0),
        scheduled_rescue=ScheduledRescueConfig(mode, 0, 0),
    ).run()


class HarqAbstractionTest(unittest.TestCase):
    def test_v04c_model_loads_and_clips_without_extrapolation(self):
        loaded = HarqHistoryLogisticModel.from_config(load_yaml(
            Path("configs/harq_history_l2s_v0_4c.yaml")))
        state = HarqEpisodeState(
            "tb", "episode", 10, 24, 72, (), (), 1)
        self.assertEqual(loaded.rv_sequence, (0, 2, 3, 1))
        self.assertEqual(
            loaded.probability(state, rv=0, effective_sinr_db=-100.0),
            loaded.probability(state, rv=0, effective_sinr_db=-4.0))
        self.assertEqual(
            loaded.probability(state, rv=0, effective_sinr_db=100.0),
            loaded.probability(state, rv=0, effective_sinr_db=6.0))
        with self.assertRaises(ValueError):
            loaded.probability(
                HarqEpisodeState("tb", "episode", 9, 24, 72, (), (), 1),
                rv=0, effective_sinr_db=0.0)
        with self.assertRaises(ValueError):
            loaded.probability(
                HarqEpisodeState("tb", "episode", 10, 24, 72, (), (), 2),
                rv=0, effective_sinr_db=0.0)

    def test_model_matches_held_out_reference_and_rejects_wrong_scope(self):
        state = HarqEpisodeState("tb", "episode", 10, 128, 384, (0,), (0.0,))
        self.assertAlmostEqual(model().probability(
            state, rescue_rv=2, rescue_effective_sinr_db=-1.0),
            0.6109514512726882)
        with self.assertRaises(ValueError):
            model().probability(state, rescue_rv=1, rescue_effective_sinr_db=-1.0)
        with self.assertRaises(ValueError):
            model().probability(state, rescue_rv=2, rescue_effective_sinr_db=5.0)

    def test_state_extension_preserves_episode_and_disallows_duplicate_rv(self):
        state = HarqEpisodeState("tb", "episode", 10, 128, 384, (0,), (0.0,))
        extended = state.append(rv=2, effective_sinr_db=1.0)
        self.assertEqual((extended.mac_tb_id, extended.harq_episode_id),
                         (state.mac_tb_id, state.harq_episode_id))
        self.assertEqual(extended.rv_history, (0, 2))
        self.assertEqual(extended.effective_sinr_history_db, (0.0, 1.0))
        with self.assertRaises(ValueError):
            extended.append(rv=2, effective_sinr_db=2.0)

    def test_h_uses_and_clears_state_while_f_remains_fresh(self):
        h = simulate(ScheduledRescueMode.HARQ_PRESERVING)
        f = simulate(ScheduledRescueMode.FRESH_TB)
        h_events = [event["event"] for event in h["event_trace"]]
        f_events = [event["event"] for event in f["event_trace"]]
        self.assertEqual(h["packets"]["completed"], 1)
        self.assertEqual(f["packets"]["completed"], 0)
        self.assertIn("harq_combining_state_stored", h_events)
        self.assertIn("harq_combining_state_extended", h_events)
        self.assertIn("harq_combining_state_cleared", h_events)
        self.assertNotIn("harq_combining_state_stored", f_events)
        self.assertEqual(h["recovery_configuration"]["scheduled_rescue"]
                         ["phy_combining"], "calibrated_conditional_harq_ir")
        self.assertEqual(f["recovery_configuration"]["scheduled_rescue"]
                         ["phy_combining"], "none")

    def test_wrapper_sampling_is_replay_deterministic(self):
        allocation = ResourceAllocation.from_assignments(
            [ResourceAssignment(0, 0)], num_ofdm_symbols=1,
            num_subcarriers=1, num_ues=1)
        state = HarqEpisodeState("tb", "episode", 10, 128, 384, (0,), (0.0,))
        outcomes = []
        for _ in range(2):
            wrapped = CalibratedHarqPhyBackend(FailedPhy(), model())
            outcomes.append(wrapped.evaluate_harq(
                allocation, tx_power_dbm=(0.0,), mcs_index=(10,),
                realization_seed=7, states=(state,), rescue_rv=2))
        self.assertEqual(outcomes[0], outcomes[1])

    def test_v04c_exact_rv_and_access_sequences_under_four_attempt_cap(self):
        history_model = HarqHistoryLogisticModel(
            parameters_by_attempt={
                1: (-30.0, 0.0), 2: (-30.0, 0.0, 0.0, 0.0),
                3: (-30.0, 0.0, 0.0, 0.0),
                4: (-30.0, 0.0, 0.0, 0.0, 0.0)},
            sinr_range_db=(-20.0, 12.0), mcs_index=10,
            information_bits=24, coded_bits=72, rv_sequence=(0, 2, 3, 1))

        def run(mode):
            rescue = None if mode is None else ScheduledRescueConfig(
                mode, 0, 0, trigger_after_attempts=2,
                maximum_physical_attempts_per_recovery_episode=4)
            return SlotSimulator(
                parameters=SimulationParameters(12, 1, 42, (0.0,), (10,)),
                traffic=OnePacket(),
                access=GrantFreeAccess(
                    resource_pool_size=1, opportunity_period_slots=1,
                    max_retries=3, retry_backoff_slots=0),
                phy_backend=CalibratedHarqPhyBackend(FailedPhy(), history_model),
                harq=HarqController(max_attempts=4, feedback_delay_slots=0),
                recovery=FastArqPolicy(failure_indication_delay_slots=0),
                scheduled_rescue=rescue,
            ).run()

        expected = {
            None: (["grant_free"] * 4, [0, 2, 3, 1]),
            ScheduledRescueMode.HARQ_PRESERVING:
                (["grant_free", "grant_free", "scheduled_rescue", "scheduled_rescue"],
                 [0, 2, 3, 1]),
            ScheduledRescueMode.FRESH_TB:
                (["grant_free", "grant_free", "scheduled_rescue", "scheduled_rescue"],
                 [0, 2, 0, 2]),
        }
        for mode, (accesses, rvs) in expected.items():
            with self.subTest(mode=mode):
                result = run(mode)
                transmissions = [event for event in result["event_trace"]
                                 if event["event"] == "phy_transmission"]
                self.assertEqual(len(transmissions), 4)
                self.assertEqual([event["transmission_access"] for event in transmissions],
                                 accesses)
                self.assertEqual([event["redundancy_version"] for event in transmissions],
                                 rvs)
                self.assertTrue(all(event["mcs_index"] == 10 for event in transmissions))
                self.assertTrue(all(event["resource_id"] == 0 for event in transmissions))
                expected_lengths = ([1, 2, 1, 2]
                    if mode == ScheduledRescueMode.FRESH_TB else [1, 2, 3, 4])
                self.assertEqual([len(event["effective_sinr_history_db"])
                                  for event in transmissions], expected_lengths)
                self.assertTrue(all(
                    event["effective_sinr_history_db"][-1] ==
                    event["effective_sinr_db"] for event in transmissions))
                episode_ids = [event["harq_episode_id"] for event in transmissions]
                tb_ids = [event["tb_id"] for event in transmissions]
                histories = [event["rv_history"] for event in result["event_trace"]
                             if event["event"] in {
                                 "harq_combining_state_stored",
                                 "harq_combining_state_extended"}]
                if mode == ScheduledRescueMode.FRESH_TB:
                    self.assertEqual(len(set(episode_ids[:2])), 1)
                    self.assertEqual(len(set(episode_ids[2:])), 1)
                    self.assertNotEqual(episode_ids[1], episode_ids[2])
                    self.assertNotEqual(tb_ids[1], tb_ids[2])
                    self.assertEqual(histories, [[0], [0, 2], [0], [0, 2]])
                else:
                    self.assertEqual(len(set(episode_ids)), 1)
                    self.assertEqual(len(set(tb_ids)), 1)
                    self.assertEqual(histories[-1], [0, 2, 3, 1])
                cleared = [event for event in result["event_trace"]
                           if event["event"] == "harq_combining_state_cleared"]
                self.assertTrue(cleared)
                self.assertEqual(cleared[-1]["reason"], "final_failure")


if __name__ == "__main__":
    unittest.main()
