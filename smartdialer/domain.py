"""Domain types and lifecycle transition rules."""

from dataclasses import dataclass
from enum import StrEnum


class AgentState(StrEnum):
    OFFLINE = "OFFLINE"
    AVAILABLE = "AVAILABLE"
    RESERVED = "RESERVED"
    DIALING = "DIALING"
    CONNECTED = "CONNECTED"
    WRAP_UP = "WRAP_UP"
    PAUSED = "PAUSED"


class CallState(StrEnum):
    QUEUED = "QUEUED"
    RESERVED = "RESERVED"
    INITIATED = "INITIATED"
    RINGING = "RINGING"
    ANSWERED = "ANSWERED"
    CONNECTED = "CONNECTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class DialMode(StrEnum):
    PROGRESSIVE = "progressive"
    PREDICTIVE = "predictive"


@dataclass(frozen=True)
class Reservation:
    call_id: str
    agent_id: str
    borrower_id: str
    phone_number: str
    created_at: float


@dataclass(frozen=True)
class ProviderEvent:
    event_id: str
    call_id: str
    state: CallState
    sequence: int
    occurred_at: float


@dataclass(frozen=True)
class SafetyDecision:
    requested: int
    approved: int
    reason: str
    fallback_to_progressive: bool = False


CALL_TRANSITIONS: dict[CallState, frozenset[CallState]] = {
    CallState.RESERVED: frozenset({CallState.INITIATED, CallState.RINGING, CallState.ANSWERED, CallState.CONNECTED, CallState.COMPLETED, CallState.FAILED, CallState.CANCELLED}),
    CallState.INITIATED: frozenset({CallState.RINGING, CallState.ANSWERED, CallState.CONNECTED, CallState.COMPLETED, CallState.FAILED, CallState.CANCELLED}),
    CallState.RINGING: frozenset({CallState.ANSWERED, CallState.CONNECTED, CallState.COMPLETED, CallState.FAILED, CallState.CANCELLED}),
    CallState.ANSWERED: frozenset({CallState.CONNECTED, CallState.COMPLETED, CallState.FAILED}),
    CallState.CONNECTED: frozenset({CallState.COMPLETED, CallState.FAILED}),
    CallState.COMPLETED: frozenset(),
    CallState.FAILED: frozenset(),
    CallState.CANCELLED: frozenset(),
}