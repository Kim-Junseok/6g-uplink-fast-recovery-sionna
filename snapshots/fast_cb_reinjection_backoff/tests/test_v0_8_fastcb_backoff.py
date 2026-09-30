"""Test D opportunity counting, RNG isolation, and protocol timing."""

from __future__ import annotations

import unittest

from ul_access.protocol import Packet
from ul_access.recovery import FastArqPolicy, FastCbReinjectionBackoffPolicy
from test_v0_5_timing import simulate, events


class FastCbBackoffTest(unittest.TestCase):
    def test_b_zero_through_three_select_o_zero_through_o_three(self):
        packet = Packet(7, 0, 0, 160)
        for period, failure, first in ((1, 7, 8), (3, 7, 9), (3, 8, 9)):
            for backoff in range(4):
                with self.subTest(period=period, failure=failure, B=backoff):
                    policy = FastCbReinjectionBackoffPolicy(
                        failure_indication_delay_slots=1, seed=9101,
                        opportunity_period_slots=period,
                        forced_backoff=backoff)
                    decision = policy.on_final_harq_failure(packet, failure)
                    recorded = policy.decisions[0]
                    self.assertEqual(recorded.original_first_opportunity, first)
                    self.assertEqual(recorded.target_opportunity,
                                     first + backoff * period)
                    self.assertEqual(decision.eligible_slot,
                                     failure + 1 if backoff == 0 else
                                     first + backoff * period)

    def test_backoff_key_includes_seed_payload_and_recovery_index(self):
        first = FastCbReinjectionBackoffPolicy(
            failure_indication_delay_slots=1, seed=9101)
        second = FastCbReinjectionBackoffPolicy(
            failure_indication_delay_slots=1, seed=9101)
        keys = [(seed, payload, episode)
                for seed in (9101, 9102)
                for payload in range(20)
                for episode in (1, 2, 3)]
        draws = [FastCbReinjectionBackoffPolicy(
            failure_indication_delay_slots=1, seed=seed).draw(
                payload, episode) for seed, payload, episode in keys]
        self.assertEqual(first.draw(500, 1), second.draw(500, 1))
        self.assertEqual(set(draws), {0, 1, 2, 3})
        self.assertGreater(len(set(draws)), 1)

    def test_forced_zero_replays_accepted_protocol_trace(self):
        original = simulate(recovery=FastArqPolicy(
            failure_indication_delay_slots=1), maximum_rlc=1)
        forced = simulate(recovery=FastCbReinjectionBackoffPolicy(
            failure_indication_delay_slots=1, seed=9101,
            forced_backoff=0), maximum_rlc=1)
        self.assertEqual(original, forced)
        self.assertEqual([row["slot"] for row in events(forced,
                         "phy_transmission")], [0, 2, 4, 6, 8, 10, 12, 14])

    def test_reinjection_delay_only_after_final_failure(self):
        for backoff in range(4):
            with self.subTest(B=backoff):
                result = simulate(recovery=FastCbReinjectionBackoffPolicy(
                    failure_indication_delay_slots=1, seed=9101,
                    forced_backoff=backoff), maximum_rlc=1)
                transmissions = events(result, "phy_transmission")
                self.assertEqual([row["slot"] for row in transmissions[:4]],
                                 [0, 2, 4, 6])
                self.assertEqual([row["slot"] for row in transmissions[4:]],
                                 [8 + backoff, 10 + backoff,
                                  12 + backoff, 14 + backoff])
                self.assertEqual([row["harq_attempt"]
                                  for row in transmissions],
                                 [1, 2, 3, 4, 1, 2, 3, 4])

    def test_policy_applies_to_warmup_and_measured_traffic(self):
        class MixedTraffic:
            def arrivals(self, slot):
                if slot:
                    return ()
                return (Packet(0, 0, 0, 160, measurement_cohort=False,
                               measurement_cohort_locked=True),
                        Packet(1, 1, 0, 160, measurement_cohort=True,
                               measurement_cohort_locked=True))

        policy = FastCbReinjectionBackoffPolicy(
            failure_indication_delay_slots=1, seed=9101,
            forced_backoff=2)
        result = simulate(num_ues=2, recovery=policy, maximum_rlc=1,
                          traffic=MixedTraffic())
        self.assertEqual({row.payload_id for row in policy.decisions}, {0, 1})
        self.assertEqual(len(result["packet_rows"]), 1)
        self.assertTrue(all(row.additional_opportunities_skipped == 2
                            for row in policy.decisions))

    def test_event_addressed_policy_replays_without_global_rng_draws(self):
        import random
        before = random.getstate()
        policies = [FastCbReinjectionBackoffPolicy(
            failure_indication_delay_slots=1, seed=9101) for _ in range(2)]
        results = [simulate(recovery=policy, maximum_rlc=1)
                   for policy in policies]
        self.assertEqual(results[0], results[1])
        self.assertEqual(policies[0].decisions, policies[1].decisions)
        self.assertEqual(random.getstate(), before)



if __name__ == "__main__":
    unittest.main()
