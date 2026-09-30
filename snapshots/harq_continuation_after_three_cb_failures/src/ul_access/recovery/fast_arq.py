"""Cross-layer fast failure-indication abstraction."""

from ul_access.protocol.models import Packet
from ul_access.recovery.interface import RecoveryDecision, RecoveryPolicy


class FastArqPolicy(RecoveryPolicy):
    def __init__(self, *, failure_indication_delay_slots: int) -> None:
        if failure_indication_delay_slots < 0:
            raise ValueError("failure_indication_delay_slots must be non-negative")
        self.failure_indication_delay_slots = int(failure_indication_delay_slots)

    @property
    def name(self) -> str:
        return "fast_arq"

    def on_final_harq_failure(
        self, pdu: Packet, current_slot: int
    ) -> RecoveryDecision:
        del pdu
        return RecoveryDecision(
            current_slot + self.failure_indication_delay_slots,
            "fast_arq_failure_indication",
        )
