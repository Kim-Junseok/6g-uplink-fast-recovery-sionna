"""Scheduled-recovery resource policies independent of protocol semantics."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ScheduledResourceMode(str, Enum):
    """Relationship between scheduled recovery and the base CB pool."""

    SHARED = "shared"
    DEDICATED = "dedicated"


@dataclass(frozen=True)
class ScheduledResourcePolicy:
    """Per-slot scheduled capacity and its coupling to the base CB pool."""

    identifier: str
    mode: ScheduledResourceMode
    base_cb_prbs: int
    scheduled_capacity: int

    def __post_init__(self) -> None:
        if self.base_cb_prbs <= 0:
            raise ValueError("base_cb_prbs must be positive")
        if self.scheduled_capacity <= 0:
            raise ValueError("scheduled_capacity must be positive")
        if (self.mode == ScheduledResourceMode.SHARED and
                self.scheduled_capacity > self.base_cb_prbs):
            raise ValueError("shared scheduled capacity exceeds the base pool")

    @property
    def dedicated(self) -> bool:
        return self.mode == ScheduledResourceMode.DEDICATED

    def cb_resources(self, scheduled_service: int) -> tuple[int, ...]:
        """Return the base-domain PRBs available to CB transmissions."""
        if scheduled_service < 0 or scheduled_service > self.scheduled_capacity:
            raise ValueError("scheduled service exceeds policy capacity")
        reserved = 0 if self.dedicated else scheduled_service
        return tuple(range(reserved, self.base_cb_prbs))

    def scheduled_resource(self, service_index: int) -> tuple[str, int, int]:
        """Return domain, logical identity, and local PHY index."""
        if service_index < 0 or service_index >= self.scheduled_capacity:
            raise ValueError("scheduled service index exceeds policy capacity")
        if self.dedicated:
            return ("dedicated_sb", self.base_cb_prbs + service_index,
                    service_index)
        return ("base", service_index, service_index)


def shared_uncapped_policy(base_cb_prbs: int) -> ScheduledResourcePolicy:
    return ScheduledResourcePolicy(
        "shared_uncapped", ScheduledResourceMode.SHARED,
        base_cb_prbs, base_cb_prbs)


def scheduled_resource_policy_from_config(
    config: dict | None, *, base_cb_prbs: int
) -> ScheduledResourcePolicy:
    """Build a policy from a deterministic YAML-compatible mapping."""
    if config is None:
        return shared_uncapped_policy(base_cb_prbs)
    return ScheduledResourcePolicy(
        identifier=str(config["identifier"]),
        mode=ScheduledResourceMode(str(config["mode"])),
        base_cb_prbs=base_cb_prbs,
        scheduled_capacity=int(config["scheduled_capacity_prbs"]),
    )
