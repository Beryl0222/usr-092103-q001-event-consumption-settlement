"""只追加事件存储（SQLite）。

职责：
- 只追加（append-only）：任何已落库事件不可改写，纠错一律用新事件
  （冲正 ADJUSTMENT_POSTED / 退款 REFUND_CONFIRMED 等）表达；
- 幂等：event_id 全局唯一，业务命令携带 client_request_id，
  同一请求的重复提交（二维码重复扫描、离线补传重试）直接返回原结果；
- 乐观并发：同一聚合 (aggregate_id, version) 唯一，版本从 1 递增；
- 哈希链：每条事件携带前一条事件哈希，可发现任何对旧账的篡改。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

from .envelope import Event

GENESIS_HASH = "0" * 64


def _canonical(event: Event, prev_hash: str) -> bytes:
    body = {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "aggregate_type": event.aggregate_type,
        "aggregate_id": event.aggregate_id,
        "occurred_at": event.occurred_at,
        "version": event.version,
        "summary": event.summary,
        "payload": event.payload,
        "prev_hash": prev_hash,
    }
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def hash_event(event: Event, prev_hash: str) -> str:
    return hashlib.sha256(_canonical(event, prev_hash)).hexdigest()


SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id       TEXT NOT NULL UNIQUE,
    event_type     TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id   TEXT NOT NULL,
    version        INTEGER NOT NULL,
    occurred_at    TEXT NOT NULL,
    summary        TEXT NOT NULL,
    payload        TEXT NOT NULL,
    prev_hash      TEXT NOT NULL,
    hash           TEXT NOT NULL,
    UNIQUE (aggregate_id, version)
);
CREATE INDEX IF NOT EXISTS idx_events_agg ON events (aggregate_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_type ON events (event_type, seq);

CREATE TABLE IF NOT EXISTS idempotency (
    client_request_id TEXT PRIMARY KEY,
    result            TEXT NOT NULL,
    created_seq       INTEGER NOT NULL
);
"""


class EventStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- 读取 -----------------------------------------------------------

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> Event:
        return Event(
            event_id=row["event_id"],
            event_type=row["event_type"],
            aggregate_type=row["aggregate_type"],
            aggregate_id=row["aggregate_id"],
            occurred_at=row["occurred_at"],
            version=row["version"],
            summary=row["summary"],
            payload=json.loads(row["payload"]),
        )

    def load_stream(self, aggregate_id: str) -> list[Event]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE aggregate_id=? ORDER BY seq", (aggregate_id,)
            ).fetchall()
            return [self._row_to_event(r) for r in rows]

    def load_all(self) -> list[Event]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
            return [self._row_to_event(r) for r in rows]

    def load_by_type(self, *event_types: str) -> list[Event]:
        if not event_types:
            return []
        marks = ",".join("?" for _ in event_types)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM events WHERE event_type IN ({marks}) ORDER BY seq", event_types
            ).fetchall()
            return [self._row_to_event(r) for r in rows]

    def exists(self, event_id: str) -> bool:
        with self._lock:
            return self._conn.execute(
                "SELECT 1 FROM events WHERE event_id=?", (event_id,)
            ).fetchone() is not None

    # ---- 写入 -----------------------------------------------------------

    def next_version(self, aggregate_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(version) AS v FROM events WHERE aggregate_id=?", (aggregate_id,)
            ).fetchone()
            return (row["v"] or 0) + 1

    def append(
        self,
        events: Iterable[Event],
        *,
        client_request_id: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> list[Event]:
        """在一个事务内追加一组事件。

        同一 client_request_id 重复调用时不产生新事件，返回首次调用的
        结果（幂等命令的核心）。
        """
        events = list(events)
        with self._lock:
            if client_request_id is not None:
                row = self._conn.execute(
                    "SELECT result FROM idempotency WHERE client_request_id=?",
                    (client_request_id,),
                ).fetchone()
                if row is not None:
                    return json.loads(row["result"])  # type: ignore[return-value]

            if not events:
                raise ValueError("至少追加一条事件")

            last = self._conn.execute(
                "SELECT hash FROM events ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            prev_hash = last["hash"] if last else GENESIS_HASH

            for event in events:
                expected = self.next_version(event.aggregate_id)
                if event.version != expected:
                    raise sqlite3.IntegrityError(
                        f"聚合 {event.aggregate_id} 版本冲突：期望 {expected}，收到 {event.version}"
                    )
                digest = hash_event(event, prev_hash)
                self._conn.execute(
                    "INSERT INTO events (event_id, event_type, aggregate_type, aggregate_id,"
                    " version, occurred_at, summary, payload, prev_hash, hash)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        event.event_id,
                        event.event_type,
                        event.aggregate_type,
                        event.aggregate_id,
                        event.version,
                        event.occurred_at,
                        event.summary,
                        json.dumps(event.payload, ensure_ascii=False),
                        prev_hash,
                        digest,
                    ),
                )
                prev_hash = digest

            if client_request_id is not None:
                created_seq = self._conn.execute("SELECT MAX(seq) AS s FROM events").fetchone()["s"]
                self._conn.execute(
                    "INSERT INTO idempotency (client_request_id, result, created_seq)"
                    " VALUES (?,?,?)",
                    (client_request_id, json.dumps(result or {}, ensure_ascii=False), created_seq),
                )
            self._conn.commit()
            return events

    def replay_result(self, client_request_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT result FROM idempotency WHERE client_request_id=?", (client_request_id,)
            ).fetchone()
            return json.loads(row["result"]) if row else None

    # ---- 审计 -----------------------------------------------------------

    def verify_chain(self) -> bool:
        """重算哈希链，发现旧账被改写时返回 False。"""
        with self._lock:
            prev_hash = GENESIS_HASH
            for row in self._conn.execute("SELECT * FROM events ORDER BY seq"):
                event = self._row_to_event(row)
                if row["prev_hash"] != prev_hash or hash_event(event, prev_hash) != row["hash"]:
                    return False
                prev_hash = row["hash"]
            return True
