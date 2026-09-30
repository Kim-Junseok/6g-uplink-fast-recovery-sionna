"""Traffic-model replay, calibration, and burst-shape tests."""

from __future__ import annotations

import statistics
import unittest
from collections import Counter

from ul_access.protocol import (BernoulliTrafficGenerator,
    EventDrivenTrafficGenerator, EventSkeleton, MatchedEventTrafficGenerator,
    PoissonTrafficGenerator, event_rate_per_slot, poisson_rate_per_ue)


class TrafficSensitivityTest(unittest.TestCase):
    def poisson(self, seed: int = 71, duration: int = 10_000, rate: float = 0.4):
        return PoissonTrafficGenerator(num_ues=4,
            arrival_rate_per_ue_per_slot=rate, packet_size_bits=24,
            seed=seed, duration_slots=duration)

    def event(self, seed: int = 81, duration: int = 10_000, window: int = 16,
              participation: float = 0.5, offered: float = 1.6):
        return EventDrivenTrafficGenerator(num_ues=8,
            event_rate_per_slot=event_rate_per_slot(offered, 8, participation),
            event_participation_probability=participation,
            event_window_slots=window, packet_size_bits=24, seed=seed,
            duration_slots=duration)

    def test_poisson_uses_exponential_continuous_interarrivals(self):
        trace = self.poisson().arrival_trace
        ue_zero = [x.arrival_time_slots for x in trace if x.ue_id == 0]
        gaps = [b - a for a, b in zip(ue_zero, ue_zero[1:])]
        self.assertTrue(any(not value.is_integer() for value in ue_zero))
        self.assertAlmostEqual(statistics.fmean(gaps), 1 / 0.4, delta=0.15)

    def test_same_seed_replays_and_different_seed_changes(self):
        self.assertEqual(self.poisson().arrival_trace, self.poisson().arrival_trace)
        self.assertNotEqual(self.poisson().arrival_trace, self.poisson(seed=72).arrival_trace)
        self.assertEqual(self.event().arrival_trace, self.event().arrival_trace)
        self.assertNotEqual(self.event().arrival_trace, self.event(seed=82).arrival_trace)

    def test_bernoulli_trace_is_exposed_without_changing_replay(self):
        generators = [BernoulliTrafficGenerator(num_ues=4,
            arrival_probability_per_ue=0.3, packet_size_bits=24, seed=91)
            for _ in range(2)]
        packet_traces = [[generator.arrivals(slot) for slot in range(20)]
                         for generator in generators]
        self.assertEqual(packet_traces[0], packet_traces[1])
        self.assertEqual(generators[0].arrival_trace, generators[1].arrival_trace)

    def test_reconstructed_generators_provide_matched_treatment_traces(self):
        traces = [self.event(seed=101).arrival_trace for _ in "ABCD"]
        self.assertTrue(all(trace == traces[0] for trace in traces[1:]))

    def test_poisson_allows_multiple_same_ue_same_slot_arrivals(self):
        trace = self.poisson(duration=100, rate=4.0).arrival_trace
        counts = Counter((x.ue_id, x.slot) for x in trace)
        self.assertGreater(max(counts.values()), 1)

    def test_poisson_mean_rate_and_mapping(self):
        offered = 1.6
        generator = self.poisson(rate=poisson_rate_per_ue(offered, 4))
        self.assertAlmostEqual(len(generator.arrival_trace) / 10_000, offered, delta=0.04)

    def test_event_participation_and_beta_offsets(self):
        generator = self.event(participation=1.0)
        by_event = Counter(x.event_id for x in generator.arrival_trace)
        interior = [event_id for event_id, time in enumerate(generator.event_times)
                    if 0 <= time and time + generator.event_window_slots < 10_000]
        self.assertTrue(interior)
        self.assertTrue(all(by_event[event_id] == 8 for event_id in interior))
        offsets = [x.arrival_time_slots - generator.event_times[x.event_id]
                   for x in generator.arrival_trace]
        self.assertTrue(all(0 <= value <= 16 for value in offsets))
        self.assertAlmostEqual(statistics.fmean(offsets), 16 * 3 / 7, delta=0.25)

    def test_event_mean_load_calibration_independent_of_window(self):
        rates = []
        peaks = []
        for window in (4, 16, 64):
            trace = self.event(duration=30_000, window=window).arrival_trace
            rates.append(len(trace) / 30_000)
            counts = Counter(x.slot for x in trace)
            peaks.append(max(counts.values()))
        for measured in rates:
            self.assertAlmostEqual(measured, 1.6, delta=0.06)
        self.assertGreater(peaks[0], peaks[-1])

    def test_exact_skeleton_population_is_invariant_across_windows(self):
        skeletons = (EventSkeleton.generate(start_slot=0, duration_slots=100,
            num_ues=8, event_rate_per_slot=0.6,
            participation_probability=0.5, seed=111),
            EventSkeleton.generate(start_slot=100, duration_slots=500,
            num_ues=8, event_rate_per_slot=0.6,
            participation_probability=0.5, seed=112))
        generators = [MatchedEventTrafficGenerator(skeletons=skeletons,
            event_window_slots=window, packet_size_bits=24)
            for window in (4, 16, 64)]
        populations = [{(record.event_id, record.ue_id) for record in generator.arrival_trace}
                       for generator in generators]
        self.assertEqual(populations[0], populations[1])
        self.assertEqual(populations[1], populations[2])
        self.assertEqual(len(generators[0].arrival_trace), len(generators[2].arrival_trace))
        self.assertNotEqual([x.arrival_time_slots for x in generators[0].arrival_trace],
                            [x.arrival_time_slots for x in generators[2].arrival_trace])
        samples = {(1_000_000_000 + x.event_id, x.ue_id): x.beta_sample
                   for x in skeletons[1].activations}
        for window, generator in zip((4, 16, 64), generators):
            for record in generator.arrival_trace:
                if record.event_id < 1_000_000_000:
                    continue
                local_event = record.event_id - 1_000_000_000
                displacement = (record.arrival_time_slots
                    - skeletons[1].event_times[local_event]) % skeletons[1].duration_slots
                self.assertAlmostEqual(displacement / window,
                    samples[(record.event_id, record.ue_id)])

    def test_population_mode_calibration(self):
        offered, reference_size = 3.6, 4.0
        for num_ues in (8, 16, 32):
            fixed_q = 0.5
            self.assertEqual(num_ues * fixed_q, num_ues / 2)
            self.assertAlmostEqual(event_rate_per_slot(offered, num_ues, fixed_q),
                                   offered / (num_ues * fixed_q))
            fixed_k_q = reference_size / num_ues
            self.assertAlmostEqual(num_ues * fixed_k_q, reference_size)
            self.assertAlmostEqual(event_rate_per_slot(offered, num_ues, fixed_k_q),
                                   offered / reference_size)
            self.assertAlmostEqual(num_ues * poisson_rate_per_ue(offered, num_ues), offered)

    def test_fixed_q_scales_event_size_and_fixed_k_preserves_it(self):
        fixed_q_sizes, fixed_k_sizes = [], []
        for num_ues in (8, 16, 32):
            for q, output in ((0.5, fixed_q_sizes), (4.0 / num_ues, fixed_k_sizes)):
                skeleton = EventSkeleton.generate(start_slot=0, duration_slots=20_000,
                    num_ues=num_ues, event_rate_per_slot=0.2,
                    participation_probability=q, seed=200 + num_ues)
                output.append(len(skeleton.activations) / len(skeleton.event_times))
        self.assertGreater(fixed_q_sizes[1], fixed_q_sizes[0] * 1.8)
        self.assertGreater(fixed_q_sizes[2], fixed_q_sizes[1] * 1.8)
        for size in fixed_k_sizes:
            self.assertAlmostEqual(size, 4.0, delta=0.12)

    def test_event_skeleton_replays_exactly(self):
        kwargs = dict(start_slot=100, duration_slots=1000, num_ues=16,
            event_rate_per_slot=0.4, participation_probability=0.25,
            seed=404)
        self.assertEqual(EventSkeleton.generate(**kwargs), EventSkeleton.generate(**kwargs))
        self.assertNotEqual(EventSkeleton.generate(**kwargs),
                            EventSkeleton.generate(**{**kwargs, "seed": 405}))


if __name__ == "__main__":
    unittest.main()
