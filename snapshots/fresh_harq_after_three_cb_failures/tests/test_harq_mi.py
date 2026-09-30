"""Deterministic V0.4e MI-HARQ mapping and integration checks."""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from ul_access.phy import (
    BicmInformationTable, HarqInformationState, HarqMiLogisticModel,
    MiHarqPhyBackend, PhyBackend, PhyOutcome, RateMatchingProfile)
from ul_access.protocol import (
    GrantFreeAccess, Packet, SimulationParameters, SlotSimulator)
from ul_access.recovery import (
    FastArqPolicy, HarqController, ScheduledRescueConfig,
    ScheduledRescueMode)
from ul_access.resource import ResourceAllocation


def table() -> BicmInformationTable:
    return BicmInformationTable((-10.0, 0.0, 10.0), (
        (0.0, 0.0, 0.0, 0.0), (0.5, 0.5, 0.5, 0.5),
        (1.0, 1.0, 1.0, 1.0)))


def profile() -> RateMatchingProfile:
    return RateMatchingProfile(184, 72, 4, {0: 0, 2: 84, 3: 156, 1: 36})


class CountingPhy(PhyBackend):
    def __init__(self):
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
        self.calls += 1
        return PhyOutcome((0.0,), (0.5,), (0,), (0,), ((0.0,),),
                          (0.001,), (0.5,))


class OnePacket:
    def arrivals(self, slot):
        return (Packet(900, 0, 0, 24),) if slot == 0 else ()


def backend(intercept: float) -> tuple[MiHarqPhyBackend, CountingPhy]:
    base = CountingPhy()
    parameters = {attempt: (intercept, 0, 0, 0, 0, 0)
                  for attempt in range(1, 5)}
    wrapped = MiHarqPhyBackend(base,
        model=HarqMiLogisticModel(parameters, (0, 2, 3, 1)), table=table(),
        profile=profile(), mcs_index=10, information_bits=24, coded_bits=72)
    return wrapped, base


def simulate(intercept: float, mode=None) -> dict:
    wrapped, _ = backend(intercept)
    rescue = None if mode is None else ScheduledRescueConfig(
        mode, 0, 0, trigger_after_attempts=2,
        maximum_physical_attempts_per_recovery_episode=4)
    return SlotSimulator(
        parameters=SimulationParameters(12, 1, 42, (0.0,), (10,)),
        traffic=OnePacket(), access=GrantFreeAccess(
            resource_pool_size=1, opportunity_period_slots=1,
            max_retries=3, retry_backoff_slots=0),
        phy_backend=wrapped, harq=HarqController(
            max_attempts=4, feedback_delay_slots=0),
        recovery=FastArqPolicy(failure_indication_delay_slots=0),
        scheduled_rescue=rescue).run()


class HarqMiTest(unittest.TestCase):
    def test_transform_interpolation_probability_and_bounds(self):
        self.assertEqual(table().at(-20.0), (0.0,)*4)
        self.assertEqual(table().at(5.0), (0.75,)*4)
        state = HarqInformationState().append(
            rv=0, sinr_db=0.0, table=table(), profile=profile())
        model = HarqMiLogisticModel(
            {attempt: (0, 1, 0, 0, 0, 0) for attempt in range(1, 5)},
            (0, 2, 3, 1))
        self.assertGreaterEqual(model.probability(state, 24), 0.0)
        self.assertLessEqual(model.probability(state, 24), 1.0)

    def test_rv_dependent_new_repeat_and_effective_rate(self):
        state = HarqInformationState()
        expected = ((72, 0, 72), (72, 0, 144), (28, 44, 172),
                    (12, 60, 184))
        previous_new = previous_repeat = 0.0
        for rv, (new, repeated, unique) in zip((0, 2, 3, 1), expected):
            state = state.append(rv=rv, sinr_db=10.0, table=table(),
                                 profile=profile())
            self.assertEqual(state.new_bit_information-previous_new, new)
            self.assertEqual(state.repeated_bit_information-previous_repeat,
                             repeated)
            self.assertEqual(state.unique_coded_bits, unique)
            self.assertAlmostEqual(state.effective_code_rate(24), 24/unique)
            previous_new, previous_repeat = (state.new_bit_information,
                                             state.repeated_bit_information)
        self.assertEqual(state.rv_history, (0, 2, 3, 1))

    def test_backend_calls_base_once_and_stores_no_raw_history(self):
        wrapped, base = backend(-30.0)
        state = wrapped.new_episode_state(
            mac_tb_id="tb", harq_episode_id="episode", mcs_index=10)
        from ul_access.resource import ResourceAssignment
        allocation = ResourceAllocation.from_assignments(
            [ResourceAssignment(0, 0)], num_ofdm_symbols=1,
            num_subcarriers=1, num_ues=1)
        wrapped.evaluate_harq_history(allocation, tx_power_dbm=(0.0,),
            mcs_index=(10,), realization_seed=1, states=(state,), rvs=(0,))
        self.assertEqual(base.calls, 1)
        updated = wrapped.append_episode_state(
            state, rv=0, effective_sinr_db=0.0)
        self.assertFalse(hasattr(updated, "effective_sinr_history_db"))
        self.assertNotIn("effective_sinr_history_db",
                         wrapped.state_trace(updated))

    def test_ack_final_failure_and_fresh_tb_clear_or_reset_state(self):
        ack = simulate(30.0)
        failure = simulate(-30.0)
        fresh = simulate(-30.0, ScheduledRescueMode.FRESH_TB)
        self.assertTrue(any(e["event"] == "harq_combining_state_cleared" and
                            e["reason"] == "ack" for e in ack["event_trace"]))
        self.assertTrue(any(e["event"] == "harq_combining_state_cleared" and
                            e["reason"] == "final_failure"
                            for e in failure["event_trace"]))
        tx = [e for e in fresh["event_trace"] if e["event"] == "phy_transmission"]
        self.assertEqual([e["redundancy_version"] for e in tx], [0, 2, 0, 2])
        self.assertEqual([len(e["rv_history"]) for e in tx], [1, 2, 1, 2])

    def test_reference_conditioning_zero_entrant_and_validation_regression(self):
        reference = json.loads(Path(
            "results/harq_mi_reference_validation_v0_4e.json").read_text())
        for point in reference["points"]:
            self.assertEqual(point["conditioned_population_entering_target"],
                             point["target"]["trials"])
            self.assertEqual(point["target_successes"]+point["target_failures"],
                             point["target"]["trials"])
        validation = json.loads(Path(
            "results/harq_mi_validation_v0_4e.json").read_text())
        rows = validation["candidates"]["MI_C"]["validation_points"]
        self.assertTrue(any(row["status"] == "UNREACHABLE" and
                            row["conditioned_trials"] == 0 for row in rows))
        for attempt in range(1, 5):
            self.assertLessEqual(validation["candidates"]["MI_C"]["metrics"]
                                 ["old_v0_4c"][str(attempt)]
                                 ["maximum_absolute_probability_error"], 0.05)

    def test_v04e_payload_hashes_and_v04d_preservation(self):
        for path in ("results/harq_mi_reference_calibration_v0_4e.json",
                     "results/harq_mi_reference_validation_v0_4e.json",
                     "results/harq_mi_validation_v0_4e.json",
                     "results/harq_mi_support_v0_4e.json"):
            artifact = json.loads(Path(path).read_text())
            canonical = {key: value for key, value in artifact.items()
                         if key not in {"runtime_seconds",
                                        "deterministic_payload_sha256"}}
            actual = hashlib.sha256(json.dumps(canonical, sort_keys=True,
                separators=(",", ":")).encode()).hexdigest()
            self.assertEqual(actual, artifact["deterministic_payload_sha256"])
        expected = {
            "configs/harq_sys_support_v0_4d.yaml":
                "95c51737c45a28bf78c77f2ee695ab99c3d81a9d636e6a771947b51d457b33e3",
            "configs/harq_targeted_l2s_v0_4d.yaml":
                "27e7a09658d7d64b87e13490a025e140495e6e1e758a5f3903dc14fc645384cc",
            "docs/HARQ_SYS_SUPPORT_V0_4D.md":
                "69c38c058cc51045a60f7b000bc5b3326e38cea85efe751dc631e5103ec12fb8",
            "results/harq_sys_support_v0_4d.json":
                "876b6aa7949b6db0d5315161cde832d9cdf54e5e799464d2e89469da2902f3da",
            "results/harq_support_validation_v0_4d.json":
                "54ff8ed27bcd7d1caba5a08b356dd0f56329fe64fac47c0064c359570e882d92"}
        for path, expected_hash in expected.items():
            self.assertEqual(hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                             expected_hash)

    def test_sls_populations_are_regenerated_and_exclusively_classified(self):
        artifact = json.loads(Path(
            "results/harq_mi_support_v0_4e.json").read_text())
        self.assertFalse(artifact["quick_smoke_run"])
        self.assertEqual(len(artifact["points"]), 8)
        self.assertEqual({point["seed"] for point in artifact["points"]},
                         {9101, 9102, 9103, 9104})
        for row in artifact["support_summaries"]:
            self.assertEqual(sum(row["counts"].values()), row["population"])
            self.assertAlmostEqual(sum(row["fractions"].values()), 1.0)


if __name__ == "__main__":
    unittest.main()
