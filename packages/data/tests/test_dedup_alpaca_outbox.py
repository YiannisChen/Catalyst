"""Synthetic regressions for Alpaca's same-transaction dedup outbox."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from catalyst_data.dedup import outbox
from catalyst_data.ingestion.collectors import alpaca
from catalyst_data.ingestion.time import timefields
from catalyst_data.storage import body_versions
from catalyst_data.storage.snapshots import load_verified_stores

INVOCATION = ["python", "-m", "catalyst_data.dedup.outbox"]
SEEN = "2026-10-27T12:05:00Z"
PUBLISHED = "2026-10-27T11:58:31Z"


def _item(article_id: int = 701, *, content: str = "<p>Synthetic full body.</p>") -> dict:
    return {
        "id": article_id,
        "created_at": PUBLISHED,
        "updated_at": "2026-10-27T12:00:00Z",
        "headline": "Synthetic Alpaca headline",
        "summary": "Synthetic summary",
        "content": content,
        "url": f"https://news.example.test/articles/{article_id}?utm_source=fixture",
        "author": "Synthetic author",
        "source": "synthetic-publisher",
        "symbols": ["SYN"],
    }


def _count(conn: sqlite3.Connection, table: str) -> int:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if exists is None:
        return 0
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot(source_path: Path, snapshot_path: Path) -> Path:
    source = sqlite3.connect(source_path)
    target = sqlite3.connect(snapshot_path)
    try:
        source.backup(target)
        target.commit()
    finally:
        target.close()
        source.close()
    return snapshot_path


def _verified(tmp_path: Path, snapshot_path: Path):
    manifest_path = tmp_path / "alpaca-inputs.json"
    manifest_path.write_text(json.dumps({
        "schema_version": 1,
        "invocation": INVOCATION,
        "files": [{"path": str(snapshot_path.resolve()), "sha256": _sha(snapshot_path)}],
        "environment": {},
    }, sort_keys=True))
    return load_verified_stores(
        manifest_path,
        invocation=INVOCATION,
        required_stores={"alpaca-store-v1": snapshot_path.resolve()},
        environ={},
    )


def test_alpaca_version_and_outbox_capture_provider_identity_and_published_assertion(tmp_path):
    db_path = tmp_path / "alpaca.sqlite3"
    conn = alpaca.connect(db_path)
    item = _item()

    alpaca.store_news(conn, [item], SEEN)
    conn.commit()

    version = body_versions.list_versions(
        conn, source_id=alpaca.SOURCE, document_id=str(item["id"])
    )[0]
    event = conn.execute(
        "SELECT record_json,provider_record_id_kind,corrected_at,time_evidence_json "
        "FROM dedup_relation_outbox WHERE source_id=? AND source_version_id=?",
        (alpaca.SOURCE, version["version_id"]),
    ).fetchone()
    assert event is not None
    record = json.loads(event[0])
    assert record["record_id"] == str(item["id"])
    assert record["provider"] == "alpaca"
    assert record["provider_record_id"] == str(item["id"])
    assert event[1] == "article"
    assert record["url"] == item["url"]
    assert record["body_fingerprint"] == hashlib.sha256(
        item["content"].encode("utf-8")
    ).hexdigest()
    assert record["fingerprint_version"] == "sha256-full-body-v1"
    assert record["body_fingerprint_confirmed"] is True
    evidence = json.loads(event[3])
    assert evidence == {
        "kind": "source_field",
        "field": "published_at",
        "version_id": version["version_id"],
    }
    assert timefields.stored(event[2]) == timefields.stored(version["published_at"])
    assert timefields.stored(version["observed_at"]) == timefields.stored(SEEN)
    assert timefields.stored(event[2]) != timefields.stored(version["observed_at"])

    snapshot_path = _snapshot(db_path, tmp_path / "alpaca.snapshot.sqlite3")
    verified = _verified(tmp_path, snapshot_path)
    catalog = sqlite3.connect(tmp_path / "relations.sqlite3")
    assert outbox.project_verified_snapshot(
        verified, "alpaca-store-v1", catalog
    ) == 1
    correction_ref = catalog.execute(
        "SELECT correction_ref FROM dedup_candidate_records"
    ).fetchone()[0]
    audit = json.loads(correction_ref)
    assert audit == {
        "kind": "source_assertion",
        "field": "published_at",
        "version_id": version["version_id"],
    }
    assert audit["field"] != "observed_at"


def test_alpaca_replaying_same_version_keeps_one_identical_outbox_event(tmp_path):
    conn = alpaca.connect(tmp_path / "alpaca.sqlite3")
    item = _item()

    alpaca.store_news(conn, [item], SEEN)
    conn.commit()
    first = conn.execute(
        "SELECT event_id,record_json,corrected_at,time_evidence_json "
        "FROM dedup_relation_outbox"
    ).fetchone()

    alpaca.store_news(conn, [item], "2026-10-27T12:10:00Z")
    conn.commit()
    second = conn.execute(
        "SELECT event_id,record_json,corrected_at,time_evidence_json "
        "FROM dedup_relation_outbox"
    ).fetchone()

    assert first == second
    assert _count(conn, "body_versions") == 1
    assert _count(conn, "dedup_relation_outbox") == 1


def test_outbox_insert_failure_rolls_back_alpaca_news_and_body_version(tmp_path):
    conn = alpaca.connect(tmp_path / "alpaca.sqlite3")
    outbox.ensure_schema(conn)
    conn.execute(
        "CREATE TRIGGER reject_alpaca_outbox BEFORE INSERT ON dedup_relation_outbox "
        "BEGIN SELECT RAISE(ABORT, 'synthetic outbox failure'); END"
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="synthetic outbox failure"):
        alpaca.store_news(conn, [_item()], SEEN)
    conn.rollback()

    assert _count(conn, "news") == 0
    assert _count(conn, "body_versions") == 0
    assert _count(conn, "dedup_relation_outbox") == 0


class _CoverageWork:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def expect_target(self, _target):
        pass

    def acquire_target(self, _target):
        pass

    def discovery_complete(self, _complete):
        pass

    def finish(self, _status):
        pass


@pytest.mark.parametrize("created_at", ["missing", None])
def test_missing_created_at_fails_closed_without_partial_candidate_or_fabricated_time(
    tmp_path, monkeypatch, created_at
):
    conn = alpaca.connect(tmp_path / "alpaca.sqlite3")
    item = _item(702)
    if created_at == "missing":
        item.pop("created_at")
    else:
        item["created_at"] = None
    monkeypatch.setattr(
        alpaca, "_now", lambda: timefields.stored(SEEN)
    )
    monkeypatch.setattr(alpaca.resilient, "task", lambda *_a, **_k: _CoverageWork())
    monkeypatch.setattr(alpaca, "call", lambda *_a, **_k: {"news": [item]})

    result = alpaca.poll(conn, minutes=90, log=lambda *_: None)

    assert result["status"] == "ERROR"
    assert _count(conn, "news") == 0
    assert _count(conn, "body_versions") == 0
    assert _count(conn, "dedup_relation_outbox") == 0
