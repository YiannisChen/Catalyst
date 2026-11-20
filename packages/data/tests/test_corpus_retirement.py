from __future__ import annotations

import hashlib
import json
import sqlite3

from catalyst_data.corpus.retirement import prune_corpus_builds, purge_incomplete_builds
from catalyst_data.corpus.streaming_publication import (
    ensure_streaming_publication_schema,
)
from catalyst_data.storage.sqlite import init_db


def _insert_generation(conn: sqlite3.Connection, build_id: str, published_at: str | None,
                       status: str = "published", *, current: bool = False) -> None:
    manifest_id = hashlib.sha256(build_id.encode()).hexdigest()
    manifest_json = json.dumps({"build_id": build_id})
    conn.execute(
        "INSERT INTO corpus_publication_builds "
        "(build_id,certified_snapshot_identity,header_json,status,manifest_id,manifest_json,chunk_count,"
        "inventory_digest,lexical_expected_digest,lexical_digest,lexical_row_count,lexical_ready,"
        "created_at,updated_at,published_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (build_id, "c" * 64, json.dumps({"normalization_version": "1.0.0"}),
         "staging" if status == "published" else status,
         manifest_id, manifest_json, 1, "d" * 64, "e" * 64, "e" * 64, 1, 1,
         published_at or "2026-11-04T00:00:00Z", published_at or "2026-11-04T00:00:00Z", published_at),
    )
    conn.execute("INSERT INTO corpus_manifest(manifest_id,manifest_json,is_current,created_at) VALUES (?,?,?,?)",
                 (manifest_id, manifest_json, int(current), published_at or "2026-11-04T00:00:00Z"))
    columns = [row[1] for row in conn.execute("PRAGMA table_info(corpus_build_chunks)")]
    values = {}
    for column in columns:
        if column == "build_id":
            values[column] = build_id
        elif column == "chunk_id":
            values[column] = f"{build_id}:chunk"
        elif column == "document_id":
            values[column] = f"{build_id}:document"
        elif column == "content_text":
            values[column] = f"payload bytes for {build_id}"
        elif column == "ticker_associations":
            values[column] = "[]"
        elif column in {"body_token_start", "body_token_end", "body_overlap_tokens", "prefix_token_count",
                        "prefix_truncated", "section_parse_degraded"}:
            values[column] = 0
        else:
            values[column] = "fixture"
    conn.execute(f"INSERT INTO corpus_build_chunks ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                 [values[column] for column in columns])
    conn.execute("INSERT INTO corpus_build_chunks_fts(build_id,chunk_id,content_text) VALUES (?,?,?)",
                 (build_id, f"{build_id}:chunk", f"fts bytes for {build_id}"))
    if status == "published":
        conn.execute("UPDATE corpus_publication_builds SET status='published' WHERE build_id=?", (build_id,))


def test_prune_keeps_current_and_previous_published_and_retires_old_and_staging(tmp_path):
    db_path = tmp_path / "corpus.sqlite3"
    conn = sqlite3.connect(db_path)
    init_db(conn)
    ensure_streaming_publication_schema(conn)
    retired = []
    previous_current_build_id = None
    for day, build_id in enumerate(("one", "two", "three"), start=1):
        if previous_current_build_id is not None:
            previous_manifest = hashlib.sha256(previous_current_build_id.encode()).hexdigest()
            conn.execute("UPDATE corpus_manifest SET is_current=0 WHERE manifest_id=?", (previous_manifest,))
        _insert_generation(conn, build_id, f"2026-11-0{day}T00:00:00Z", current=True)
        conn.commit()
        retired.extend(prune_corpus_builds(conn, previous_current_build_id=previous_current_build_id))
        previous_current_build_id = build_id
    _insert_generation(conn, "failed-stage", None, status="staging")
    conn.commit()
    retired.extend(purge_incomplete_builds(conn))

    assert set(retired) == {"one", "failed-stage"}
    assert {row[0] for row in conn.execute(
        "SELECT build_id FROM corpus_publication_builds WHERE status='published'")} == {"two", "three"}
    assert conn.execute("SELECT COUNT(*) FROM corpus_build_chunks").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM corpus_build_chunks_fts").fetchone()[0] == 2
    assert {row[0] for row in conn.execute("SELECT build_id FROM corpus_retired_builds")} == set(retired)
    assert conn.execute("SELECT manifest_id FROM corpus_manifest WHERE is_current=1").fetchone()[0] == hashlib.sha256(
        b"three"
    ).hexdigest()
    from catalyst_data.ingestion.ops.daily import _generation_payload_bytes
    assert _generation_payload_bytes(conn, "three") == len("payload bytes for three") + len("fts bytes for three")
    conn.close()


def test_rollback_then_publish_preserves_rollback_target_for_next_rollback(tmp_path):
    from catalyst_data.corpus.rollback import rollback_database

    path = tmp_path / "corpus.sqlite3"
    conn = sqlite3.connect(path)
    init_db(conn)
    ensure_streaming_publication_schema(conn)
    _insert_generation(conn, "A", "2026-11-01T00:00:00Z", current=False)
    _insert_generation(conn, "B", "2026-11-02T00:00:00Z", current=False)
    b_manifest = hashlib.sha256(b"B").hexdigest()
    conn.execute("UPDATE corpus_manifest SET is_current=1 WHERE manifest_id=?", (b_manifest,))
    conn.execute(
        "INSERT INTO lexical_index_state "
        "(singleton_id,schema_version,corpus_manifest_id,mode_served,row_count,built_at,"
        "lexical_generation_id,lexical_digest) VALUES (1,'1.0.0',?,'fts5',1,'now','B',?)",
        (b_manifest, "e" * 64),
    )
    conn.commit()
    conn.close()

    assert rollback_database(path, to_build_id="A") == {"before": "B", "after": "A"}
    conn = sqlite3.connect(path)
    a_manifest = hashlib.sha256(b"A").hexdigest()
    conn.execute("UPDATE corpus_manifest SET is_current=0 WHERE manifest_id=?", (a_manifest,))
    c_manifest = hashlib.sha256(b"C").hexdigest()
    _insert_generation(conn, "C", "2026-11-03T00:00:00Z", current=True)
    conn.execute("DELETE FROM lexical_index_state")
    conn.execute(
        "INSERT INTO lexical_index_state "
        "(singleton_id,schema_version,corpus_manifest_id,mode_served,row_count,built_at,"
        "lexical_generation_id,lexical_digest) VALUES (1,'1.0.0',?,'fts5',1,'now','C',?)",
        (c_manifest, "e" * 64),
    )
    conn.commit()

    retired = prune_corpus_builds(conn, previous_current_build_id="A")

    assert retired == ["B"]
    assert {row[0] for row in conn.execute(
        "SELECT build_id FROM corpus_publication_builds WHERE status='published'")} == {"A", "C"}
    conn.close()
    assert rollback_database(path) == {"before": "C", "after": "A"}
