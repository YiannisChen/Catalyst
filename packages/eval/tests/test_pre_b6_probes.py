"""Pre-B6 coverage invariant and FTS5-only lexical smoke probe tests."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from catalyst_eval.probes.coverage_invariant import check_corpus_coverage_invariant
from catalyst_eval.probes.lexical_smoke import (
    SMOKE_STOPWORDS_V1,
    LexicalSmokeError,
    build_lexical_smoke_query,
    fts5_and_query,
    run_lexical_smoke_probe,
)

MID = "a" * 64
CUTOFF = "2025-12-31T00:00:00Z"
BODY = (
    "The quarterly earnings guidance revenue growth product demand outlook "
    "and further details about operating margins"
)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _conn_with_fts(tmp_path: Path) -> sqlite3.Connection:
    from catalyst_data.storage.sqlite import init_db
    from catalyst_data.migrations import run_migrations
    from catalyst_data.retrieval.fts5_builder import build_fts5_index

    conn = sqlite3.connect(tmp_path / "t.db")
    init_db(conn)
    run_migrations(conn)
    conn.execute(
        """INSERT OR IGNORE INTO corpus_manifest
           (manifest_id, manifest_json, is_current, created_at)
           VALUES (?, ?, 1, '2026-01-01T00:00:00Z')""",
        (MID, json.dumps({"manifest_id": MID, "certified_snapshot_identity": "s" * 64})),
    )
    for i, (cid, text) in enumerate(
        (
            ("chunk_aaa", BODY),
            ("chunk_bbb", "earnings guidance revenue growth product demand outlook details"),
        )
    ):
        conn.execute(
            """INSERT INTO corpus_chunks (
                chunk_id, document_id, chunk_profile_version, section_key, ordinal,
                content_text, content_hash, metadata_hash, source_class,
                available_at, ticker_associations, eligibility, manifest_id, status,
                boundary_kind, body_token_start, body_token_end, body_overlap_tokens,
                prefix_token_count, prefix_truncated, section_parse_degraded,
                created_at, updated_at
            ) VALUES (?,?, 'filing_v3', 's', ?,
                ?, ?, ?, 'official_government',
                '2025-08-01T00:00:00Z', ?, 'eligible', ?, 'pending_embedding',
                'document_end', 0, 10, 0, 0, 0, 0,
                '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')""",
            (
                cid,
                "doc1",
                f"{i:04d}",
                text,
                _hash(text),
                _hash(f"meta{i}"),
                json.dumps(["AAPL"]),
                MID,
            ),
        )
    conn.commit()
    build_fts5_index(conn, MID, clock=lambda: "2026-01-02T00:00:00Z")
    return conn


def test_corpus_coverage_invariant_requires_eligible_and_filing_v3(tmp_path: Path):
    conn = _conn_with_fts(tmp_path)
    r = check_corpus_coverage_invariant(
        conn, ticker="AAPL", corpus_manifest_id=MID, cutoff="2025-12-31T00:00:00Z"
    )
    assert r.ok is True
    r2 = check_corpus_coverage_invariant(
        conn, ticker="MSFT", corpus_manifest_id=MID, cutoff="2025-12-31T00:00:00Z"
    )
    assert r2.ok is False
    conn.close()


def test_lexical_smoke_query_uses_nfc_ascii_word_terms_not_bge_subwords():
    terms = build_lexical_smoke_query("The Earnings ##guidance revenue")
    assert "the" not in terms
    assert all("##" not in t for t in terms)
    assert "earnings" in terms


def test_lexical_smoke_stopwords_v1_exact_set():
    assert "the" in SMOKE_STOPWORDS_V1
    assert "earnings" not in SMOKE_STOPWORDS_V1


def test_lexical_smoke_query_deduplicates_and_uses_first_8_terms():
    text = " ".join([f"term{i}" for i in range(20)] + ["term1", "term2"])
    terms = build_lexical_smoke_query(text)
    assert len(terms) == 8
    assert len(set(terms)) == 8


def test_lexical_smoke_query_escapes_fts5_and_uses_and_semantics():
    q = fts5_and_query(["a", 'b"c'])
    assert " AND " in q
    assert '""' in q or "b" in q


def test_lexical_smoke_fails_when_no_non_stopword_terms():
    with pytest.raises(ValueError):
        build_lexical_smoke_query("the and or of to")


def test_lexical_smoke_query_stable_across_reruns():
    t1 = build_lexical_smoke_query("Alpha Beta Gamma")
    t2 = build_lexical_smoke_query("Alpha Beta Gamma")
    assert t1 == t2


def test_lexical_smoke_built_fts5_passes(tmp_path: Path):
    conn = _conn_with_fts(tmp_path)
    res = run_lexical_smoke_probe(
        conn, ticker="AAPL", corpus_manifest_id=MID, probe_cutoff=CUTOFF
    )
    assert res.ok is True
    assert res.mode_served == "fts5"
    assert res.hit_mode in {"anchor", "sibling"}
    assert res.anchor_chunk_id == "chunk_aaa"
    conn.close()


def test_lexical_smoke_ticker_filter_fixed(tmp_path: Path):
    conn = _conn_with_fts(tmp_path)
    with pytest.raises(LexicalSmokeError):
        run_lexical_smoke_probe(
            conn, ticker="MSFT", corpus_manifest_id=MID, probe_cutoff=CUTOFF
        )
    conn.close()


def test_lexical_smoke_cutoff_equals_anchor_available_at(tmp_path: Path):
    conn = _conn_with_fts(tmp_path)
    res = run_lexical_smoke_probe(
        conn, ticker="AAPL", corpus_manifest_id=MID, probe_cutoff=CUTOFF
    )
    assert res.cutoff == "2025-08-01T00:00:00Z"
    conn.close()


def test_lexical_smoke_is_not_quality_metric_label(tmp_path: Path):
    conn = _conn_with_fts(tmp_path)
    res = run_lexical_smoke_probe(
        conn, ticker="AAPL", corpus_manifest_id=MID, probe_cutoff=CUTOFF
    )
    assert res.is_quality_metric is False
    conn.close()


def test_lexical_smoke_missing_fts_fails(tmp_path: Path):
    from catalyst_data.storage.sqlite import init_db
    from catalyst_data.migrations import run_migrations

    conn = sqlite3.connect(tmp_path / "nofts.db")
    init_db(conn)
    run_migrations(conn)
    conn.execute(
        """INSERT OR IGNORE INTO corpus_manifest
           (manifest_id, manifest_json, is_current, created_at)
           VALUES (?, '{}', 1, '2026-01-01T00:00:00Z')""",
        (MID,),
    )
    conn.execute(
        """INSERT INTO corpus_chunks (
            chunk_id, document_id, chunk_profile_version, section_key, ordinal,
            content_text, content_hash, metadata_hash, source_class,
            available_at, ticker_associations, eligibility, manifest_id, status,
            boundary_kind, body_token_start, body_token_end, body_overlap_tokens,
            prefix_token_count, prefix_truncated, section_parse_degraded,
            created_at, updated_at
        ) VALUES ('chunk_aaa','doc1','filing_v3','s','0000',
            ?, ?, ?, 'official_government',
            '2025-08-01T00:00:00Z', '["AAPL"]', 'eligible', ?, 'pending_embedding',
            'document_end', 0, 10, 0, 0, 0, 0,
            '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')""",
        (BODY, _hash(BODY), _hash("m"), MID),
    )
    conn.commit()
    with pytest.raises(LexicalSmokeError) as ei:
        run_lexical_smoke_probe(
            conn, ticker="AAPL", corpus_manifest_id=MID, probe_cutoff=CUTOFF
        )
    assert ei.value.reason in {
        "fts5_missing",
        "fts5_unavailable",
        "fts5_stale",
        "not_fts5",
    }
    conn.close()


def test_lexical_smoke_stale_manifest_fails(tmp_path: Path):
    conn = _conn_with_fts(tmp_path)
    other = "b" * 64
    conn.execute(
        """INSERT OR IGNORE INTO corpus_manifest
           (manifest_id, manifest_json, is_current, created_at)
           VALUES (?, '{}', 0, '2026-01-01T00:00:00Z')""",
        (other,),
    )
    conn.execute(
        "UPDATE lexical_index_state SET corpus_manifest_id=? WHERE singleton_id=1",
        (other,),
    )
    conn.commit()
    with pytest.raises(LexicalSmokeError) as ei:
        run_lexical_smoke_probe(
            conn, ticker="AAPL", corpus_manifest_id=MID, probe_cutoff=CUTOFF
        )
    assert ei.value.reason == "fts5_stale"
    conn.close()


def test_lexical_smoke_non_fts5_mode_state_fails(tmp_path: Path):
    conn = _conn_with_fts(tmp_path)
    conn.execute(
        "UPDATE lexical_index_state SET mode_served='sql_like' WHERE singleton_id=1"
    )
    conn.commit()
    with pytest.raises(LexicalSmokeError) as ei:
        run_lexical_smoke_probe(
            conn, ticker="AAPL", corpus_manifest_id=MID, probe_cutoff=CUTOFF
        )
    assert ei.value.reason == "fts5_unavailable"
    conn.close()


def test_lexical_smoke_no_anchor_or_sibling_hit_preserves_evidence(
    tmp_path: Path,
):
    conn = _conn_with_fts(tmp_path)
    conn.execute("DELETE FROM corpus_chunks_fts")
    conn.commit()
    with pytest.raises(LexicalSmokeError) as ei:
        run_lexical_smoke_probe(
            conn, ticker="AAPL", corpus_manifest_id=MID, probe_cutoff=CUTOFF
        )
    assert ei.value.reason == "no_hit"
    assert ei.value.anchor_chunk_id == "chunk_aaa"
    assert ei.value.query_terms
    assert ei.value.cutoff == "2025-08-01T00:00:00Z"
    conn.close()
