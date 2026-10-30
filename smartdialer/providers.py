"""Provider abstraction and deliberately different deterministic simulators."""

import random
import time
from dataclasses import dataclass
from typing import Protocol

from smartdialer.domain import CallState, ProviderEvent, Reservation


class TelecomProvider(Protocol):
    name: str

    def events_for(self, reservation: Reservation, answer_rate: float) -> list[ProviderEvent]: ...


@dataclass
class MockProviderA:
    """Fast, reliable provider with at most one lifecycle event per stage."""

    seed: int = 1
    name: str = "provider-a"

    def events_for(self, reservation: Reservation, answer_rate: float) -> list[ProviderEvent]:
        generator = random.Random(f"{self.seed}:{reservation.call_id}")
        answered = generator.random() < answer_rate
        states = [CallState.INITIATED, CallState.RINGING]
        states += [CallState.ANSWERED, CallState.CONNECTED, CallState.COMPLETED] if answered else [CallState.FAILED]
        return _events(reservation.call_id, states)


@dataclass
class MockProviderB:
    """Slower provider with timeout, duplicate, and out-of-order event behavior."""

    seed: int = 2
    failure_rate: float = 0.12
    duplicate_rate: float = 0.25
    name: str = "provider-b"

    def events_for(self, reservation: Reservation, answer_rate: float) -> list[ProviderEvent]:
        generator = random.Random(f"{self.seed}:{reservation.call_id}")
        if generator.random() < self.failure_rate:
            return _events(reservation.call_id, [CallState.FAILED])
        answered = generator.random() < answer_rate
        states = [CallState.INITIATED, CallState.RINGING]
        states += [CallState.ANSWERED, CallState.CONNECTED, CallState.COMPLETED] if answered else [CallState.FAILED]
        events = _events(reservation.call_id, states)
        if events and generator.random() < self.duplicate_rate:
            events.insert(1, events[0])
        if len(events) >= 3 and generator.random() < 0.3:
            events[1], events[2] = events[2], events[1]
        return events


def _events(call_id: str, states: list[CallState]) -> list[ProviderEvent]:
    now = time.time()
    return [
        ProviderEvent(f"{call_id}:{sequence}:{state.value}", call_id, state, sequence, now + sequence * 0.001)
        for sequence, state in enumerate(states, start=1)
    ]