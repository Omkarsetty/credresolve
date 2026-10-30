"""Campaign orchestration: pacing proposes, safety approves, store reserves, provider emits."""

from dataclasses import asdict
from typing import Any

from smartdialer.domain import AgentState, CallState, DialMode, ProviderEvent
from smartdialer.pacing import CampaignSignals, PacingEngine, SafetyController
from smartdialer.providers import MockProviderA, MockProviderB, TelecomProvider
from smartdialer.storage import Store


class SmartDialer:
    def __init__(
        self,
        store: Store,
        mode: DialMode = DialMode.PREDICTIVE,
        provider: TelecomProvider | None = None,
        answer_rate: float = 0.5,
        provider_healthy: bool = True,
    ) -> None:
        self.store = store
        self.mode = mode
        self.provider = provider or MockProviderA()
        self.answer_rate = min(max(answer_rate, 0.0), 1.0)
        self.provider_healthy = provider_healthy
        self.pacing = PacingEngine(mode)
        self.safety = SafetyController()

    def run_tick(self, max_calls: int | None = None) -> dict[str, Any]:
        metrics = self.store.metrics()
        signals = CampaignSignals(
            available_agents=metrics["available_agents"],
            queued_borrowers=metrics["queued_borrowers"],
            ringing_calls=metrics["ringing_calls"],
            connected_calls=metrics["connected_calls"],
            answer_rate=self.answer_rate,
            average_talk_seconds=180,
            provider_healthy=self.provider_healthy,
        )
        requested, explanation = self.pacing.propose(signals)
        if max_calls is not None:
            requested = min(requested, max_calls)
        decision = self.safety.approve(requested, signals)
        initiated: list[str] = []
        outcomes: list[str] = []

        for _ in range(decision.approved):
            reservation = self.store.reserve_next()
            if reservation is None:
                break
            self.store.record_provider(reservation.call_id, self.provider.name)
            if not self.store.begin_provider_call(reservation.call_id):
                outcomes.append(CallState.CANCELLED.value)
                continue
            try:
                events = self.provider.events_for(reservation, self.answer_rate)
            except Exception:
                events = [_failure_event(reservation.call_id)]
            if not events:
                events = [_failure_event(reservation.call_id)]
            initiated.append(reservation.call_id)
            for event in events:
                self.store.transition_call(event)
            outcome = self._call_state(reservation.call_id)
            outcomes.append(outcome)
            self.store.mark_agent_available(reservation.agent_id)

        after = self.store.metrics()
        return {
            "mode": self.mode.value,
            "provider": self.provider.name,
            "pacing": explanation,
            "requested": decision.requested,
            "approved": decision.approved,
            "safety": asdict(decision),
            "calls_initiated": len(initiated),
            "outcomes": outcomes,
            "metrics": after,
            "abandoned_connected_calls": 0,
        }

    def apply_event(self, event: ProviderEvent) -> bool:
        return self.store.transition_call(event)

    def _call_state(self, call_id: str) -> str:
        call = next(row for row in self.store.snapshot()["calls"] if row["id"] == call_id)
        return CallState(call["state"]).value


def _failure_event(call_id: str) -> ProviderEvent:
    import time

    return ProviderEvent(f"{call_id}:provider-exception", call_id, CallState.FAILED, 999, time.time())