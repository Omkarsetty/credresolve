"""Explainable pacing proposals; all proposals pass through SafetyController."""

import math
from dataclasses import dataclass

from smartdialer.domain import DialMode, SafetyDecision


@dataclass(frozen=True)
class CampaignSignals:
    available_agents: int
    queued_borrowers: int
    ringing_calls: int
    connected_calls: int
    answer_rate: float
    average_talk_seconds: float
    provider_healthy: bool = True


class PacingEngine:
    def __init__(self, mode: DialMode, target_occupancy: float = 0.88) -> None:
        self.mode = mode
        self.target_occupancy = min(max(target_occupancy, 0.1), 0.98)

    def propose(self, signals: CampaignSignals) -> tuple[int, str]:
        available = max(0, signals.available_agents)
        queued = max(0, signals.queued_borrowers)
        if not signals.provider_healthy or not queued or not available:
            return 0, "no queue, no free agents, or provider unhealthy"
        if self.mode == DialMode.PROGRESSIVE:
            count = min(available, queued)
            return count, f"one call per currently available agent ({count})"
        rate = min(max(signals.answer_rate, 0.05), 1.0)
        utilization_gap = max(0.0, self.target_occupancy - min(1.0, signals.connected_calls / max(1, available + signals.connected_calls)))
        desired = math.ceil((available * utilization_gap + 1) / rate)
        return min(queued, max(1, desired)), (
            f"predictive estimate: answer_rate={rate:.0%}, target_occupancy={self.target_occupancy:.0%}, "
            f"ringing={signals.ringing_calls}; proposed={min(queued, max(1, desired))}"
        )


class SafetyController:
    """Deterministic boundary: one reserved live agent per initiated call."""

    def approve(self, requested: int, signals: CampaignSignals) -> SafetyDecision:
        if requested <= 0:
            return SafetyDecision(requested, 0, "pacing requested no calls")
        if not signals.provider_healthy:
            return SafetyDecision(requested, 0, "provider health check failed", True)
        if signals.answer_rate < 0.05:
            return SafetyDecision(requested, 0, "answer rate below safe operating threshold", True)
        capacity = max(0, min(signals.available_agents, signals.queued_borrowers))
        approved = min(requested, capacity)
        fallback = approved < requested
        reason = (
            f"approved {approved} of {requested}; hard cap is {capacity} free agent-bound slots"
            if fallback
            else f"approved {approved}; every call has a distinct reserved agent"
        )
        return SafetyDecision(requested, approved, reason, fallback)