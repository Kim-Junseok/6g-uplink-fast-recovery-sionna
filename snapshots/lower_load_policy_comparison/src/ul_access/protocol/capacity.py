"""Explicit nominal service-capacity and offered-load definitions."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class NominalCapacity:
    """Fixed-packet capacity of the configured UL resource pool."""

    num_resources_per_slot: int
    resource_payload_bits: int
    packet_size_bits: int
    packets_per_resource: int
    nominal_packets_per_slot: int

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def nominal_capacity(
    *,
    num_resources_per_slot: int,
    resource_payload_bits: int,
    packet_size_bits: int,
) -> NominalCapacity:
    """Derive nominal packets/slot by integer packet packing per resource."""
    if min(num_resources_per_slot, resource_payload_bits, packet_size_bits) <= 0:
        raise ValueError("capacity inputs must be positive")
    packets_per_resource = resource_payload_bits // packet_size_bits
    if packets_per_resource < 1:
        raise ValueError("one packet must fit within one configured resource")
    return NominalCapacity(
        num_resources_per_slot=num_resources_per_slot,
        resource_payload_bits=resource_payload_bits,
        packet_size_bits=packet_size_bits,
        packets_per_resource=packets_per_resource,
        nominal_packets_per_slot=num_resources_per_slot * packets_per_resource,
    )


def normalized_offered_load(
    *,
    num_ues: int,
    arrival_probability_per_ue: float,
    nominal_packets_per_slot: int,
) -> float:
    """Return system-wide mean arrivals/slot divided by nominal capacity."""
    if num_ues <= 0 or nominal_packets_per_slot <= 0:
        raise ValueError("UE count and nominal capacity must be positive")
    if not 0.0 <= arrival_probability_per_ue <= 1.0:
        raise ValueError("arrival probability must be in [0, 1]")
    return (
        num_ues * arrival_probability_per_ue / nominal_packets_per_slot
    )

