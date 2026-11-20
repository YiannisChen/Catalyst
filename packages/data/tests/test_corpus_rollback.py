from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from catalyst_data.corpus.streaming_publication import (
    ensure_streaming_publication_schema,
)
from catalyst_data.storage.sqlite import init_db


def _published_build(conn: sqlite3.Connection, build_id: str, manifest_id: str,
                     published_at: str, *, current: bool) -> None:
    manifest_json = json.dumps({"build_id": build_id})
    conn.execute(
        "INSERT INTO corpus_manifest(manifest_id,manifest_json,is_current,created_at) VALUES (?,?,?,?)",
        (manifest_id, manifest_json, int(current), published_at),
    )
    conn.execute(
        """INSERT INTO corpus_publication_builds
           (build_id,certified_snapshot_identity,header_json,status,manifest_id,manifest_json,
            document_count,chunk_count,inventory_digest,lexical_expected_digest,lexical_digest,
            lexical_row_count,lexical_ready,created_at,updated_at,published_at)
           VALUES (?,?,?,'published',?,?,1,1,?,?,?,1,1,?,?,?)""",
        (build_id, "c" * 64, json.dumps({"normalization_version": "1.0.0"}), manifest_id,
         manifest_json, "d" * 64, "e" * 64, "e" * 64, published_at, published_at, published_at),
    )
    conn.execute(
        "INSERT INTO corpus_build_chunks_fts(build_id,chunk_id,content_text) VALUES (?,?,?)",
        (build_id, f"{build_id}:chunk", "rollback fixture"),
    )


def _corpus(path: Path) -> None:
    conn = sqlite3.connect(path)
    init_db(conn)
    ensure_streaming_publication_schema(conn)
    _published_build(conn, "previous", "a" * 64, "2026-11-01T00:00:00Z", current=False)
    _published_build(conn, "current", "b" * 64, "2026-11-02T00:00:00Z", current=True)
    conn.execute(
        """INSERT INTO lexical_index_state
           (singleton_id,schema_version,corpus_manifest_id,mode_served,row_count,built_at,
            lexical_generation_id,lexical_digest)
           VALUES (1,'1.0.0',?,'fts5',1,'2026-11-02T00:00:00Z','current',?)""",
        ("b" * 64, "e" * 64),
    )
    conn.commit()
    conn.close()


def test_rollback_selects_previous_published_build_without_deleting_builds(tmp_path):
    from catalyst_data.corpus.rollback import rollback_database

    path = tmp_path / "corpus.sqlite3"
    _corpus(path)

    result = rollback_database(path)

    assert result == {"before": "current", "after": "previous"}
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT manifest_id FROM corpus_manifest WHERE is_current=1").fetchone()[0] == "a" * 64
    assert conn.execute("SELECT corpus_manifest_id,lexical_generation_id FROM lexical_index_state").fetchone() == (
        "a" * 64, "previous")
    assert conn.execute("SELECT COUNT(*) FROM corpus_publication_builds").fetchone()[0] == 2
    assert conn.execute("SELECT status FROM corpus_publication_builds WHERE build_id='current'").fetchone()[0] == "published"
    conn.close()


def test_rollback_cli_prints_before_and_after_build_ids(tmp_path, capsys):
    from catalyst_data.corpus.rollback import main

    path = tmp_path / "corpus.sqlite3"
    _corpus(path)

    assert main(["--db", str(path)]) == 0
    output = capsys.readouterr().out
    assert "current_build_before=current" in output
    assert "current_build_after=previous" in output


def test_rollback_rejects_non_published_target_without_changing_current(tmp_path):
    from catalyst_data.corpus.rollback import rollback_database

    path = tmp_path / "corpus.sqlite3"
    _corpus(path)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE corpus_publication_builds SET status='staging' WHERE build_id='previous'")
    conn.commit()
    conn.close()

    with pytest.raises(ValueError, match="published"):
        rollback_database(path, to_build_id="previous")
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT manifest_id FROM corpus_manifest WHERE is_current=1").fetchone()[0] == "b" * 64
    conn.close()


def test_rollback_compare_and_swap_rejects_changed_current_without_switching(tmp_path):
    from catalyst_data.corpus.rollback import rollback_database

    path = tmp_path / "corpus.sqlite3"
    _corpus(path)

    with pytest.raises(ValueError, match="current build changed"):
        rollback_database(path, to_build_id="previous", expected_current_build_id="another-build")

    conn = sqlite3.connect(path)
    assert conn.execute("SELECT manifest_id FROM corpus_manifest WHERE is_current=1").fetchone()[0] == "b" * 64
    conn.close()
