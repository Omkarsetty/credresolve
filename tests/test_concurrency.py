import tempfile
import threading
import unittest
from pathlib import Path

from smartdialer.domain import AgentState
from smartdialer.storage import Store


class ReservationConcurrencyTests(unittest.TestCase):
    def test_two_workers_cannot_reserve_the_same_agent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "dialer.sqlite"
            store = Store(database)
            store.add_agent("agent-1")
            store.add_borrower("borrower-1", "+15550000001")
            store.add_borrower("borrower-2", "+15550000002")
            start = threading.Barrier(3)
            results = []

            def worker() -> None:
                worker_store = Store(database)
                start.wait()
                results.append(worker_store.reserve_next())

            workers = [threading.Thread(target=worker) for _ in range(2)]
            for worker_thread in workers:
                worker_thread.start()
            start.wait()
            for worker_thread in workers:
                worker_thread.join()

            reservations = [result for result in results if result is not None]
            self.assertEqual(len(reservations), 1)
            self.assertEqual(store.counts()["agents_reserved"], 1)
            self.assertEqual(store.counts()["calls_reserved"], 1)
            self.assertEqual(store.counts()["borrowers_reserved"], 1)

    def test_offline_agent_is_never_reserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "dialer.sqlite")
            store.add_agent("agent-1", AgentState.OFFLINE)
            store.add_borrower("borrower-1", "+15550000001")
            self.assertIsNone(store.reserve_next())


if __name__ == "__main__":
    unittest.main()