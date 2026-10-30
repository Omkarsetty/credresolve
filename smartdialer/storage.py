"""SQLite-backed state with atomic reservations and idempotent events."""

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from smartdialer.domain import CALL_TRANSITIONS, AgentState, CallState, ProviderEvent, Reservation


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exception_type: Any, exception: Any, traceback: Any) -> None:
        try:
            super().__exit__(exception_type, exception, traceback)
        finally:
            self.close()


class Store:
    def __init__(self, path: str | Path = "smartdialer.db") -> None:
        self.path = str(path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None, factory=_ClosingConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS agents (
                    id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS borrowers (
                    id TEXT PRIMARY KEY,
                    phone_number TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'QUEUED',
                    attempts INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS calls (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL REFERENCES agents(id),
                    borrower_id TEXT NOT NULL REFERENCES borrowers(id),
                    state TEXT NOT NULL,
                    provider TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS calls_active_idx
                    ON calls(state) WHERE state IN ('RESERVED', 'INITIATED', 'RINGING', 'ANSWERED', 'CONNECTED');
                CREATE TABLE IF NOT EXISTS provider_events (
                    event_id TEXT PRIMARY KEY,
                    call_id TEXT NOT NULL REFERENCES calls(id),
                    state TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    occurred_at REAL NOT NULL,
                    received_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at REAL NOT NULL,
                    action TEXT NOT NULL,
                    details TEXT NOT NULL
                );
                """
            )

    def add_agent(self, agent_id: str, state: AgentState = AgentState.AVAILABLE) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO agents(id, state) VALUES (?, ?) "
                "ON CONFLICT(id) DO UPDATE SET state = excluded.state, version = agents.version + 1",
                (agent_id, state.value),
            )

    def add_borrower(self, borrower_id: str, phone_number: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO borrowers(id, phone_number) VALUES (?, ?) "
                "ON CONFLICT(id) DO NOTHING",
                (borrower_id, phone_number),
            )

    def seed_campaign(self, agent_count: int, borrower_count: int) -> None:
        """Bulk-create deterministic demo records in one transaction."""
        if agent_count < 0 or borrower_count < 0:
            raise ValueError("campaign counts must be non-negative")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                "INSERT INTO agents(id, state) VALUES (?, 'AVAILABLE') ON CONFLICT(id) DO NOTHING",
                ((f"agent-{index + 1:04d}",) for index in range(agent_count)),
            )
            connection.executemany(
                "INSERT INTO borrowers(id, phone_number) VALUES (?, ?) ON CONFLICT(id) DO NOTHING",
                ((f"borrower-{index + 1:06d}", f"+1555{index:07d}") for index in range(borrower_count)),
            )
            connection.commit()

    def reserve_next(self) -> Reservation | None:
        """Atomically reserve one AVAILABLE agent and one QUEUED borrower."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            agent = connection.execute(
                "SELECT id FROM agents WHERE state = ? ORDER BY id LIMIT 1",
                (AgentState.AVAILABLE.value,),
            ).fetchone()
            borrower = connection.execute(
                "SELECT id, phone_number FROM borrowers WHERE state = 'QUEUED' ORDER BY id LIMIT 1"
            ).fetchone()
            if agent is None or borrower is None:
                connection.commit()
                return None

            now = time.time()
            call_id = str(uuid.uuid4())
            agent_update = connection.execute(
                "UPDATE agents SET state = ?, version = version + 1 WHERE id = ? AND state = ?",
                (AgentState.RESERVED.value, agent["id"], AgentState.AVAILABLE.value),
            )
            borrower_update = connection.execute(
                "UPDATE borrowers SET state = 'RESERVED', attempts = attempts + 1 "
                "WHERE id = ? AND state = 'QUEUED'",
                (borrower["id"],),
            )
            if agent_update.rowcount != 1 or borrower_update.rowcount != 1:
                connection.rollback()
                return None
            connection.execute(
                "INSERT INTO calls(id, agent_id, borrower_id, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (call_id, agent["id"], borrower["id"], CallState.RESERVED.value, now, now),
            )
            self._audit(connection, "call_reserved", {"call_id": call_id, "agent_id": agent["id"], "borrower_id": borrower["id"]}, now)
            connection.commit()
            return Reservation(call_id, agent["id"], borrower["id"], borrower["phone_number"], now)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def record_provider(self, call_id: str, provider: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE calls SET provider = ?, updated_at = ? WHERE id = ?",
                (provider, time.time(), call_id),
            )

    def begin_provider_call(self, call_id: str) -> bool:
        """Atomically confirm the reserved agent is still present before dialing."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT agent_id FROM calls WHERE id = ? AND state = 'RESERVED'", (call_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                return False
            update = connection.execute(
                "UPDATE agents SET state = 'DIALING', version = version + 1 "
                "WHERE id = ? AND state = 'RESERVED'",
                (row["agent_id"],),
            )
            if update.rowcount != 1:
                now = time.time()
                connection.execute("UPDATE calls SET state = 'CANCELLED', updated_at = ? WHERE id = ?", (now, call_id))
                connection.execute(
                    "UPDATE borrowers SET state = 'QUEUED' WHERE id = "
                    "(SELECT borrower_id FROM calls WHERE id = ?)",
                    (call_id,),
                )
                self._audit(connection, "call_cancelled_agent_unavailable", {"call_id": call_id}, now)
                connection.commit()
                return False
            now = time.time()
            connection.execute(
                "UPDATE calls SET state = 'INITIATED', updated_at = ? WHERE id = ?",
                (now, call_id),
            )
            self._audit(connection, "provider_call_initiated", {"call_id": call_id}, now)
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def transition_call(self, event: ProviderEvent) -> bool:
        """Apply a valid forward event once; duplicate/stale events are harmless."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            inserted = connection.execute(
                "INSERT OR IGNORE INTO provider_events(event_id, call_id, state, sequence, occurred_at, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (event.event_id, event.call_id, event.state.value, event.sequence, event.occurred_at, time.time()),
            )
            if inserted.rowcount != 1:
                connection.rollback()
                return False
            row = connection.execute(
                "SELECT state, agent_id, borrower_id FROM calls WHERE id = ?", (event.call_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(f"Unknown call: {event.call_id}")
            current_state = CallState(row["state"])
            if event.state not in CALL_TRANSITIONS.get(current_state, frozenset()):
                connection.commit()
                return False
            now = time.time()
            connection.execute(
                "UPDATE calls SET state = ?, updated_at = ? WHERE id = ?",
                (event.state.value, now, event.call_id),
            )
            if event.state == CallState.INITIATED:
                connection.execute("UPDATE agents SET state = ? WHERE id = ?", (AgentState.DIALING.value, row["agent_id"]))
            elif event.state == CallState.RINGING:
                connection.execute("UPDATE agents SET state = ? WHERE id = ?", (AgentState.DIALING.value, row["agent_id"]))
            elif event.state in {CallState.ANSWERED, CallState.CONNECTED}:
                connection.execute("UPDATE agents SET state = ? WHERE id = ?", (AgentState.CONNECTED.value, row["agent_id"]))
                connection.execute("UPDATE borrowers SET state = 'CONTACTED' WHERE id = ?", (row["borrower_id"],))
            elif event.state in {CallState.COMPLETED, CallState.FAILED, CallState.CANCELLED}:
                connection.execute("UPDATE agents SET state = ? WHERE id = ?", (AgentState.WRAP_UP.value, row["agent_id"]))
                borrower_state = "QUEUED" if event.state in {CallState.FAILED, CallState.CANCELLED} else "CONTACTED"
                connection.execute("UPDATE borrowers SET state = ? WHERE id = ?", (borrower_state, row["borrower_id"]))
            self._audit(connection, "provider_event_applied", {"call_id": event.call_id, "state": event.state.value, "sequence": event.sequence}, now)
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def recover_stale(self, older_than: float) -> int:
        """Fail stale calls after a worker crash and release their reservations."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            stale = connection.execute(
                "SELECT id, agent_id, borrower_id FROM calls "
                "WHERE state IN ('RESERVED', 'INITIATED', 'RINGING', 'ANSWERED') AND updated_at < ?",
                (older_than,),
            ).fetchall()
            now = time.time()
            for row in stale:
                call_state = connection.execute("SELECT state FROM calls WHERE id = ?", (row["id"],)).fetchone()[0]
                if call_state == CallState.ANSWERED.value:
                    connection.execute(
                        "UPDATE calls SET state = 'CONNECTED', updated_at = ? WHERE id = ?",
                        (now, row["id"]),
                    )
                    connection.execute("UPDATE agents SET state = 'CONNECTED' WHERE id = ?", (row["agent_id"],))
                    connection.execute("UPDATE borrowers SET state = 'CONTACTED' WHERE id = ?", (row["borrower_id"],))
                    self._audit(connection, "answered_call_recovered", {"call_id": row["id"]}, now)
                    continue
                connection.execute(
                    "UPDATE calls SET state = 'FAILED', updated_at = ? WHERE id = ?",
                    (now, row["id"]),
                )
                connection.execute(
                    "UPDATE agents SET state = 'AVAILABLE', version = version + 1 "
                    "WHERE id = ? AND state IN ('RESERVED', 'DIALING')",
                    (row["agent_id"],),
                )
                connection.execute(
                    "UPDATE borrowers SET state = 'QUEUED' WHERE id = ? AND state = 'RESERVED'",
                    (row["borrower_id"],),
                )
                self._audit(connection, "stale_call_recovered", {"call_id": row["id"]}, now)
            connection.commit()
            return len(stale)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def mark_agent_available(self, agent_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE agents SET state = 'AVAILABLE', version = version + 1 WHERE id = ?",
                (agent_id,),
            )

    def metrics(self) -> dict[str, int]:
        with self._connect() as connection:
            return {
                "available_agents": connection.execute("SELECT COUNT(*) FROM agents WHERE state = 'AVAILABLE'").fetchone()[0],
                "active_calls": connection.execute("SELECT COUNT(*) FROM calls WHERE state IN ('RESERVED', 'INITIATED', 'RINGING', 'ANSWERED', 'CONNECTED')").fetchone()[0],
                "queued_borrowers": connection.execute("SELECT COUNT(*) FROM borrowers WHERE state = 'QUEUED'").fetchone()[0],
                "connected_calls": connection.execute("SELECT COUNT(*) FROM calls WHERE state = 'CONNECTED'").fetchone()[0],
                "ringing_calls": connection.execute("SELECT COUNT(*) FROM calls WHERE state = 'RINGING'").fetchone()[0],
            }

    def set_agent_state(self, agent_id: str, state: AgentState) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE agents SET state = ?, version = version + 1 WHERE id = ?",
                (state.value, agent_id),
            )

    def release_wrap_up(self, agent_id: str) -> bool:
        with self._connect() as connection:
            result = connection.execute(
                "UPDATE agents SET state = ?, version = version + 1 WHERE id = ? AND state = ?",
                (AgentState.AVAILABLE.value, agent_id, AgentState.WRAP_UP.value),
            )
            return result.rowcount == 1

    def counts(self) -> dict[str, int]:
        with self._connect() as connection:
            agents = connection.execute("SELECT state, COUNT(*) AS count FROM agents GROUP BY state").fetchall()
            calls = connection.execute("SELECT state, COUNT(*) AS count FROM calls GROUP BY state").fetchall()
            borrowers = connection.execute("SELECT state, COUNT(*) AS count FROM borrowers GROUP BY state").fetchall()
        return {
            **{f"agents_{row['state'].lower()}": row["count"] for row in agents},
            **{f"calls_{row['state'].lower()}": row["count"] for row in calls},
            **{f"borrowers_{row['state'].lower()}": row["count"] for row in borrowers},
        }

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        with self._connect() as connection:
            tables = ("agents", "borrowers", "calls", "provider_events", "audit_log")
            return {
                table: [dict(row) for row in connection.execute(f"SELECT * FROM {table}").fetchall()]
                for table in tables
            }

    @staticmethod
    def _audit(connection: sqlite3.Connection, action: str, details: dict[str, Any], at: float) -> None:
        connection.execute(
            "INSERT INTO audit_log(occurred_at, action, details) VALUES (?, ?, ?)",
            (at, action, json.dumps(details, sort_keys=True)),
        )