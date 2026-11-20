from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from catalyst_data.ingestion.structured_types import FactRow, FetchReceipt
from catalyst_data.ingestion.time import timefields
from catalyst_data.storage.errors import StoreReadError

_SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts (
    request_hash TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    params_json TEXT NOT NULL,
    status TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    response_json TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS facts (
    request_hash TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    kind TEXT NOT NULL,
    subject TEXT NOT NULL,
    event_at TEXT,
    available_at TEXT,
    precision TEXT NOT NULL,
    source TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(request_hash, ordinal)
);
CREATE INDEX IF NOT EXISTS facts_kind_subject ON facts (kind, subject);
"""


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _dt(value: str | None) -> datetime | None:
    return timefields.stored_legacy(value)


class StructuredCache:
    def __init__(self, path: str | Path, offline: bool = False, *, create: bool = False):
        self.path = Path(path).expanduser()
        self.offline = offline
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(self.path, timeout=30)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(_SCHEMA)
            return

        if not self.path.is_file():
            cause = FileNotFoundError(self.path)
            raise StoreReadError(self.path, "open structured facts store", str(cause)) from cause
        uri = f"{self.path.resolve().as_uri()}?mode=ro"
        try:
            self.conn = sqlite3.connect(uri, timeout=30, uri=True)
        except sqlite3.Error as exc:
            raise StoreReadError(self.path, "open structured facts store", str(exc)) from exc
        try:
            tables = {
                row[0]
                for row in self.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if not {"receipts", "facts"}.issubset(tables):
                raise sqlite3.OperationalError("structured facts store schema is incomplete")
        except sqlite3.Error as exc:
            self.conn.close()
            raise StoreReadError(self.path, "check structured facts schema", str(exc)) from exc

    def get(self, request_hash: str) -> FetchReceipt | None:
        try:
            row = self.conn.execute(
                "SELECT request_hash, provider, endpoint, params_json, status, fetched_at, "
                "response_json, error FROM receipts WHERE request_hash = ?",
                (request_hash,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise StoreReadError(self.path, "read structured fact receipt", str(exc)) from exc
        if row is None:
            return None
        return FetchReceipt(
            row[0], row[1], row[2], json.loads(row[3]), row[4],
            _dt(row[5]), row[6], row[7],
        )

    def put(self, receipt: FetchReceipt, rows: tuple[FactRow, ...] = ()) -> FetchReceipt:
        stored = self.get(receipt.request_hash)
        if stored is not None and stored.status in {"OK", "EMPTY"}:
            # The client stores the receipt before parsing; parsed rows attach once to the pinned receipt.
            has_rows = self.conn.execute("SELECT 1 FROM facts WHERE request_hash = ? LIMIT 1",
                                         (receipt.request_hash,)).fetchone()
            if not rows or has_rows:
                return stored
            receipt = stored
        with self.conn:
            if receipt is not stored:
                self.conn.execute(
                    "INSERT OR REPLACE INTO receipts (request_hash, provider, endpoint, params_json, "
                    "status, fetched_at, response_json, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        receipt.request_hash, receipt.provider, receipt.endpoint,
                        json.dumps(receipt.params, sort_keys=True), receipt.status,
                        _iso(receipt.fetched_at), receipt.response_json, receipt.error,
                    ),
                )
            self.conn.execute("DELETE FROM facts WHERE request_hash = ?", (receipt.request_hash,))
            for ordinal, row in enumerate(rows):
                self.conn.execute(
                    "INSERT INTO facts (request_hash, ordinal, kind, subject, event_at, "
                    "available_at, precision, source, payload_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        receipt.request_hash, ordinal, row.kind, row.subject,
                        _iso(row.event_at), _iso(row.available_at), row.precision,
                        row.source, json.dumps(row.payload, sort_keys=True),
                    ),
                )
        return receipt

    def rows(self, kind: str, subject: str) -> tuple[FactRow, ...]:
        query = (
            "SELECT f.kind, f.subject, f.event_at, f.available_at, f.precision, f.source, "
            "f.payload_json FROM facts f JOIN receipts r ON r.request_hash = f.request_hash "
            "WHERE r.status = 'OK' AND f.kind = ? AND f.subject = ? "
            "ORDER BY f.event_at IS NULL, f.event_at, f.ordinal"
        )
        try:
            return tuple(
                FactRow(row[0], row[1], _dt(row[2]), _dt(row[3]), row[4], row[5], json.loads(row[6]))
                for row in self.conn.execute(query, (kind, subject))
            )
        except (sqlite3.Error, json.JSONDecodeError, ValueError) as exc:
            raise StoreReadError(self.path, "read structured facts", str(exc)) from exc
