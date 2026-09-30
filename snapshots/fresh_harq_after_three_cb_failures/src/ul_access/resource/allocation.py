"""Resource activity masks that are independent of Sionna schedulers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import torch


@dataclass(frozen=True)
class ResourceAssignment:
    """One UE stream's activity on one time-frequency resource."""

    ue_id: int
    subcarrier: int
    ofdm_symbol: int = 0
    stream: int = 0


@dataclass(frozen=True)
class ResourceAllocation:
    """A boolean UE activity mask over an OFDM resource grid.

    The tensor layout intentionally matches the public Sionna SYS convention:
    ``[batch, ofdm_symbol, subcarrier, ue, stream]``. ``True`` means that the
    UE stream is active on the resource; it does not state how access was
    granted and does not imply a protocol-level collision.
    """

    mask: torch.Tensor

    def __post_init__(self) -> None:
        if self.mask.dtype is not torch.bool:
            raise TypeError("allocation mask must have dtype torch.bool")
        if self.mask.ndim != 5:
            raise ValueError(
                "allocation mask must have shape "
                "[batch, ofdm_symbol, subcarrier, ue, stream]"
            )

    @classmethod
    def from_assignments(
        cls,
        assignments: Iterable[ResourceAssignment],
        *,
        num_ofdm_symbols: int,
        num_subcarriers: int,
        num_ues: int,
        streams_per_ue: int = 1,
    ) -> "ResourceAllocation":
        """Build a one-batch activity mask from explicit assignments."""
        dimensions = (
            num_ofdm_symbols,
            num_subcarriers,
            num_ues,
            streams_per_ue,
        )
        if any(value <= 0 for value in dimensions):
            raise ValueError("all allocation dimensions must be positive")
        mask = torch.zeros((1, *dimensions), dtype=torch.bool)
        for assignment in assignments:
            coordinate = (
                assignment.ofdm_symbol,
                assignment.subcarrier,
                assignment.ue_id,
                assignment.stream,
            )
            if any(value < 0 or value >= limit for value, limit in zip(coordinate, dimensions)):
                raise ValueError(f"assignment coordinate {coordinate} is out of bounds")
            mask[(0, *coordinate)] = True
        return cls(mask)

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "ResourceAllocation":
        """Build an allocation from the deterministic YAML representation."""
        grid = config["resource_grid"]
        allocation = config["allocation"]
        if not allocation.get("apply_to_all_ofdm_symbols", False):
            raise ValueError("only apply_to_all_ofdm_symbols=true is supported")

        num_symbols = int(grid["num_ofdm_symbols"])
        num_subcarriers = int(grid["fft_size"])
        num_ues = int(grid["num_ues"])
        num_streams = int(grid["streams_per_ue"])
        mask = torch.zeros(
            (1, num_symbols, num_subcarriers, num_ues, num_streams),
            dtype=torch.bool,
        )

        seen_ues: set[int] = set()
        for entry in allocation["ue_resources"]:
            ue = int(entry["ue"])
            if ue < 0 or ue >= num_ues:
                raise ValueError(f"UE index {ue} is outside [0, {num_ues})")
            if ue in seen_ues:
                raise ValueError(f"duplicate allocation entry for UE {ue}")
            seen_ues.add(ue)
            for subcarrier_value in entry["subcarriers"]:
                subcarrier = int(subcarrier_value)
                if subcarrier < 0 or subcarrier >= num_subcarriers:
                    raise ValueError(
                        f"subcarrier {subcarrier} is outside [0, {num_subcarriers})"
                    )
                mask[:, :, subcarrier, ue, :] = True

        if seen_ues != set(range(num_ues)):
            raise ValueError("configuration must contain exactly one entry per UE")
        return cls(mask)

    @property
    def shape(self) -> tuple[int, ...]:
        return tuple(self.mask.shape)

    def allocated_re_per_ue(self) -> torch.Tensor:
        """Return allocated stream-RE counts with shape ``[batch, ue]``."""
        return self.mask.to(torch.int32).sum(dim=(1, 2, 4))

    def occupancy(self) -> torch.Tensor:
        """Return the number of active UEs per time-frequency resource."""
        return self.mask.any(dim=-1).to(torch.int32).sum(dim=-1)

    def has_overlap(self) -> bool:
        """Whether at least one time-frequency resource has multiple UEs."""
        return bool(torch.any(self.occupancy() > 1).item())

    def overlap_coordinates(self) -> list[list[int]]:
        """Return ``[batch, symbol, subcarrier]`` coordinates with overlap."""
        return torch.nonzero(self.occupancy() > 1, as_tuple=False).tolist()
