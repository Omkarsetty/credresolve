"""Repeatable local SQLite reservation/event throughput smoke test."""

import argparse
import json
import tempfile
import time
from pathlib import Path

from smartdialer.domain import DialMode
from smartdialer.engine import SmartDialer
from smartdialer.providers import MockProviderA
from smartdialer.storage import Store


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agents", type=int, default=100)
    parser.add_argument("--borrowers", type=int, default=5000)
    parser.add_argument("--ticks", type=int, default=3)
    parser.add_argument("--answer-rate", type=float, default=0.5)
    arguments = parser.parse_args()
    if arguments.agents < 0 or arguments.borrowers < 0 or arguments.ticks < 1:
        parser.error("agents/borrowers must be non-negative and ticks must be positive")

    with tempfile.TemporaryDirectory(prefix="smartdialer-load-") as directory:
        store = Store(Path(directory) / "load.sqlite")
        store.seed_campaign(arguments.agents, arguments.borrowers)
        dialer = SmartDialer(store, DialMode.PREDICTIVE, MockProviderA(), arguments.answer_rate)
        started = time.perf_counter()
        tick_results = [dialer.run_tick() for _ in range(arguments.ticks)]
        elapsed = time.perf_counter() - started
        calls = sum(result["calls_initiated"] for result in tick_results)
        assert all(result["calls_initiated"] <= arguments.agents for result in tick_results)
        print(json.dumps({
            "agents": arguments.agents,
            "borrowers": arguments.borrowers,
            "ticks": arguments.ticks,
            "calls_initiated": calls,
            "elapsed_seconds": round(elapsed, 3),
            "calls_per_second": round(calls / elapsed, 2) if elapsed else 0,
            "final_counts": store.counts(),
        }, indent=2))


if __name__ == "__main__":
    main()