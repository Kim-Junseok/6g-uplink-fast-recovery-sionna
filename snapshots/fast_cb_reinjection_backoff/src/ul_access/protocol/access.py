"""Scheduled and grant-free access abstractions."""

from __future__ import annotations

import random
import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Sequence

from ul_access.protocol.models import Packet, PacketQueue, PacketState


@dataclass(frozen=True)
class Transmission:
    """One head-of-line packet assigned to one frequency resource."""

    ue_id: int
    resource_id: int
    packet: Packet
    is_scheduled_rescue: bool = False
    resource_domain: str = "base"
    phy_resource_id: int | None = None


@dataclass(frozen=True)
class AccessDecision:
    """Transmissions and abstract control transactions for one slot."""

    transmissions: tuple[Transmission, ...]
    request_count: int = 0
    grant_count: int = 0


class AccessPolicy(ABC):
    """Sionna-independent access decision policy."""

    def __init__(self, *, resource_pool_size: int, max_retries: int | None = None) -> None:
        if resource_pool_size <= 0:
            raise ValueError("resource_pool_size must be positive")
        self.resource_pool_size = int(resource_pool_size)
        # Kept as a constructor compatibility shim for V0.3a configurations only.
        # Recovery semantics are exclusively owned by HarqController/RecoveryPolicy.
        if max_retries is not None and max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        self.max_retries = int(max_retries or 0)

    @abstractmethod
    def decide(
        self,
        slot: int,
        queues: Sequence[PacketQueue],
        rng: random.Random,
    ) -> AccessDecision:
        """Return this slot's transmissions."""

    @abstractmethod
    def reset_packet_access(self, packet: Packet, eligible_slot: int) -> None:
        """Reset access bookkeeping when recovery makes a packet eligible."""

    def packet_removed(self, ue_id: int, *, queue_empty: bool) -> None:
        """Notify the policy after a completed or dropped head packet."""
        del ue_id, queue_empty

    @property
    @abstractmethod
    def allows_resource_overlap(self) -> bool:
        """Whether multiple UE attempts may share one physical resource."""


class ScheduledAccess(AccessPolicy):
    """NR-inspired request/delay/grant abstraction with orthogonal resources."""

    def __init__(
        self,
        *,
        resource_pool_size: int,
        request_period_slots: int,
        grant_delay_slots: int,
        max_retries: int,
        retry_delay_slots: int,
        eligibility_mode: str = "per_packet",
    ) -> None:
        super().__init__(
            resource_pool_size=resource_pool_size, max_retries=max_retries
        )
        if request_period_slots <= 0:
            raise ValueError("request_period_slots must be positive")
        if grant_delay_slots < 0 or retry_delay_slots < 0:
            raise ValueError("delays must be non-negative")
        if eligibility_mode not in {"per_packet", "queue_until_empty"}:
            raise ValueError(
                "eligibility_mode must be 'per_packet' or 'queue_until_empty'"
            )
        self.request_period_slots = int(request_period_slots)
        self.grant_delay_slots = int(grant_delay_slots)
        self.retry_delay_slots = int(retry_delay_slots)
        self.eligibility_mode = eligibility_mode
        self._eligible_ues: set[int] = set()
        self._grant_ready_by_ue: dict[int, int] = {}

    @property
    def allows_resource_overlap(self) -> bool:
        return False

    def decide(
        self,
        slot: int,
        queues: Sequence[PacketQueue],
        rng: random.Random,
    ) -> AccessDecision:
        del rng
        if self.eligibility_mode == "queue_until_empty":
            return self._decide_queue_eligibility(slot, queues)

        request_count = 0
        ready: list[Packet] = []
        for queue in queues:
            packet = queue.peek()
            if (packet is None or packet.state not in {
                PacketState.ACCESS_WAIT, PacketState.HARQ_RETX_ACCESS_WAIT,
                PacketState.RLC_RETX_ACCESS_WAIT,
            } or slot < packet.next_eligible_slot):
                continue
            if (
                packet.request_slot is None
                and slot % self.request_period_slots == 0
            ):
                packet.request_slot = slot
                packet.grant_ready_slot = slot + self.grant_delay_slots
                request_count += 1
            if packet.grant_ready_slot is not None and packet.grant_ready_slot <= slot:
                ready.append(packet)

        ready.sort(
            key=lambda packet: (
                packet.grant_ready_slot,
                packet.arrival_slot,
                packet.ue_id,
            )
        )
        selected = ready[: self.resource_pool_size]
        transmissions = tuple(
            Transmission(packet.ue_id, resource_id, packet)
            for resource_id, packet in enumerate(selected)
        )
        return AccessDecision(
            transmissions=transmissions,
            request_count=request_count,
            grant_count=len(transmissions),
        )

    def _decide_queue_eligibility(
        self, slot: int, queues: Sequence[PacketQueue]
    ) -> AccessDecision:
        request_count = 0
        ready: list[Packet] = []
        for queue in queues:
            packet = queue.peek()
            ue_id = queue.ue_id
            if packet is None:
                self._eligible_ues.discard(ue_id)
                self._grant_ready_by_ue.pop(ue_id, None)
                continue
            if packet.state not in {
                PacketState.ACCESS_WAIT, PacketState.HARQ_RETX_ACCESS_WAIT,
                PacketState.RLC_RETX_ACCESS_WAIT,
            } or slot < packet.next_eligible_slot:
                continue
            if ue_id not in self._eligible_ues:
                grant_ready = self._grant_ready_by_ue.get(ue_id)
                if grant_ready is None and slot % self.request_period_slots == 0:
                    self._grant_ready_by_ue[ue_id] = slot + self.grant_delay_slots
                    request_count += 1
                    grant_ready = self._grant_ready_by_ue[ue_id]
                if grant_ready is not None and grant_ready <= slot:
                    self._eligible_ues.add(ue_id)
                    self._grant_ready_by_ue.pop(ue_id, None)
            if ue_id in self._eligible_ues:
                ready.append(packet)

        ready.sort(key=lambda packet: (packet.arrival_slot, packet.ue_id))
        selected = ready[: self.resource_pool_size]
        transmissions = tuple(
            Transmission(packet.ue_id, resource_id, packet)
            for resource_id, packet in enumerate(selected)
        )
        return AccessDecision(
            transmissions=transmissions,
            request_count=request_count,
            grant_count=len(transmissions),
        )

    def reset_packet_access(self, packet: Packet, eligible_slot: int) -> None:
        if self.eligibility_mode == "per_packet":
            packet.request_slot = None
            packet.grant_ready_slot = None
        packet.next_eligible_slot = eligible_slot

    def prepare_retry(self, packet: Packet, failed_slot: int) -> None:
        """V0.3a calibration compatibility; not used by the V0.3b engine."""
        self.reset_packet_access(packet, failed_slot + self.retry_delay_slots)

    def packet_removed(self, ue_id: int, *, queue_empty: bool) -> None:
        if self.eligibility_mode == "queue_until_empty" and queue_empty:
            self._eligible_ues.discard(ue_id)
            self._grant_ready_by_ue.pop(ue_id, None)


class GrantFreeAccess(AccessPolicy):
    """Periodic autonomous access with independent uniform resource choice."""

    def __init__(
        self,
        *,
        resource_pool_size: int,
        opportunity_period_slots: int,
        max_retries: int,
        retry_backoff_slots: int,
    ) -> None:
        super().__init__(
            resource_pool_size=resource_pool_size, max_retries=max_retries
        )
        if opportunity_period_slots <= 0:
            raise ValueError("opportunity_period_slots must be positive")
        if retry_backoff_slots < 0:
            raise ValueError("retry_backoff_slots must be non-negative")
        self.opportunity_period_slots = int(opportunity_period_slots)
        self.retry_backoff_slots = int(retry_backoff_slots)

    @property
    def allows_resource_overlap(self) -> bool:
        return True

    def decide(
        self,
        slot: int,
        queues: Sequence[PacketQueue],
        rng: random.Random,
    ) -> AccessDecision:
        if slot % self.opportunity_period_slots != 0:
            return AccessDecision(())
        transmissions = []
        for queue in queues:
            packet = queue.peek()
            if (packet is None or packet.state not in {
                PacketState.ACCESS_WAIT, PacketState.HARQ_RETX_ACCESS_WAIT,
                PacketState.RLC_RETX_ACCESS_WAIT,
            } or slot < packet.next_eligible_slot):
                continue
            transmissions.append(
                Transmission(
                    ue_id=packet.ue_id,
                    resource_id=rng.randrange(self.resource_pool_size),
                    packet=packet,
                )
            )
        return AccessDecision(tuple(transmissions))

    def reset_packet_access(self, packet: Packet, eligible_slot: int) -> None:
        packet.next_eligible_slot = eligible_slot

    def prepare_retry(self, packet: Packet, failed_slot: int) -> None:
        """V0.3a calibration compatibility; not used by the V0.3b engine."""
        self.reset_packet_access(packet, failed_slot + self.retry_backoff_slots)


class EventAddressedGrantFreeAccess(GrantFreeAccess):
    """Grant-free access with a PRB draw addressed by slot and UE."""

    def __init__(self, *, seed: int, **kwargs) -> None:
        super().__init__(**kwargs)
        self.seed = int(seed)

    def decide(self, slot: int, queues: Sequence[PacketQueue],
               rng: random.Random) -> AccessDecision:
        del rng
        if slot % self.opportunity_period_slots != 0:
            return AccessDecision(())
        transmissions = []
        for queue in queues:
            packet = queue.peek()
            if (packet is None or packet.state not in {
                    PacketState.ACCESS_WAIT, PacketState.HARQ_RETX_ACCESS_WAIT,
                    PacketState.RLC_RETX_ACCESS_WAIT}
                    or slot < packet.next_eligible_slot):
                continue
            raw = (f"v0.4g-cb-prb\0{self.seed}\0{slot}\0{packet.ue_id}\0"
                   f"{packet.harq_episode_id}\0{packet.harq_attempt_index}").encode()
            resource = int.from_bytes(
                hashlib.blake2s(raw, digest_size=8).digest(), "big"
            ) % self.resource_pool_size
            transmissions.append(Transmission(
                packet.ue_id, resource, packet))
        return AccessDecision(tuple(transmissions))

    def assign_available_resources(
        self, slot: int, transmissions: Sequence[Transmission],
        available_resources: Sequence[int],
    ) -> tuple[Transmission, ...]:
        """Draw directly and uniformly from the currently available PRB set."""
        resources = tuple(int(value) for value in available_resources)
        if not resources:
            return ()
        output = []
        for tx in transmissions:
            packet = tx.packet
            raw = (f"v0.4g-cb-available\0{self.seed}\0{slot}\0{packet.ue_id}\0"
                   f"{packet.harq_episode_id}\0{packet.harq_attempt_index}\0"
                   f"{','.join(map(str, resources))}").encode()
            local_rng = random.Random(int.from_bytes(
                hashlib.blake2s(raw, digest_size=8).digest(), "big"))
            output.append(Transmission(
                tx.ue_id, local_rng.choice(resources), packet,
                tx.is_scheduled_rescue))
        return tuple(output)
