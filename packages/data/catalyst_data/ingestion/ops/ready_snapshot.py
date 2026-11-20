"""Build a stable snapshot of the daily job store's prepared ticker/date scope."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from catalyst_data.ingestion.ops import readiness
from catalyst_data.storage.connect import connect_readonly
from catalyst_data.storage.runtime_paths import runtime_store_path

SCHEMA_VERSION = 2


def default_path(jobs_db: str | Path | None = None) -> Path:
    configured = os.environ.get("CATALYST_READY_SNAPSHOT")
    if configured:
        return Path(configured)
    jobs = Path(jobs_db) if jobs_db is not None else runtime_store_path("jobs_db")
    return jobs.with_name("ready_range.json")


def _canonical_json(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def build(jobs_db: str | Path, out: str | Path | None = None, *, generated_at: datetime | None = None) -> dict:
    source = Path(jobs_db)
    with connect_readonly(source, timeout_ms=10_000, immutable=False) as conn:
        conn.execute("BEGIN")
        pairs = [{"ticker": pair.ticker, "session_date": pair.session_date}
                 for pair in readiness.prepared_pairs_from_connection(conn)]
        has_jobs = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone()
        row = conn.execute("SELECT max(finished_at) FROM jobs WHERE status IN ('COMPLETED','PARTIAL')").fetchone() if has_jobs else None
        completed_at = row[0] if row else None
        conn.commit()
    pairs_sha256 = hashlib.sha256(_canonical_json(pairs)).hexdigest()
    stamp = generated_at or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        raise ValueError("generated_at must be timezone-aware")
    stamp = stamp.astimezone(timezone.utc).replace(microsecond=0)
    payload = {"schema_version": SCHEMA_VERSION, "generated_at": stamp.isoformat().replace("+00:00", "Z"),
               "pairs_sha256": pairs_sha256, "daily_last_completed_at": completed_at,
               "last_completed_session_date": max((pair["session_date"] for pair in pairs), default=None),
               "pairs": pairs}
    target = Path(out) if out is not None else default_path(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, target)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    build_parser = sub.add_parser("build")
    build_parser.add_argument("--jobs-db", required=True)
    build_parser.add_argument("--out")
    args = parser.parse_args(argv)
    build(args.jobs_db, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
