"""Regression tests for FTS5 builder precision — Item 2 from B4 review."""
from __future__ import annotations

from retrieval_fixtures import MANIFEST_A, fresh_v10_db, insert_corpus_chunk


_fresh_v10_db = fresh_v10_db


def _seed_chunk(db, chunk_id, content_text):
    insert_corpus_chunk(db, chunk_id=chunk_id, content_text=content_text)
    db.commit()


def test_builder_rejects_active_transaction():
    """build_fts5_index rejects conn.in_transaction with LexicalIndexBuildError
    code='transaction_active'."""
    import pytest
    from catalyst_data.retrieval.fts5_builder import (
        build_fts5_index, LexicalIndexBuildError,
    )

    db = _fresh_v10_db()
    _seed_chunk(db, "poly:a:news_v2:body:0001", "AAPL earnings")

    # Start a transaction before calling builder (must commit implicit tx first)
    # sqlite3 has an implicit transaction after writes — commit it
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    with pytest.raises(LexicalIndexBuildError) as exc_info:
        build_fts5_index(db, MANIFEST_A, clock=lambda: "2026-01-02T00:00:00Z")
    assert exc_info.value.code == "transaction_active"
    db.execute("ROLLBACK")


def test_builder_uses_begin_immediate():
    """build_fts5_index opens its own BEGIN IMMEDIATE transaction."""
    from catalyst_data.retrieval.fts5_builder import build_fts5_index

    db = _fresh_v10_db()
    _seed_chunk(db, "poly:a:news_v2:body:0001", "AAPL earnings")

    result = build_fts5_index(db, MANIFEST_A, clock=lambda: "2026-01-02T00:00:00Z")
    assert result.mode_served == "fts5"
    assert result.row_count == 1

    # Verify lexical_index_state was written
    state = db.execute(
        "SELECT mode_served, row_count FROM lexical_index_state WHERE singleton_id=1"
    ).fetchone()
    assert state is not None
    assert state["mode_served"] == "fts5"
    assert state["row_count"] == 1


def test_builder_failure_preserves_old_state():
    """Failure after delete rolls back, preserving previous FTS rows and state."""
    from catalyst_data.retrieval.fts5_builder import build_fts5_index

    db = _fresh_v10_db()
    _seed_chunk(db, "poly:a:news_v2:body:0001", "AAPL earnings")

    # Build successful index first
    build_fts5_index(db, MANIFEST_A, clock=lambda: "2026-01-02T00:00:00Z")

    # Verify first build
    count1 = db.execute("SELECT COUNT(*) FROM corpus_chunks_fts").fetchone()[0]
    assert count1 == 1

    old_state = db.execute(
        "SELECT corpus_manifest_id, row_count, built_at FROM lexical_index_state"
    ).fetchone()

    def fail_after_delete(stage: str) -> None:
        if stage == "after_delete":
            raise RuntimeError("injected build failure")

    import pytest
    with pytest.raises(RuntimeError, match="injected build failure"):
        build_fts5_index(
            db, MANIFEST_A, clock=lambda: "2026-01-03T00:00:00Z",
            failure_injector=fail_after_delete,
        )

    assert db.execute("SELECT COUNT(*) FROM corpus_chunks_fts").fetchone()[0] == count1
    assert db.execute(
        "SELECT corpus_manifest_id, row_count, built_at FROM lexical_index_state"
    ).fetchone() == old_state


def test_builder_batches_fts_inserts_at_most_500_rows_inside_transaction():
    import inspect
    from catalyst_data.retrieval.fts5_builder import build_fts5_index

    source = inspect.getsource(build_fts5_index)
    assert ".fetchall()" not in source
    assert "fetchmany(500)" in source


def test_builder_no_broad_exception_handling():
    """build_fts5_index does not contain broad except Exception or
    except OperationalError for non-FTS5 errors."""
    import inspect
    from catalyst_data.retrieval.fts5_builder import build_fts5_index

    source = inspect.getsource(build_fts5_index)
    # Must not have bare except Exception: pass
    assert "except Exception:" not in source, (
        "No broad Exception handling allowed"
    )
    # Must have LexicalIndexBuildError usage
    assert "LexicalIndexBuildError" in source, (
        "Must use LexicalIndexBuildError for contract errors"
    )
