"""Scenario loading and feasibility-experiment assembly."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import torch
import yaml

from ul_access.phy.sionna_backend import SionnaPhyBackend
from ul_access.provenance import software_environment
from ul_access.resource import ResourceAllocation


def load_config(path: str | Path) -> dict[str, Any]:
    """Load and minimally validate a scenario configuration."""
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as stream:
        loaded = yaml.safe_load(stream)
    if not isinstance(loaded, dict):
        raise ValueError("configuration root must be a mapping")
    required = {
        "schema_version",
        "scenario",
        "seed",
        "resource_grid",
        "allocation",
        "transmit_power",
        "channel",
        "phy_abstraction",
    }
    missing = sorted(required - loaded.keys())
    if missing:
        raise ValueError(f"configuration is missing keys: {missing}")
    if loaded["schema_version"] != 1:
        raise ValueError("unsupported configuration schema_version")
    return copy.deepcopy(loaded)


def run_scenario(config: dict[str, Any]) -> dict[str, Any]:
    """Run one feasibility allocation through the shared PHY backend."""
    allocation = ResourceAllocation.from_config(config)
    backend = SionnaPhyBackend(config)
    tx_power_dbm = config["transmit_power"]["per_ue_per_ofdm_symbol_dbm"]
    mcs_index = config["phy_abstraction"]["mcs_index"]
    outcome = backend.evaluate(
        allocation,
        tx_power_dbm=tx_power_dbm,
        mcs_index=mcs_index,
        realization_seed=int(config["seed"]),
    )
    tx_power_w = [10 ** ((float(value) - 30.0) / 10.0) for value in tx_power_dbm]
    effective_linear = [
        10 ** (value / 10.0) if value is not None else None
        for value in outcome.effective_sinr_db
    ]

    return {
        "scenario": config["scenario"]["name"],
        "description": config["scenario"]["description"],
        "seed": int(config["seed"]),
        "allocation_shape": list(allocation.shape),
        "activity_mask": allocation.mask.to(torch.int8).tolist(),
        "allocated_re_per_ue": allocation.allocated_re_per_ue()[0].tolist(),
        "has_overlap": allocation.has_overlap(),
        "overlap_coordinates": allocation.overlap_coordinates(),
        "tx_power_per_ue_per_ofdm_symbol_w": tx_power_w,
        "tx_power_per_ue_per_ofdm_symbol_dbm": list(tx_power_dbm),
        "tx_power_per_active_re_w": list(outcome.tx_power_per_active_re_w),
        "post_equalization_sinr_active_db": [
            list(values) for values in outcome.post_equalization_sinr_active_db
        ],
        "effective_sinr_linear": effective_linear,
        "effective_sinr_db": list(outcome.effective_sinr_db),
        "bler": list(outcome.bler),
        "tbler": list(outcome.tbler),
        "harq_feedback": list(outcome.feedback),
        "decoding_outcome": [
            "ACK" if value == 1 else "NACK" for value in outcome.feedback
        ],
        "num_decoded_bits": list(outcome.decoded_bits),
        "mcs_index": list(mcs_index),
    }


def assert_scenario_controls_match(
    orthogonal: dict[str, Any], overlapping: dict[str, Any]
) -> None:
    """Verify that allocation (and labels) are the only scenario differences."""
    left = copy.deepcopy(orthogonal)
    right = copy.deepcopy(overlapping)
    for candidate in (left, right):
        candidate.pop("scenario")
        candidate.pop("allocation")
    if left != right:
        raise ValueError("scenario controls differ outside scenario/allocation")


__all__ = [
    "assert_scenario_controls_match",
    "load_config",
    "run_scenario",
    "software_environment",
]
