"""Actual-SLS history selection and public-Sionna validation utilities."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from typing import Iterable, Sequence

import torch
from sionna.phy.fec.ldpc import LDPC5GDecoder, LDPC5GEncoder
from sionna.phy.mapping import Demapper, Mapper

from ul_access.phy import (
    BicmInformationTable, HarqInformationState, HarqMiLogisticModel, RateMatchingProfile,
    build_information_state)


def wilson(successes: int, trials: int) -> list[float] | None:
    if trials == 0:
        return None
    z = 1.959963984540054
    estimate = successes/trials
    denominator = 1.0+z*z/trials
    center = (estimate+z*z/(2*trials))/denominator
    half = z*math.sqrt(estimate*(1-estimate)/trials+
                       z*z/(4*trials*trials))/denominator
    return [center-half, center+half]


def binomial_metric(successes: int, trials: int) -> dict:
    interval = wilson(successes, trials)
    return {"successes": successes, "failures": trials-successes,
            "trials": trials, "estimate": successes/trials if trials else None,
            "wilson_95": interval,
            "wilson_95_half_width": ((interval[1]-interval[0])/2
                                     if interval else None)}


def noise_variance_from_sinr_db(gamma_db: float) -> float:
    """Convert the common SLS effective-SINR input directly to AWGN variance."""
    value = float(gamma_db)
    if not math.isfinite(value):
        raise ValueError("effective SINR must be finite")
    return 10.0**(-value/10.0)


def mapping_components(mapping: dict):
    raw = mapping["bicm_information_table"]
    table = BicmInformationTable(tuple(raw["sinr_db"]),
        tuple(tuple(row) for row in raw["information_by_bit"]))
    audit = mapping["rate_matching_audit"]
    profile = RateMatchingProfile(
        audit["encoder"]["compressed_circular_buffer_bits"],
        audit["encoder"]["n"], 4,
        {row["rv"]: row["start"] for row in audit["sequence"]})
    parameters = {int(key): value for key, value in
        mapping["candidates"]["MI_C"]["parameters_by_attempt"].items()}
    model = HarqMiLogisticModel(parameters, (0, 2, 3, 1))
    return table, profile, model


def mi_prefix_probabilities(history_db: Sequence[float], mapping: dict) -> list[float]:
    table, profile, model = mapping_components(mapping)
    return [model.probability(build_information_state(
        history_db[:attempt], table=table, profile=profile), 24)
        for attempt in range(1, len(history_db)+1)]


def flatten_population(artifact: dict, mapping: dict,
                       tolerance_db: float = 1e-12) -> list[dict]:
    """Extract exact histories and stable row locators from the tracked artifact."""
    output = []
    _, _, model = mapping_components(mapping)
    for point_index, point in enumerate(artifact["points"]):
        for scheme, records in point["populations"].items():
            for row_index, row in enumerate(records):
                history = [float(value) for value in
                           row["diagnostic_sinr_history_db"]]
                if len(history) != int(row["attempt"]):
                    raise ValueError("history length differs from episode attempt")
                if abs(history[-1]-float(row["effective_sinr_db"])) > tolerance_db:
                    raise ValueError("serialized current SINR does not replay exactly")
                expected_rv = (0, 2, 3, 1)[len(history)-1]
                if int(row["rv"]) != expected_rv:
                    raise ValueError("RV ordering differs from accepted contract")
                compact = row["harq_information_state"]
                state = HarqInformationState(
                    rv_history=(0, 2, 3, 1)[:len(history)],
                    new_bit_information=float(compact["new_bit_information"]),
                    repeated_bit_information=float(compact["repeated_bit_information"]),
                    latest_transmission_information=float(
                        compact["latest_transmission_information"]),
                    unique_coded_bits=int(compact["unique_coded_bits"]))
                probability = model.probability(state, 24)
                probabilities = [None]*(len(history)-1)+[probability]
                output.append({**row, "diagnostic_sinr_history_db": history,
                    "traffic": point["traffic_scenario"], "seed": point["seed"],
                    "scheme": scheme, "source_point_index": point_index,
                    "source_row_index": row_index,
                    "model_prefix_success_probabilities": probabilities,
                    "model_probability": probabilities[-1]})
    return output


def missing_anatomy(records: Iterable[dict]) -> dict:
    values = list(records)
    outside = [row for row in values if row["support_class"] == "OUTSIDE_SUPPORT"]
    grouped = Counter((row["traffic"], row["scheme"], row["attempt"],
        row["physical_attempt"], row["rv"], row["access"]) for row in outside)
    return {"total_decisions": len(values), "outside_decisions": len(outside),
        "outside_fraction": len(outside)/len(values),
        "attempt_2_cb_outside": sum(count for key, count in grouped.items()
                                       if key[2] == 2 and key[5] == "CB"),
        "groups": [{"traffic": key[0], "scheme": key[1],
            "episode_attempt": key[2], "physical_attempt": key[3],
            "rv": key[4], "access": key[5], "outside_decisions": count,
            "fraction_of_all_missing": count/len(outside)}
            for key, count in grouped.most_common()]}


def _axis_bin(value: float, edges: Sequence[float]) -> int:
    return next(index for index, (low, high) in enumerate(zip(edges, edges[1:]))
                if low <= value < high)


def cell_key(row: dict, sinr_edges: Sequence[float],
             probability_edges: Sequence[float]) -> tuple:
    return (int(row["attempt"]), int(row["physical_attempt"]), int(row["rv"]),
        str(row["access"]),
        tuple(_axis_bin(value, sinr_edges)
              for value in row["diagnostic_sinr_history_db"]),
        _axis_bin(float(row["model_probability"]), probability_edges))


def select_representatives(records: Sequence[dict], *, sinr_edges: Sequence[float],
        probability_edges: Sequence[float], representatives_per_cell: int,
        target_total_coverage: float) -> dict:
    outside = [row for row in records if row["support_class"] == "OUTSIDE_SUPPORT"]
    cells: dict[tuple, list[dict]] = defaultdict(list)
    for row in outside:
        cells[cell_key(row, sinr_edges, probability_edges)].append(row)
    transition_bin = _axis_bin(0.5, probability_edges)
    ordered = sorted(cells.items(), key=lambda item: (
        -len(item[1]), 0 if item[0][-1] == transition_bin else 1,
        repr(item[0])))
    accepted = len(records)-len(outside)
    selected_cells, mass = [], 0
    for key, rows in ordered:
        selected_cells.append((key, rows)); mass += len(rows)
        if (accepted+mass)/len(records) >= target_total_coverage:
            break
    selected = []
    for cell_index, (key, rows) in enumerate(selected_cells):
        dimensions = len(rows[0]["diagnostic_sinr_history_db"])
        center = [sorted(row["diagnostic_sinr_history_db"][axis] for row in rows)
                  [(len(rows)-1)//2] for axis in range(dimensions)]
        ranked = sorted(rows, key=lambda row: (
            sum((value-center[index])**2 for index, value in enumerate(
                row["diagnostic_sinr_history_db"])),
            abs(row["model_probability"]-0.5), row["traffic"], row["scheme"],
            row["seed"], row["source_row_index"]))
        choices = [ranked[0]]
        if representatives_per_cell > 1 and len(ranked) > 1:
            boundary = max(ranked, key=lambda row: (
                sum((value-center[index])**2 for index, value in enumerate(
                    row["diagnostic_sinr_history_db"])),
                abs(row["model_probability"]-0.5)))
            choices.append(boundary)
        for rank, row in enumerate(choices[:representatives_per_cell]):
            selected.append({**row, "selection_cell_index": cell_index,
                "selection_cell_key": list(key), "cell_decision_mass": len(rows),
                "representative_role": "medoid" if rank == 0 else "boundary"})
    selected.sort(key=lambda row: (
        not (0.2 < row["model_probability"] < 0.8),
        -row["cell_decision_mass"], row["selection_cell_index"],
        row["representative_role"], row["traffic"], row["scheme"], row["seed"],
        row["source_row_index"]))
    return {"selected": selected, "cells": [{"cell_index": index,
        "key": list(key), "decision_mass": len(rows)}
        for index, (key, rows) in enumerate(selected_cells)],
        "nominal_coverage_if_all_pass": (accepted+mass)/len(records),
        "selected_outside_mass": mass, "existing_supported_mass": accepted}


class SequentialHarqAwgn:
    """Memory-bounded public-Sionna HARQ reference for one exact history."""

    def __init__(self, cfg: dict) -> None:
        self.k, self.n = int(cfg["information_bits"]), int(cfg["coded_bits_per_transmission"])
        self.qm = int(cfg["num_bits_per_symbol"])
        self.rv = tuple(int(value) for value in cfg["rv_sequence"])
        precision = str(cfg["precision"])
        self.encoder = LDPC5GEncoder(self.k, self.n,
            num_bits_per_symbol=self.qm, precision=precision, device="cpu")
        self.decoder = LDPC5GDecoder(self.encoder,
            num_iter=int(cfg["decoder_iterations"]), harq_mode=True,
            precision=precision, device="cpu")
        self.mapper = Mapper("qam", self.qm, precision=precision, device="cpu")
        self.demapper = Demapper("app", "qam", self.qm,
                                 precision=precision, device="cpu")

    @staticmethod
    def generator(seed: int) -> torch.Generator:
        return torch.Generator(device="cpu").manual_seed(int(seed))

    def _observe(self, bits: torch.Tensor, gamma_db: float,
                 generator: torch.Generator) -> torch.Tensor:
        symbols = self.mapper(bits)
        no = torch.tensor(noise_variance_from_sinr_db(gamma_db), dtype=torch.float32)
        noise = torch.complex(torch.randn(symbols.shape, generator=generator),
                              torch.randn(symbols.shape, generator=generator))
        return self.demapper(symbols+noise*torch.sqrt(no/2.0), no)

    def batch(self, history_db: Sequence[float], batch_size: int,
              seed: int) -> tuple[list[int], list[int]]:
        u = torch.randint(0, 2, (batch_size, self.k),
            generator=self.generator(seed*10+1), dtype=torch.float32)
        codewords = self.encoder(u, rv=list(self.rv[:len(history_db)]))
        observations = [self._observe(codewords[:, index, :], gamma,
            self.generator(seed*10+2+index)) for index, gamma in enumerate(history_db)]
        active = torch.ones(batch_size, dtype=torch.bool)
        entrants, successes = [], []
        for index in range(len(history_db)):
            llr = observations[0] if index == 0 else torch.stack(observations[:index+1], 1)
            decoded = self.decoder(llr, rv=list(self.rv[:index+1]))
            ok = torch.all(decoded == u, dim=-1)
            entrants.append(int(active.sum())); successes.append(int((active & ok).sum()))
            active &= ~ok
        return entrants, successes


def validate_history(reference: SequentialHarqAwgn, history_db: Sequence[float],
                     *, seed: int, sampling: dict, mapping: dict,
                     conditional_limit: float, reach_limit: float) -> dict:
    attempt, batch = len(history_db), int(sampling["batch_size"])
    maximum = int(sampling["maximum_initial_samples_by_attempt"].get(
        attempt, sampling["maximum_initial_samples_by_attempt"].get(str(attempt))))
    entrants, successes, initial = [0]*attempt, [0]*attempt, 0
    while initial < maximum:
        size = min(batch, maximum-initial)
        current_e, current_s = reference.batch(history_db, size, seed*100000+initial)
        entrants = [a+b for a,b in zip(entrants,current_e)]
        successes = [a+b for a,b in zip(successes,current_s)]
        initial += size
        target = binomial_metric(successes[-1], entrants[-1])
        if (initial >= int(sampling["minimum_initial_samples"]) and
            entrants[-1] >= int(sampling["minimum_conditioned_samples"]) and
            target["wilson_95_half_width"] <= float(sampling["wilson_95_max_half_width"])):
            break
    model_prefix = mi_prefix_probabilities(history_db, mapping)
    model_reach = math.prod(1.0-value for value in model_prefix[:-1])
    lls_reach = binomial_metric(entrants[-1], initial)
    conditional = binomial_metric(successes[-1], entrants[-1])
    conditional_error = (abs(model_prefix[-1]-conditional["estimate"])
                         if conditional["estimate"] is not None else None)
    reach_error = abs(model_reach-lls_reach["estimate"])
    if entrants[-1] < int(sampling["minimum_conditioned_samples"]):
        status = "SAMPLE-LIMITED"
    elif reach_error > reach_limit:
        status = "FAIL-STAGE-REACH"
    elif conditional_error > conditional_limit:
        status = "FAIL-CONDITIONAL"
    else:
        status = "PASS"
    return {"history_db": list(history_db), "rv_sequence": list(reference.rv[:attempt]),
        "initial_samples": initial, "conditioned_entrants": entrants[-1],
        "sequential": [{"attempt": i+1, "rv": reference.rv[i],
            "entrants": entrants[i], "success": binomial_metric(successes[i], entrants[i]),
            "model_success_probability": model_prefix[i]}
            for i in range(attempt)], "model_stage_reach_probability": model_reach,
        "lls_stage_reach": lls_reach, "stage_reach_absolute_error": reach_error,
        "model_conditional_success_probability": model_prefix[-1],
        "lls_conditional_success": conditional,
        "conditional_absolute_error": conditional_error, "status": status}


def deterministic_hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()
