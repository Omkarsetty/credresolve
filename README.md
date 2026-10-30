# SmartDialer Prototype

A local, simulation-only outbound dialer demonstrating progressive and predictive pacing behind a deterministic safety controller. It does not place real calls or contain customer data.

## Run

Requires Python 3.11 or newer. The project uses only the Python standard library.

```powershell
python -m smartdialer --mode predictive --provider a --agents 10 --borrowers 100 --answer-rate 0.5 --ticks 3
python -m smartdialer --mode progressive --provider b --agents 10 --borrowers 100 --answer-rate 0.5 --ticks 3
```

Output is JSON with the pacing explanation, safety decision, outcomes, and state counts. Provider A is repeatable and reliable; provider B injects failures, duplicate events, and ordering variation. `--database path\to\campaign.sqlite` keeps the simulation state for inspection. Without it, each run uses a temporary SQLite database.

Run the test suite:

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

Run a repeatable local load simulation:

```powershell
python scripts/load_test.py --agents 100 --borrowers 5000 --ticks 3
```

## Safety and consistency

- The pacing engine proposes a call count; it has no provider access.
- The safety controller rejects unhealthy-provider requests and caps approved calls at the number of currently available, agent-bound slots and queued borrowers.
- SQLite `BEGIN IMMEDIATE` serializes reservation transactions. Agent and borrower state changes plus the call row commit together, so concurrent workers cannot reserve one agent twice.
- Provider event IDs are idempotent. Forward lifecycle events can skip missing intermediate states; late regressions and post-terminal events are ignored.
- A worker crash before answer expires a mock attempt and releases its reservations. A crash after `ANSWERED` is reconciled to `CONNECTED`, retaining the agent and preventing another dial from taking that capacity.
- Failed calls requeue the borrower. Answered/completed calls mark it contacted. Agent wrap-up is released by the synchronous simulator after the outcome is recorded.

The strict capacity cap intentionally favors zero connected-call abandonment over speculative over-dialing. Predictive mode produces a transparent proposal using answer rate, occupancy target, queue depth, and provider health, but the safety layer can reduce it to free agent capacity. This prototype does not model real call setup latency or claim predictive utilization gains; see [the architecture decision](docs/architecture.md).

## Project layout

- `smartdialer/domain.py`: agent/call states and event types.
- `smartdialer/storage.py`: SQLite transactions, reservation, event ledger, recovery, and audit log.
- `smartdialer/pacing.py`: progressive and explainable predictive proposals plus the independent safety gate.
- `smartdialer/providers.py`: provider protocol and two deterministic mock implementations.
- `smartdialer/engine.py`: campaign tick orchestration.
- `tests/`: concurrency, safety, event, and recovery tests.
- `scripts/load_test.py`: repeatable throughput smoke test.