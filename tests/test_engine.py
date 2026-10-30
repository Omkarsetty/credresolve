import tempfile
import unittest
from pathlib import Path

from smartdialer.domain import AgentState, CallState, DialMode, ProviderEvent
from smartdialer.engine import SmartDialer
from smartdialer.pacing import CampaignSignals, SafetyController
from smartdialer.providers import MockProviderA
from smartdialer.storage import Store


class EngineTests(unittest.TestCase):
    def make_store(self, agents: int = 2, borrowers: int = 8) -> Store:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = Store(Path(directory.name) / "dialer.sqlite")
        for index in range(agents):
            store.add_agent(f"agent-{index}")
        for index in range(borrowers):
            store.add_borrower(f"borrower-{index}", f"+1555{index:07d}")
        return store

    def test_safety_clamps_predictive_request_to_agent_capacity(self) -> None:
        signals = CampaignSignals(available_agents=3, queued_borrowers=20, ringing_calls=0,
                                  connected_calls=0, answer_rate=0.5, average_talk_seconds=120)
        decision = SafetyController().approve(12, signals)
        self.assertEqual(decision.approved, 3)
        self.assertTrue(decision.fallback_to_progressive)

    def test_provider_failure_stops_new_dials(self) -> None:
        store = self.make_store()
        dialer = SmartDialer(store, DialMode.PREDICTIVE, MockProviderA(), provider_healthy=False)
        result = dialer.run_tick()
        self.assertEqual(result["calls_initiated"], 0)
        self.assertEqual(store.metrics()["available_agents"], 2)

    def test_provider_events_are_idempotent_and_cannot_regress_state(self) -> None:
        store = self.make_store(agents=1, borrowers=1)
        reservation = store.reserve_next()
        self.assertIsNotNone(reservation)
        assert reservation is not None
        initiated = ProviderEvent("evt-1", reservation.call_id, CallState.INITIATED, 1, 1.0)
        answered = ProviderEvent("evt-2", reservation.call_id, CallState.ANSWERED, 2, 2.0)
        old_ringing = ProviderEvent("evt-3", reservation.call_id, CallState.RINGING, 1, 1.5)
        self.assertTrue(store.transition_call(initiated))
        self.assertTrue(store.transition_call(answered))
        self.assertFalse(store.transition_call(answered))
        self.assertFalse(store.transition_call(old_ringing))
        self.assertEqual(store.snapshot()["calls"][0]["state"], "ANSWERED")
        self.assertEqual(store.snapshot()["borrowers"][0]["state"], "CONTACTED")

    def test_completion_may_arrive_before_intermediate_events(self) -> None:
        store = self.make_store(agents=1, borrowers=1)
        reservation = store.reserve_next()
        assert reservation is not None
        self.assertTrue(store.transition_call(ProviderEvent("evt-init", reservation.call_id, CallState.INITIATED, 1, 1.0)))
        self.assertTrue(store.transition_call(ProviderEvent("evt-ring", reservation.call_id, CallState.RINGING, 2, 2.0)))
        self.assertTrue(store.transition_call(ProviderEvent("evt-complete", reservation.call_id, CallState.COMPLETED, 5, 5.0)))
        self.assertFalse(store.transition_call(ProviderEvent("evt-answer-late", reservation.call_id, CallState.ANSWERED, 3, 3.0)))
        self.assertEqual(store.snapshot()["calls"][0]["state"], "COMPLETED")
        self.assertEqual(store.snapshot()["borrowers"][0]["state"], "CONTACTED")

    def test_stale_call_recovery_releases_resources(self) -> None:
        store = self.make_store(agents=1, borrowers=1)
        reservation = store.reserve_next()
        assert reservation is not None
        recovered = store.recover_stale(older_than=reservation.created_at + 1)
        self.assertEqual(recovered, 1)
        self.assertEqual(store.metrics()["available_agents"], 1)
        self.assertEqual(store.metrics()["queued_borrowers"], 1)
        self.assertEqual(store.snapshot()["calls"][0]["state"], "FAILED")

    def test_worker_crash_after_answer_keeps_agent_and_call_connected(self) -> None:
        store = self.make_store(agents=1, borrowers=1)
        reservation = store.reserve_next()
        assert reservation is not None
        self.assertTrue(store.transition_call(ProviderEvent("evt-init", reservation.call_id, CallState.INITIATED, 1, 1.0)))
        self.assertTrue(store.transition_call(ProviderEvent("evt-answer", reservation.call_id, CallState.ANSWERED, 2, 2.0)))
        self.assertEqual(store.recover_stale(older_than=reservation.created_at + 1), 1)
        self.assertEqual(store.snapshot()["calls"][0]["state"], "CONNECTED")
        self.assertEqual(store.snapshot()["agents"][0]["state"], "CONNECTED")
        self.assertEqual(store.snapshot()["borrowers"][0]["state"], "CONTACTED")

    def test_agent_disappearing_during_setup_cancels_before_provider_call(self) -> None:
        store = self.make_store(agents=1, borrowers=1)
        reservation = store.reserve_next()
        assert reservation is not None
        store.set_agent_state(reservation.agent_id, AgentState.OFFLINE)
        self.assertFalse(store.begin_provider_call(reservation.call_id))
        self.assertEqual(store.snapshot()["calls"][0]["state"], "CANCELLED")
        self.assertEqual(store.metrics()["queued_borrowers"], 1)
        self.assertEqual(store.metrics()["available_agents"], 0)

    def test_simulation_never_connects_more_calls_than_agents(self) -> None:
        store = self.make_store(agents=4, borrowers=30)
        dialer = SmartDialer(store, DialMode.PREDICTIVE, MockProviderA(seed=4), answer_rate=0.7)
        result = dialer.run_tick()
        self.assertLessEqual(result["calls_initiated"], 4)
        self.assertEqual(result["abandoned_connected_calls"], 0)
        self.assertEqual(store.metrics()["available_agents"], 4)


if __name__ == "__main__":
    unittest.main()