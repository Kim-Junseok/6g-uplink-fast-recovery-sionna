"""Small reproducible packet-arrival models."""

from __future__ import annotations

import random
import hashlib
import heapq
import math
from collections import defaultdict
from dataclasses import dataclass

from ul_access.protocol.models import Packet


def poisson_rate_per_ue(offered_packets_per_slot: float, num_ues: int) -> float:
    """Map aggregate offered load to each independent UE's Poisson rate."""
    if offered_packets_per_slot < 0 or num_ues <= 0:
        raise ValueError("offered rate must be non-negative and num_ues positive")
    return offered_packets_per_slot / num_ues


def event_rate_per_slot(offered_packets_per_slot: float, num_ues: int,
                        participation_probability: float) -> float:
    """Calibrate global events using E[packets/event] = N * participation."""
    if (offered_packets_per_slot < 0 or num_ues <= 0 or
            not 0.0 < participation_probability <= 1.0):
        raise ValueError("invalid offered rate, UE count, or participation probability")
    return offered_packets_per_slot / (num_ues * participation_probability)


class BernoulliTrafficGenerator:
    """Generate at most one fixed-size packet per UE and slot."""

    def __init__(
        self,
        *,
        num_ues: int,
        arrival_probability_per_ue: float,
        packet_size_bits: int,
        seed: int,
    ) -> None:
        if num_ues <= 0:
            raise ValueError("num_ues must be positive")
        if not 0.0 <= arrival_probability_per_ue <= 1.0:
            raise ValueError("arrival probability must be in [0, 1]")
        if packet_size_bits <= 0:
            raise ValueError("packet_size_bits must be positive")
        self._num_ues = int(num_ues)
        self._probability = float(arrival_probability_per_ue)
        self._packet_size_bits = int(packet_size_bits)
        self._rng = random.Random(seed)
        self._next_packet_id = 0
        self._arrival_trace: list[ArrivalRecord] = []

    @property
    def arrival_trace(self) -> tuple[ArrivalRecord, ...]:
        """Expose arrivals generated so far without changing Bernoulli draws."""
        return tuple(self._arrival_trace)

    def arrivals(self, slot: int) -> tuple[Packet, ...]:
        packets: list[Packet] = []
        for ue_id in range(self._num_ues):
            if self._rng.random() < self._probability:
                packets.append(
                    Packet(
                        packet_id=self._next_packet_id,
                        ue_id=ue_id,
                        arrival_slot=slot,
                        size_bits=self._packet_size_bits,
                        next_eligible_slot=slot,
                    )
                )
                self._arrival_trace.append(ArrivalRecord(
                    self._next_packet_id, ue_id, float(slot)))
                self._next_packet_id += 1
        return tuple(packets)


class EventAddressedBernoulliTraffic:
    """Bernoulli arrivals addressed independently by slot and UE."""

    def __init__(self, *, num_ues: int, arrival_probability_per_ue: float,
                 packet_size_bits: int, seed: int) -> None:
        if num_ues <= 0 or packet_size_bits <= 0:
            raise ValueError("num_ues and packet_size_bits must be positive")
        if not 0.0 <= arrival_probability_per_ue <= 1.0:
            raise ValueError("arrival probability must be in [0, 1]")
        self.num_ues = int(num_ues)
        self.probability = float(arrival_probability_per_ue)
        self.packet_size_bits = int(packet_size_bits)
        self.seed = int(seed)

    def _uniform(self, slot: int, ue: int) -> float:
        raw = f"v0.4g-traffic\0{self.seed}\0{slot}\0{ue}".encode()
        value = int.from_bytes(hashlib.blake2s(raw, digest_size=8).digest(), "big")
        return value / 2**64

    def arrivals(self, slot: int) -> tuple[Packet, ...]:
        return tuple(Packet(
            packet_id=slot * self.num_ues + ue, ue_id=ue,
            arrival_slot=slot, size_bits=self.packet_size_bits,
            next_eligible_slot=slot)
            for ue in range(self.num_ues)
            if self._uniform(slot, ue) < self.probability)


class CountedPoissonTrafficGenerator:
    """Generate an exact packet-count prefix of independent per-UE Poisson streams."""

    def __init__(self, *, num_ues: int, rate_per_ue_per_slot: float,
                 warmup_packets: int, measured_packets: int,
                 packet_size_bits: int, seed: int) -> None:
        if num_ues <= 0 or rate_per_ue_per_slot <= 0:
            raise ValueError("num_ues and rate_per_ue_per_slot must be positive")
        if warmup_packets < 0 or measured_packets <= 0:
            raise ValueError("invalid warm-up or measured packet count")
        self.num_ues = int(num_ues)
        self.rate_per_ue_per_slot = float(rate_per_ue_per_slot)
        self.warmup_packets = int(warmup_packets)
        self.measured_packets = int(measured_packets)
        self.packet_size_bits = int(packet_size_bits)
        self.seed = int(seed)
        self._rngs = [random.Random(self._seed_for_ue(ue))
                      for ue in range(self.num_ues)]
        heap = [(rng.expovariate(self.rate_per_ue_per_slot), ue)
                for ue, rng in enumerate(self._rngs)]
        heapq.heapify(heap)
        records: list[tuple[int, int, float, bool]] = []
        total = self.warmup_packets + self.measured_packets
        for packet_id in range(total):
            arrival, ue = heapq.heappop(heap)
            measured = packet_id >= self.warmup_packets
            records.append((packet_id, ue, arrival, measured))
            heapq.heappush(heap, (
                arrival + self._rngs[ue].expovariate(self.rate_per_ue_per_slot), ue))
        self._records = tuple(records)
        grouped: dict[int, list[tuple[int, int, float, bool]]] = defaultdict(list)
        for record in records:
            grouped[int(record[2])].append(record)
        self._by_slot = {slot: tuple(values) for slot, values in grouped.items()}
        self.duration_slots = int(math.floor(records[-1][2])) + 1
        self.measurement_start_time_slots = records[self.warmup_packets][2]
        self.measurement_start_slot = int(self.measurement_start_time_slots)
        self.arrival_stop_time_slots = records[-1][2]

    def _seed_for_ue(self, ue: int) -> int:
        raw = f"v0.4g-p2-poisson\0{self.seed}\0{ue}".encode()
        return int.from_bytes(hashlib.blake2s(raw, digest_size=8).digest(), "big")

    @property
    def arrival_trace(self) -> tuple[ArrivalRecord, ...]:
        return tuple(ArrivalRecord(packet_id, ue, arrival)
                     for packet_id, ue, arrival, _ in self._records)

    def arrivals(self, slot: int) -> tuple[Packet, ...]:
        return tuple(Packet(
            packet_id=packet_id, ue_id=ue, arrival_slot=slot,
            size_bits=self.packet_size_bits, next_eligible_slot=slot,
            continuous_arrival_time_slots=arrival,
            measurement_cohort=measured, measurement_cohort_locked=True)
            for packet_id, ue, arrival, measured in self._by_slot.get(slot, ()))


@dataclass(frozen=True)
class ArrivalRecord:
    """Immutable arrival used to replay a generated continuous-time trace."""

    packet_id: int
    ue_id: int
    arrival_time_slots: float
    event_id: int | None = None

    @property
    def slot(self) -> int:
        return int(self.arrival_time_slots)


class _PrecomputedTraffic:
    """Convert an immutable trace into fresh packets as slots are requested."""

    def __init__(self, *, packet_size_bits: int, records: list[ArrivalRecord]) -> None:
        if packet_size_bits <= 0:
            raise ValueError("packet_size_bits must be positive")
        self._packet_size_bits = int(packet_size_bits)
        self._records = tuple(records)
        grouped: dict[int, list[ArrivalRecord]] = defaultdict(list)
        for record in records:
            grouped[record.slot].append(record)
        self._by_slot = {slot: tuple(values) for slot, values in grouped.items()}

    @property
    def arrival_trace(self) -> tuple[ArrivalRecord, ...]:
        """Expose the immutable continuous-time trace for replay/debugging."""
        return self._records

    def arrivals(self, slot: int) -> tuple[Packet, ...]:
        return tuple(
            Packet(packet_id=record.packet_id, ue_id=record.ue_id,
                   arrival_slot=slot, size_bits=self._packet_size_bits,
                   next_eligible_slot=slot,
                   continuous_arrival_time_slots=record.arrival_time_slots)
            for record in self._by_slot.get(slot, ())
        )


class PoissonTrafficGenerator(_PrecomputedTraffic):
    """Independent per-UE homogeneous Poisson arrivals in continuous slot time."""

    def __init__(self, *, num_ues: int, arrival_rate_per_ue_per_slot: float,
                 packet_size_bits: int, seed: int, duration_slots: int) -> None:
        if num_ues <= 0 or duration_slots <= 0:
            raise ValueError("num_ues and duration_slots must be positive")
        if arrival_rate_per_ue_per_slot < 0:
            raise ValueError("arrival rate must be non-negative")
        rng = random.Random(seed)
        raw: list[tuple[float, int]] = []
        if arrival_rate_per_ue_per_slot:
            for ue_id in range(num_ues):
                time = rng.expovariate(arrival_rate_per_ue_per_slot)
                while time < duration_slots:
                    raw.append((time, ue_id))
                    time += rng.expovariate(arrival_rate_per_ue_per_slot)
        raw.sort()
        records = [ArrivalRecord(i, ue_id, time) for i, (time, ue_id) in enumerate(raw)]
        super().__init__(packet_size_bits=packet_size_bits, records=records)


class EventDrivenTrafficGenerator(_PrecomputedTraffic):
    """3GPP-inspired correlated traffic from Poisson global events and Beta offsets."""

    def __init__(self, *, num_ues: int, event_rate_per_slot: float,
                 event_participation_probability: float, event_window_slots: int,
                 packet_size_bits: int, seed: int, duration_slots: int,
                 beta_alpha: float = 3.0, beta_beta: float = 4.0) -> None:
        if num_ues <= 0 or duration_slots <= 0 or event_window_slots <= 0:
            raise ValueError("num_ues, duration_slots, and event_window_slots must be positive")
        if event_rate_per_slot < 0:
            raise ValueError("event rate must be non-negative")
        if not 0.0 < event_participation_probability <= 1.0:
            raise ValueError("event participation probability must be in (0, 1]")
        if beta_alpha <= 0 or beta_beta <= 0:
            raise ValueError("Beta shape parameters must be positive")
        rng = random.Random(seed)
        event_times: list[float] = []
        # Generate from one complete window before slot zero so activations in
        # the observation interval are not suppressed by a start-up boundary.
        if event_rate_per_slot:
            time = -float(event_window_slots) + rng.expovariate(event_rate_per_slot)
            while time < duration_slots:
                event_times.append(time)
                time += rng.expovariate(event_rate_per_slot)
        raw: list[tuple[float, int, int]] = []
        for event_id, event_time in enumerate(event_times):
            for ue_id in range(num_ues):
                if rng.random() < event_participation_probability:
                    arrival_time = event_time + event_window_slots * rng.betavariate(beta_alpha, beta_beta)
                    if 0.0 <= arrival_time < duration_slots:
                        raw.append((arrival_time, ue_id, event_id))
        raw.sort()
        records = [ArrivalRecord(i, ue_id, time, event_id)
                   for i, (time, ue_id, event_id) in enumerate(raw)]
        super().__init__(packet_size_bits=packet_size_bits, records=records)
        self.event_times = tuple(event_times)
        self.event_window_slots = int(event_window_slots)


@dataclass(frozen=True)
class EventActivation:
    """One canonical participating UE and its window-independent Beta sample."""

    event_id: int
    ue_id: int
    beta_sample: float


@dataclass(frozen=True)
class EventSkeleton:
    """Window-independent global events, participants, and activation samples."""

    start_slot: int
    duration_slots: int
    event_times: tuple[float, ...]
    activations: tuple[EventActivation, ...]

    @classmethod
    def generate(cls, *, start_slot: int, duration_slots: int, num_ues: int,
                 event_rate_per_slot: float, participation_probability: float,
                 seed: int, beta_alpha: float = 3.0,
                 beta_beta: float = 4.0) -> EventSkeleton:
        if start_slot < 0 or duration_slots <= 0 or num_ues <= 0:
            raise ValueError("invalid skeleton interval or UE count")
        if event_rate_per_slot < 0:
            raise ValueError("event rate must be non-negative")
        if not 0.0 < participation_probability <= 1.0:
            raise ValueError("participation probability must be in (0, 1]")
        if beta_alpha <= 0 or beta_beta <= 0:
            raise ValueError("Beta shape parameters must be positive")
        rng = random.Random(seed)
        event_times: list[float] = []
        if event_rate_per_slot:
            time = start_slot + rng.expovariate(event_rate_per_slot)
            end = start_slot + duration_slots
            while time < end:
                event_times.append(time)
                time += rng.expovariate(event_rate_per_slot)
        activations = tuple(
            EventActivation(event_id, ue_id, rng.betavariate(beta_alpha, beta_beta))
            for event_id in range(len(event_times))
            for ue_id in range(num_ues)
            if rng.random() < participation_probability
        )
        return cls(int(start_slot), int(duration_slots), tuple(event_times), activations)


class MatchedEventTrafficGenerator(_PrecomputedTraffic):
    """Derive a window treatment from exact skeletons with circular phase guards.

    Each phase is treated as a periodic observation interval. Offsets wrapping
    past its right boundary re-enter at its left boundary. This makes the packet
    population exactly invariant to the activation window without duplicating or
    censoring packets at warm-up/measurement boundaries.
    """

    def __init__(self, *, skeletons: tuple[EventSkeleton, ...],
                 event_window_slots: int, packet_size_bits: int) -> None:
        if not skeletons or event_window_slots <= 0:
            raise ValueError("at least one skeleton and a positive window are required")
        raw: list[tuple[float, int, int]] = []
        for phase_id, skeleton in enumerate(skeletons):
            end = skeleton.start_slot + skeleton.duration_slots
            if event_window_slots >= skeleton.duration_slots:
                raise ValueError("event window must be shorter than every skeleton phase")
            for activation in skeleton.activations:
                event_time = skeleton.event_times[activation.event_id]
                arrival = skeleton.start_slot + (
                    event_time - skeleton.start_slot
                    + event_window_slots * activation.beta_sample
                ) % skeleton.duration_slots
                if not skeleton.start_slot <= arrival < end:
                    raise RuntimeError("circular event mapping escaped its phase")
                raw.append((arrival, activation.ue_id,
                            phase_id * 1_000_000_000 + activation.event_id))
        raw.sort()
        records = [ArrivalRecord(i, ue_id, time, event_id)
                   for i, (time, ue_id, event_id) in enumerate(raw)]
        super().__init__(packet_size_bits=packet_size_bits, records=records)
        self.skeletons = skeletons
        self.event_window_slots = int(event_window_slots)
