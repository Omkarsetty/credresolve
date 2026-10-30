"""Command-line simulation entry point."""

import argparse
import json
import tempfile
from pathlib import Path

from smartdialer.domain import AgentState, DialMode
from smartdialer.engine import SmartDialer
from smartdialer.providers import MockProviderA, MockProviderB
from smartdialer.storage import Store


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a safe SmartDialer simulation")
    parser.add_argument("--mode", choices=[mode.value for mode in DialMode], default="predictive")
    parser.add_argument("--provider", choices=["a", "b"], default="a")
    parser.add_argument("--agents", type=int, default=10)
    parser.add_argument("--borrowers", type=int, default=100)
    parser.add_argument("--answer-rate", type=float, default=0.5)
    parser.add_argument("--ticks", type=int, default=3)
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--database", type=Path, help="SQLite path; defaults to a temporary database")
    arguments = parser.parse_args()
    if arguments.agents < 0 or arguments.borrowers < 0 or arguments.ticks < 1:
        parser.error("agents/borrowers must be non-negative and ticks must be positive")

    if arguments.database:
        store = Store(arguments.database)
    else:
        temporary = tempfile.TemporaryDirectory(prefix="smartdialer-")
        store = Store(Path(temporary.name) / "simulation.sqlite")
    store.seed_campaign(arguments.agents, arguments.borrowers)

    provider = MockProviderA(arguments.seed) if arguments.provider == "a" else MockProviderB(arguments.seed)
    dialer = SmartDialer(store, DialMode(arguments.mode), provider, arguments.answer_rate)
    result = []
    for _ in range(arguments.ticks):
        tick = dialer.run_tick()
        result.append(tick)
        if not tick["calls_initiated"]:
            break
    print(json.dumps({"ticks": result, "final_counts": store.counts()}, indent=2))


if __name__ == "__main__":
    main()