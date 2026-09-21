"""All intents precede network effects; fills deduplicate within a transaction."""

import fcntl
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from trader.models import Fill, Order


def encode(value):
    return json.dumps(value, default=str, separators=(",", ":"), allow_nan=False)


class Store:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=5, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(Path(__file__).with_name("schema.sql").read_text())
        if path != ":memory:":
            os.chmod(path, 0o600)

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.db.execute(
            "INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, encode(value)),
        )

    def nonce(self, namespace: str) -> int:
        with self.transaction():
            key = "nonce:" + namespace
            value = max(time.time_ns() // 1_000_000, self.get(key, 0) + 1)
            self.set(key, value)
            return value

    def event(self, category, value, ts_ns=None):
        self.db.execute(
            "INSERT INTO events(ts_ns,category,payload) VALUES(?,?,?)",
            (ts_ns if ts_ns is not None else time.time_ns(), category, encode(value)),
        )

    def events(self, category, since=0, limit=10000):
        return [
            {"ts_ns": r[0], **json.loads(r[1])}
            for r in self.db.execute(
                "SELECT ts_ns,payload FROM events WHERE category=? AND ts_ns>=? ORDER BY id DESC LIMIT ?",
                (category, since, limit),
            ).fetchall()[::-1]
        ]

    def put_order(self, order: Order, new=False):
        if new:
            self.db.execute(
                "INSERT INTO orders VALUES (?,?,?,?,?)",
                (order.client_id, order.exchange_id, order.status, order.created_ns, order.model_dump_json()),
            )
        else:
            self.db.execute(
                "UPDATE orders SET exchange_id=?,status=?,payload=? WHERE client_id=?",
                (order.exchange_id, order.status, order.model_dump_json(), order.client_id),
            )

    def order(self, client_id):
        row = self.db.execute("SELECT payload FROM orders WHERE client_id=?", (client_id,)).fetchone()
        return Order.model_validate_json(row[0]) if row else None

    def orders(self, active_only=False):
        clause = "WHERE status IN ('intent','unknown','open')" if active_only else ""
        return [
            Order.model_validate_json(r[0])
            for r in self.db.execute(f"SELECT payload FROM orders {clause} ORDER BY created_ns, client_id")
        ]

    def fill(self, fill: Fill) -> bool:
        cursor = self.db.execute(
            "INSERT OR IGNORE INTO fills VALUES (?,?,?,?)",
            (fill.fill_id, fill.client_id, fill.ts_ns, fill.model_dump_json()),
        )
        return cursor.rowcount == 1

    def fills(self):
        return [
            Fill.model_validate_json(r[0])
            for r in self.db.execute("SELECT payload FROM fills ORDER BY ts_ns, rowid")
        ]

    def kill(self, reason, ts_ns=None):
        with self.transaction():
            self.set("killed", {"reason": reason, "ts_ns": ts_ns or time.time_ns()})
            self.set("paused", True)
            self.event("risk", {"reason": reason, "action": "KILL"}, ts_ns)

    def command(self, action):
        if action not in {"pause", "resume", "cancel-all", "flatten", "kill", "reset-kill"}:
            raise ValueError("Unsupported command")
        # Stop entry immediately, before the runtime consumes the command.
        with self.transaction():
            if action in {"pause", "cancel-all", "flatten", "kill"}:
                self.set("paused", True)
            if action == "kill":
                self.set("killed", {"reason": "OPERATOR_KILL", "ts_ns": time.time_ns()})
            cursor = self.db.execute(
                "INSERT INTO commands(created_ns,action) VALUES (?,?)", (time.time_ns(), action)
            )
            return cursor.lastrowid

    def pending_commands(self):
        return self.db.execute("SELECT * FROM commands WHERE status='pending' ORDER BY id").fetchall()

    def finish_command(self, command_id, result, success=True):
        self.db.execute(
            "UPDATE commands SET status=?,result=? WHERE id=?",
            ("done" if success else "failed", result, command_id),
        )


@contextmanager
def process_lock(path: str):
    """Linux advisory lock releases on process death; never delete its inode."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another process owns this runtime") from exc
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
