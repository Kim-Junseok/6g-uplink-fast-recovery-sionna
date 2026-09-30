"""Implementation of :class:`PhyBackend` using public Sionna 2.0.1 APIs."""

from __future__ import annotations

import random
from typing import Any, Mapping, Sequence

import numpy as np
import torch

from sionna.phy import config as sionna_config
from sionna.phy.channel import GenerateOFDMChannel, RayleighBlockFading
from sionna.phy.mimo import StreamManagement
from sionna.phy.ofdm import (
    EyePrecodedChannel,
    LMMSEPostEqualizationSINR,
    ResourceGrid,
)
from sionna.phy.utils import dbm_to_watt, lin_to_db
from sionna.sys import EESM, PHYAbstraction, spread_across_subcarriers

from ul_access.phy.interface import PhyBackend, PhyOutcome
from ul_access.resource import ResourceAllocation


class SionnaPhyBackend(PhyBackend):
    """Known-activity, perfect-CSI, joint-LMMSE uplink PHY abstraction."""

    def __init__(self, config: Mapping[str, Any]) -> None:
        self._config = dict(config)
        grid = config["resource_grid"]
        channel = config["channel"]
        self._num_ues = int(grid["num_ues"])
        self._num_subcarriers = int(grid["fft_size"])
        self._num_symbols = int(grid["num_ofdm_symbols"])
        self._streams_per_ue = int(grid.get("streams_per_ue", 1))
        self._num_rx_ant = int(channel["num_bs_receive_antennas"])
        self._channel_config = dict(channel)
        self._mcs_table_index = int(config["phy_abstraction"]["mcs_table_index"])
        self._mcs_category = int(config["phy_abstraction"]["mcs_category"])
        self._data_re_per_simulation_resource = int(
            config["phy_abstraction"].get("data_re_per_simulation_resource", 1))
        if self._data_re_per_simulation_resource <= 0:
            raise ValueError("data_re_per_simulation_resource must be positive")

        if self._streams_per_ue != 1:
            raise ValueError("the protocol milestone supports one stream per UE")
        self._resource_grid = ResourceGrid(
            num_ofdm_symbols=self._num_symbols,
            fft_size=self._num_subcarriers,
            subcarrier_spacing=float(grid["subcarrier_spacing_hz"]),
            num_tx=self._num_ues,
            num_streams_per_tx=self._streams_per_ue,
        )
        self._stream_management = StreamManagement(
            np.ones((1, self._num_ues), dtype=np.int32), self._streams_per_ue
        )
        self._precoded_channel = EyePrecodedChannel(
            self._resource_grid, self._stream_management
        )
        self._sinr_computer = LMMSEPostEqualizationSINR(
            self._resource_grid, self._stream_management
        )
        self._effective_sinr = EESM()
        self._phy_abstraction = PHYAbstraction(
            sinr_effective_fun=self._effective_sinr)

        if channel["model"] == "rayleigh_block":
            rayleigh = RayleighBlockFading(
                num_rx=1,
                num_rx_ant=self._num_rx_ant,
                num_tx=self._num_ues,
                num_tx_ant=1,
            )
            self._channel_generator = GenerateOFDMChannel(
                rayleigh, self._resource_grid
            )
        elif channel["model"] == "deterministic_flat":
            self._channel_generator = None
        else:
            raise ValueError(f"unsupported channel model: {channel['model']}")
        if channel.get("csi") != "perfect":
            raise ValueError("SionnaPhyBackend currently supports perfect CSI only")

    @property
    def num_ues(self) -> int:
        return self._num_ues

    @property
    def num_subcarriers(self) -> int:
        return self._num_subcarriers

    def _set_seeds(self, seed: int) -> None:
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        sionna_config.seed = seed

    def _deterministic_channel(self) -> torch.Tensor:
        vectors = self._channel_config["ue_channel_vectors"]
        if len(vectors) != self._num_ues or any(
            len(vector) != self._num_rx_ant for vector in vectors
        ):
            raise ValueError(
                "ue_channel_vectors must have shape [num_ues, num_rx_ant]"
            )
        coefficients = torch.tensor(
            [
                [complex(float(pair[0]), float(pair[1])) for pair in vector]
                for vector in vectors
            ],
            dtype=torch.complex64,
        ).transpose(0, 1)
        h_freq = coefficients.reshape(
            1, 1, self._num_rx_ant, self._num_ues, 1, 1, 1
        )
        return h_freq.expand(
            1,
            1,
            self._num_rx_ant,
            self._num_ues,
            1,
            self._num_symbols,
            self._num_subcarriers,
        ).clone()

    def _channel(self) -> torch.Tensor:
        if self._channel_generator is None:
            return self._deterministic_channel()
        return self._channel_generator(batch_size=1)

    def evaluate(
        self,
        allocation: ResourceAllocation,
        *,
        tx_power_dbm: Sequence[float],
        mcs_index: Sequence[int],
        realization_seed: int,
    ) -> PhyOutcome:
        expected_shape = (
            1,
            self._num_symbols,
            self._num_subcarriers,
            self._num_ues,
            self._streams_per_ue,
        )
        if allocation.shape != expected_shape:
            raise ValueError(
                f"allocation shape {allocation.shape} does not match {expected_shape}"
            )
        if len(tx_power_dbm) != self._num_ues:
            raise ValueError("tx_power_dbm must contain one value per UE")
        if len(mcs_index) != self._num_ues:
            raise ValueError("mcs_index must contain one value per UE")

        self._set_seeds(int(realization_seed))
        tx_dbm = torch.tensor(tx_power_dbm, dtype=torch.float32)
        tx_power_w = dbm_to_watt(tx_dbm)
        tx_power_per_symbol_w = tx_power_w.reshape(1, 1, self._num_ues).expand(
            1, self._num_symbols, self._num_ues
        )
        tx_power_grid_w = spread_across_subcarriers(
            tx_power_per_symbol_w, allocation.mask, num_tx=self._num_ues
        )

        effective_channel = self._precoded_channel(
            self._channel(), tx_power_grid_w
        )
        noise_power = torch.tensor(float(self._channel_config["noise_power_w"]))
        sinr = self._sinr_computer(
            effective_channel,
            no=noise_power,
            interference_whitening=True,
        ).reshape(expected_shape)

        mcs = torch.tensor([list(mcs_index)], dtype=torch.int32)
        # Apply EESM once on the modeled post-LMMSE resources. The simulation
        # resource is a bundle for TB accounting, so only num_allocated_re is
        # expanded; the receiver SINR sample is not duplicated or recombined.
        sinr_eff = self._effective_sinr(
            sinr, mcs, mcs_table_index=self._mcs_table_index,
            mcs_category=self._mcs_category)
        num_allocated_re = (allocation.allocated_re_per_ue()
                            * self._data_re_per_simulation_resource)
        decoded_bits, feedback, sinr_eff, tbler, bler = self._phy_abstraction(
            mcs,
            sinr_eff=sinr_eff,
            num_allocated_re=num_allocated_re,
            mcs_table_index=self._mcs_table_index,
            mcs_category=self._mcs_category,
        )

        active = allocation.allocated_re_per_ue()[0] > 0
        active_sinr: list[tuple[float, ...]] = []
        per_re_power: list[float | None] = []
        for ue in range(self._num_ues):
            selected = sinr[..., ue, :][allocation.mask[..., ue, :]]
            active_sinr.append(
                tuple(float(value) for value in lin_to_db(selected).cpu().tolist())
            )
            selected_power = tx_power_grid_w[:, ue, ...]
            selected_power = selected_power[selected_power > 0]
            per_re_power.append(
                float(selected_power[0].item()) if selected_power.numel() else None
            )

        def optional_floats(values: torch.Tensor) -> tuple[float | None, ...]:
            raw = values[0].detach().cpu().tolist()
            return tuple(
                float(value) if is_active else None
                for value, is_active in zip(raw, active)
            )

        return PhyOutcome(
            effective_sinr_db=optional_floats(lin_to_db(sinr_eff)),
            tbler=optional_floats(tbler),
            feedback=tuple(int(value) for value in feedback[0].cpu().tolist()),
            decoded_bits=tuple(
                int(value) for value in decoded_bits[0].cpu().tolist()
            ),
            post_equalization_sinr_active_db=tuple(active_sinr),
            tx_power_per_active_re_w=tuple(per_re_power),
            bler=optional_floats(bler),
        )
