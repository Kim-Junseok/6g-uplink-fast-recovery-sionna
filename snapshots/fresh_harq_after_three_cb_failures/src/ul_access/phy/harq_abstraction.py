"""Compact, research-owned HARQ-IR state and conditional-success lookup."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable, Mapping, Sequence


@dataclass(frozen=True)
class HarqEpisodeState:
    """System-level evidence retained for one failed HARQ episode.

    This intentionally contains no information bits, codewords, or LLRs.
    """

    mac_tb_id: str
    harq_episode_id: str
    mcs_index: int
    information_bits: int
    coded_bits: int
    rv_history: tuple[int, ...]
    effective_sinr_history_db: tuple[float, ...]
    resource_units_per_transmission: int = 1

    def append(self, *, rv: int, effective_sinr_db: float) -> "HarqEpisodeState":
        if rv in self.rv_history:
            raise ValueError(f"RV {rv} is already present in this episode")
        return HarqEpisodeState(
            mac_tb_id=self.mac_tb_id,
            harq_episode_id=self.harq_episode_id,
            mcs_index=self.mcs_index,
            information_bits=self.information_bits,
            coded_bits=self.coded_bits,
            rv_history=self.rv_history + (int(rv),),
            effective_sinr_history_db=(
                self.effective_sinr_history_db + (float(effective_sinr_db),)),
            resource_units_per_transmission=self.resource_units_per_transmission,
        )


class HarqConditionalSuccessTable:
    """Bilinear lookup calibrated for one MCS, TB-size category, and RV pair."""

    def __init__(self, *, gamma_1_db: Sequence[float],
                 gamma_2_db: Sequence[float], values: Sequence[Sequence[float]],
                 mcs_index: int, information_bits: int, coded_bits: int,
                 rv_sequence: Sequence[int]) -> None:
        self.gamma_1_db = tuple(float(value) for value in gamma_1_db)
        self.gamma_2_db = tuple(float(value) for value in gamma_2_db)
        self.values = tuple(tuple(float(value) for value in row) for row in values)
        self.mcs_index = int(mcs_index)
        self.information_bits = int(information_bits)
        self.coded_bits = int(coded_bits)
        self.rv_sequence = tuple(int(value) for value in rv_sequence)
        if len(self.gamma_1_db) < 2 or len(self.gamma_2_db) < 2:
            raise ValueError("both SINR axes need at least two points")
        if tuple(sorted(set(self.gamma_1_db))) != self.gamma_1_db:
            raise ValueError("gamma_1_db must be strictly increasing")
        if tuple(sorted(set(self.gamma_2_db))) != self.gamma_2_db:
            raise ValueError("gamma_2_db must be strictly increasing")
        if len(self.values) != len(self.gamma_1_db) or any(
                len(row) != len(self.gamma_2_db) for row in self.values):
            raise ValueError("values must match the two SINR axes")
        if any(value < 0.0 or value > 1.0
               for row in self.values for value in row):
            raise ValueError("conditional success values must lie in [0, 1]")

    @classmethod
    def from_points(cls, points: Iterable[Mapping], *, mcs_index: int,
                    information_bits: int, coded_bits: int,
                    rv_sequence: Sequence[int]) -> "HarqConditionalSuccessTable":
        records = list(points)
        axis_1 = sorted({float(point["gamma_1_db"]) for point in records})
        axis_2 = sorted({float(point["gamma_2_db"]) for point in records})
        indexed = {(float(point["gamma_1_db"]), float(point["gamma_2_db"])):
                   float(point["h_conditional_success"]["estimate"])
                   for point in records}
        if len(indexed) != len(axis_1) * len(axis_2):
            raise ValueError("points do not form a complete rectangular grid")
        return cls(
            gamma_1_db=axis_1, gamma_2_db=axis_2,
            values=[[indexed[(first, second)] for second in axis_2]
                    for first in axis_1],
            mcs_index=mcs_index, information_bits=information_bits,
            coded_bits=coded_bits, rv_sequence=rv_sequence)

    @staticmethod
    def _bracket(axis: tuple[float, ...], value: float) -> tuple[int, int, float]:
        if value < axis[0] or value > axis[-1]:
            raise ValueError(
                f"SINR {value} lies outside calibrated range [{axis[0]}, {axis[-1]}]")
        if value == axis[-1]:
            return len(axis) - 2, len(axis) - 1, 1.0
        upper = next(index for index, item in enumerate(axis) if item > value)
        lower = upper - 1
        weight = (value - axis[lower]) / (axis[upper] - axis[lower])
        return lower, upper, weight

    def probability(self, state: HarqEpisodeState, *, rescue_rv: int,
                    rescue_effective_sinr_db: float) -> float:
        if state.mcs_index != self.mcs_index:
            raise ValueError("MCS does not match calibration")
        if (state.information_bits, state.coded_bits) != (
                self.information_bits, self.coded_bits):
            raise ValueError("TB-size category does not match calibration")
        expected_history = self.rv_sequence[:-1]
        if state.rv_history != expected_history or rescue_rv != self.rv_sequence[-1]:
            raise ValueError("RV history does not match calibration")
        if len(state.effective_sinr_history_db) != 1:
            raise ValueError("V0.4b table supports exactly one prior observation")
        first = state.effective_sinr_history_db[0]
        second = float(rescue_effective_sinr_db)
        i0, i1, wi = self._bracket(self.gamma_1_db, first)
        j0, j1, wj = self._bracket(self.gamma_2_db, second)
        low = self.values[i0][j0] * (1.0 - wj) + self.values[i0][j1] * wj
        high = self.values[i1][j0] * (1.0 - wj) + self.values[i1][j1] * wj
        return low * (1.0 - wi) + high * wi


class CalibratedLogisticHarqModel:
    """Validated additive-logit model for one calibrated PHY configuration."""

    def __init__(self, *, parameters: Sequence[float], gamma_1_range_db: Sequence[float],
                 gamma_2_range_db: Sequence[float], mcs_index: int,
                 information_bits: int, coded_bits: int,
                 rv_sequence: Sequence[int]) -> None:
        if len(parameters) != 3:
            raise ValueError("parameters must contain intercept, g1, and g2")
        self.parameters = tuple(float(value) for value in parameters)
        self.gamma_1_range_db = tuple(float(value) for value in gamma_1_range_db)
        self.gamma_2_range_db = tuple(float(value) for value in gamma_2_range_db)
        self.mcs_index = int(mcs_index)
        self.information_bits = int(information_bits)
        self.coded_bits = int(coded_bits)
        self.rv_sequence = tuple(int(value) for value in rv_sequence)
        if len(self.gamma_1_range_db) != 2 or self.gamma_1_range_db[0] >= self.gamma_1_range_db[1]:
            raise ValueError("gamma_1_range_db must be an increasing pair")
        if len(self.gamma_2_range_db) != 2 or self.gamma_2_range_db[0] >= self.gamma_2_range_db[1]:
            raise ValueError("gamma_2_range_db must be an increasing pair")

    def probability(self, state: HarqEpisodeState, *, rescue_rv: int,
                    rescue_effective_sinr_db: float) -> float:
        if state.mcs_index != self.mcs_index:
            raise ValueError("MCS does not match calibration")
        if (state.information_bits, state.coded_bits) != (
                self.information_bits, self.coded_bits):
            raise ValueError("TB-size category does not match calibration")
        if state.rv_history != self.rv_sequence[:-1] or rescue_rv != self.rv_sequence[-1]:
            raise ValueError("RV history does not match calibration")
        if len(state.effective_sinr_history_db) != 1:
            raise ValueError("V0.4b model supports exactly one prior observation")
        first = state.effective_sinr_history_db[0]
        second = float(rescue_effective_sinr_db)
        if not self.gamma_1_range_db[0] <= first <= self.gamma_1_range_db[1]:
            raise ValueError("first-transmission SINR is outside calibration")
        if not self.gamma_2_range_db[0] <= second <= self.gamma_2_range_db[1]:
            raise ValueError("rescue-transmission SINR is outside calibration")
        eta = self.parameters[0] + self.parameters[1] * first + self.parameters[2] * second
        return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, eta))))


class HarqHistoryLogisticModel:
    """Validated V0.4c conditional-success model for RV 0/2/3/1.

    Inputs are one post-LMMSE EESM scalar per physical transmission. Values
    outside the audited/calibrated range are clipped at the boundary; this
    avoids unsupported extrapolation while retaining prior observations.
    """

    def __init__(self, *, parameters_by_attempt: Mapping[int, Sequence[float]],
                 sinr_range_db: Sequence[float], mcs_index: int,
                 information_bits: int, coded_bits: int,
                 rv_sequence: Sequence[int],
                 resource_units_per_transmission: int = 1,
                 input_bounds_by_attempt_db: Mapping[int, Sequence[Sequence[float]]] | None = None) -> None:
        self.parameters_by_attempt = {
            int(key): tuple(float(value) for value in parameters)
            for key, parameters in parameters_by_attempt.items()}
        self.sinr_range_db = tuple(float(value) for value in sinr_range_db)
        self.mcs_index = int(mcs_index)
        self.information_bits = int(information_bits)
        self.coded_bits = int(coded_bits)
        self.rv_sequence = tuple(int(value) for value in rv_sequence)
        self.resource_units_per_transmission = int(resource_units_per_transmission)
        self.input_bounds_by_attempt_db = (
            {int(attempt): tuple((float(bounds[0]), float(bounds[1]))
                                 for bounds in coordinates)
             for attempt, coordinates in input_bounds_by_attempt_db.items()}
            if input_bounds_by_attempt_db is not None else None)
        if self.rv_sequence != (0, 2, 3, 1):
            raise ValueError("V0.4c is calibrated only for RV sequence 0/2/3/1")
        if set(self.parameters_by_attempt) != {1, 2, 3, 4}:
            raise ValueError("parameters are required for attempts 1 through 4")
        expected = {1: 2, 2: 4, 3: 4, 4: 5}
        if any(len(self.parameters_by_attempt[key]) != size
               for key, size in expected.items()):
            raise ValueError("parameters do not match the V0.4c feature maps")
        if len(self.sinr_range_db) != 2 or self.sinr_range_db[0] >= self.sinr_range_db[1]:
            raise ValueError("sinr_range_db must be an increasing pair")
        if self.resource_units_per_transmission <= 0:
            raise ValueError("resource_units_per_transmission must be positive")
        if self.input_bounds_by_attempt_db is not None:
            if set(self.input_bounds_by_attempt_db) != {1, 2, 3, 4}:
                raise ValueError("input bounds are required for attempts 1 through 4")
            for attempt, coordinates in self.input_bounds_by_attempt_db.items():
                if len(coordinates) != attempt or any(
                        low >= high for low, high in coordinates):
                    raise ValueError("input bounds must match each history length")

    @classmethod
    def from_config(cls, config: Mapping) -> "HarqHistoryLogisticModel":
        phy = config["phy_tb_category"]
        policy = config["range_policy"]
        selected = config["selected_model"]
        if selected["candidate"] != "D":
            raise ValueError("V0.4c integration requires validated candidate D")
        return cls(
            parameters_by_attempt={int(key): value for key, value in
                                   selected["parameters_by_attempt"].items()},
            sinr_range_db=(policy["global_guard_min_db"],
                           policy["global_guard_max_db"]),
            mcs_index=phy["mcs_index"], information_bits=phy["information_bits"],
            coded_bits=phy["coded_bits_per_transmission"],
            rv_sequence=phy["rv_sequence"],
            resource_units_per_transmission=phy["simulation_resources_per_transmission"],
            input_bounds_by_attempt_db=selected["input_bounds_by_attempt_db"])

    def _features(self, history_db: Sequence[float]) -> tuple[float, ...]:
        attempt = len(history_db)
        bounds = (self.input_bounds_by_attempt_db[attempt]
                  if self.input_bounds_by_attempt_db is not None
                  else (self.sinr_range_db,) * attempt)
        clipped = tuple(max(low, min(high, float(value)))
                        for value, (low, high) in zip(history_db, bounds))
        if attempt == 1:
            return (1.0, clipped[0])
        if attempt < 4:
            return (1.0, sum(clipped[:-1]), clipped[-1], clipped[-1] ** 2)
        linear = tuple(10.0 ** (value / 10.0) for value in clipped)
        return (1.0, sum(linear[:-1]), linear[-1],
                sum(value * value for value in linear[:-1]), linear[-1] ** 2)

    def probability(self, state: HarqEpisodeState, *, rv: int,
                    effective_sinr_db: float) -> float:
        if state.mcs_index != self.mcs_index:
            raise ValueError("MCS does not match calibration")
        if (state.information_bits, state.coded_bits) != (
                self.information_bits, self.coded_bits):
            raise ValueError("TB-size category does not match calibration")
        if state.resource_units_per_transmission != self.resource_units_per_transmission:
            raise ValueError("resource allocation does not match calibration")
        attempt = len(state.rv_history) + 1
        if attempt > len(self.rv_sequence):
            raise ValueError("HARQ episode exceeds the calibrated attempt limit")
        if state.rv_history != self.rv_sequence[:attempt - 1] or rv != self.rv_sequence[attempt - 1]:
            raise ValueError("RV history does not match calibrated 0/2/3/1 order")
        history = state.effective_sinr_history_db + (float(effective_sinr_db),)
        features = self._features(history)
        parameters = self.parameters_by_attempt[attempt]
        eta = sum(left * right for left, right in zip(parameters, features))
        return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, eta))))
