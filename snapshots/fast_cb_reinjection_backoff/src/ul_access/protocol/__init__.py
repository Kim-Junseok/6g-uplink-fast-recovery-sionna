"""Slot-based traffic and uplink-access simulation components."""

from ul_access.protocol.access import (
    EventAddressedGrantFreeAccess, GrantFreeAccess, ScheduledAccess)
from ul_access.protocol.capacity import (
    NominalCapacity,
    nominal_capacity,
    normalized_offered_load,
)
from ul_access.protocol.calibration import (
    CalibratedSimulationParameters,
    CalibratedSlotSimulator,
)
from ul_access.protocol.models import Packet, PacketQueue, PacketState
from ul_access.protocol.simulator import (
    SimulationParameters,
    SlotSimulator,
    derive_stream_seed,
)
from ul_access.protocol.traffic import (BernoulliTrafficGenerator,
                                        CountedPoissonTrafficGenerator,
                                        EventAddressedBernoulliTraffic,
                                        EventDrivenTrafficGenerator, EventSkeleton,
                                        MatchedEventTrafficGenerator,
                                        PoissonTrafficGenerator,
                                        event_rate_per_slot,
                                        poisson_rate_per_ue)
from ul_access.protocol.statistics import (
    aggregate_calibration_runs,
    student_t_confidence_interval_95,
)
from ul_access.protocol.factorial import (
    TREATMENTS, aggregate_paired_effects, scalar_metrics, seed_effects,
)

__all__ = [
    "BernoulliTrafficGenerator",
    "CountedPoissonTrafficGenerator",
    "EventAddressedBernoulliTraffic",
    "EventAddressedGrantFreeAccess",
    "PoissonTrafficGenerator",
    "EventDrivenTrafficGenerator",
    "EventSkeleton",
    "MatchedEventTrafficGenerator",
    "poisson_rate_per_ue",
    "event_rate_per_slot",
    "CalibratedSimulationParameters",
    "CalibratedSlotSimulator",
    "GrantFreeAccess",
    "NominalCapacity",
    "Packet",
    "PacketQueue",
    "PacketState",
    "ScheduledAccess",
    "SimulationParameters",
    "SlotSimulator",
    "aggregate_calibration_runs",
    "nominal_capacity",
    "normalized_offered_load",
    "student_t_confidence_interval_95",
    "derive_stream_seed",
    "TREATMENTS", "aggregate_paired_effects", "scalar_metrics", "seed_effects",
]
