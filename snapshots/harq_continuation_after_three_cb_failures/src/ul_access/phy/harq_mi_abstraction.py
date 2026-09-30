"""Research-owned information-domain abstraction for fixed-scope HARQ IR."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Mapping, Sequence


@dataclass(frozen=True)
class HarqMiEpisodeState:
    """Protocol identity plus the compact MI accumulator for one HARQ episode."""

    mac_tb_id: str
    harq_episode_id: str
    mcs_index: int
    information_bits: int
    coded_bits: int
    information_state: "HarqInformationState"
    resource_units_per_transmission: int = 1

    @property
    def rv_history(self) -> tuple[int, ...]:
        return self.information_state.rv_history


@dataclass(frozen=True)
class RateMatchingProfile:
    """Rate-matching geometry audited from the public Sionna encoder."""

    circular_buffer_bits: int
    transmitted_bits: int
    modulation_order_bits: int
    start_by_rv: Mapping[int, int]

    def indices(self, rv: int) -> tuple[int, ...]:
        start = int(self.start_by_rv[int(rv)])
        return tuple((start + offset) % self.circular_buffer_bits
                     for offset in range(self.transmitted_bits))

    def bit_channel(self, offset: int) -> int:
        columns = self.transmitted_bits // self.modulation_order_bits
        interleaver = tuple(column * columns + row
            for row in range(columns)
            for column in range(self.modulation_order_bits))
        inverse = {source: target for target, source in enumerate(interleaver)}
        return inverse[int(offset)] % self.modulation_order_bits


@dataclass(frozen=True)
class BicmInformationTable:
    """Numerical normalized BICM bit-information mapping for one modulation."""

    sinr_db: tuple[float, ...]
    information_by_bit: tuple[tuple[float, ...], ...]

    def __post_init__(self) -> None:
        if len(self.sinr_db) != len(self.information_by_bit) or len(self.sinr_db) < 2:
            raise ValueError("BICM axes have inconsistent lengths")
        if tuple(sorted(set(self.sinr_db))) != self.sinr_db:
            raise ValueError("BICM SINR axis must be strictly increasing")
        width = len(self.information_by_bit[0])
        if width == 0 or any(len(row) != width for row in self.information_by_bit):
            raise ValueError("BICM bit-channel rows have inconsistent widths")
        if any(not 0.0 <= value <= 1.0
               for row in self.information_by_bit for value in row):
            raise ValueError("BICM information must lie in [0,1]")

    def at(self, sinr_db: float) -> tuple[float, ...]:
        value = float(sinr_db)
        if not math.isfinite(value):
            raise ValueError("SINR must be finite")
        if value <= self.sinr_db[0]:
            return self.information_by_bit[0]
        if value >= self.sinr_db[-1]:
            return self.information_by_bit[-1]
        upper = next(index for index, item in enumerate(self.sinr_db)
                     if item > value)
        lower = upper - 1
        weight = ((value-self.sinr_db[lower]) /
                  (self.sinr_db[upper]-self.sinr_db[lower]))
        return tuple(left*(1.0-weight)+right*weight for left, right in zip(
            self.information_by_bit[lower], self.information_by_bit[upper]))


@dataclass(frozen=True)
class HarqInformationState:
    """Compact additive information state; no LLR or decoder tensor is stored."""

    rv_history: tuple[int, ...] = ()
    new_bit_information: float = 0.0
    repeated_bit_information: float = 0.0
    latest_transmission_information: float = 0.0
    unique_coded_bits: int = 0

    @property
    def total_information(self) -> float:
        return self.new_bit_information + self.repeated_bit_information

    def effective_code_rate(self, information_bits: int) -> float:
        if self.unique_coded_bits == 0:
            raise ValueError("empty HARQ state has no effective code rate")
        return float(information_bits) / self.unique_coded_bits

    def append(self, *, rv: int, sinr_db: float, table: BicmInformationTable,
               profile: RateMatchingProfile) -> "HarqInformationState":
        rv = int(rv)
        if rv in self.rv_history:
            raise ValueError("an RV may appear only once in the fixed sequence")
        seen = {index for previous in self.rv_history
                for index in profile.indices(previous)}
        information = table.at(sinr_db)
        new = repeated = 0.0
        for offset, index in enumerate(profile.indices(rv)):
            contribution = information[profile.bit_channel(offset)]
            if index in seen:
                repeated += contribution
            else:
                new += contribution
        unique = len(seen | set(profile.indices(rv)))
        return HarqInformationState(
            rv_history=self.rv_history + (rv,),
            new_bit_information=self.new_bit_information + new,
            repeated_bit_information=self.repeated_bit_information + repeated,
            latest_transmission_information=new + repeated,
            unique_coded_bits=unique)


class HarqMiLogisticModel:
    """Bounded stage-specific mapping from compact MI state to success."""

    def __init__(self, parameters_by_attempt: Mapping[int, Sequence[float]],
                 rv_sequence: Sequence[int]) -> None:
        self.parameters_by_attempt = {int(key): tuple(float(x) for x in value)
                                      for key, value in parameters_by_attempt.items()}
        self.rv_sequence = tuple(int(value) for value in rv_sequence)
        if self.rv_sequence != (0, 2, 3, 1):
            raise ValueError("MI model requires fixed RV sequence 0/2/3/1")
        if set(self.parameters_by_attempt) != {1, 2, 3, 4}:
            raise ValueError("parameters are required for attempts 1 through 4")
        if any(len(value) != 6 for value in self.parameters_by_attempt.values()):
            raise ValueError("MI-C uses six compact-state coefficients")

    @staticmethod
    def features(state: HarqInformationState,
                 information_bits: int) -> tuple[float, ...]:
        new = state.new_bit_information / information_bits
        repeated = state.repeated_bit_information / information_bits
        latest = state.latest_transmission_information / information_bits
        return (1.0, new, repeated, latest, new*new, latest*latest)

    def probability(self, state: HarqInformationState,
                    information_bits: int) -> float:
        attempt = len(state.rv_history)
        if state.rv_history != self.rv_sequence[:attempt] or attempt not in range(1, 5):
            raise ValueError("state does not follow fixed RV sequence")
        eta = sum(left*right for left, right in zip(
            self.parameters_by_attempt[attempt],
            self.features(state, information_bits)))
        eta = max(-30.0, min(30.0, eta))
        return 1.0 / (1.0 + math.exp(-eta))


def build_information_state(history_db: Sequence[float], *,
                            table: BicmInformationTable,
                            profile: RateMatchingProfile,
                            rv_sequence: Sequence[int] = (0, 2, 3, 1),
                            ) -> HarqInformationState:
    state = HarqInformationState()
    for rv, sinr in zip(rv_sequence, history_db):
        state = state.append(rv=rv, sinr_db=float(sinr), table=table,
                             profile=profile)
    return state
