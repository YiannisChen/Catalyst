"""Source bundle export tests."""

from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3
from pathlib import Path

import pytest

from catalyst_data.retrieval.source_bundle import (
    export_source_bundle as production_export_source_bundle,
)
from catalyst_data.manifests.universe import RATIFIED_TICKERS
from catalyst_data.pre_b6_probes import (
    PROBE_POLICY_REVISION,
    PROBE_REPORT_SCHEMA_VERSION,
    compute_probe_report_id,
)
import catalyst_data.retrieval.source_bundle as source_bundle_module

UNIVERSE_MANIFEST_ID = "6" * 64
PROBE_CUTOFF = "2025-12-31T00:00:00Z"


def _ch(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _mh(seed: str = "meta") -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _seed_manifest(conn, mid: str, snapshot_id: str, *, is_current: int = 1):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS corpus_manifest (
            manifest_id TEXT PRIMARY KEY, manifest_json TEXT, is_current INTEGER, created_at TEXT
        )"""
    )
    conn.execute(
        "INSERT OR REPLACE INTO corpus_manifest VALUES (?,?,?,?)",
        (
            mid,
            json.dumps({"certified_snapshot_identity": snapshot_id, "manifest_id": mid}),
            is_current,
            "2026-01-01T00:00:00Z",
        ),
    )


def _seed_lex(conn, mid: str, mode: str = "fts5"):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS lexical_index_state (
            singleton_id INTEGER PRIMARY KEY, corpus_manifest_id TEXT, mode_served TEXT,
            row_count INTEGER, built_at TEXT
        )"""
    )
    conn.execute(
        "INSERT OR REPLACE INTO lexical_index_state VALUES (1,?,?,1,?)",
        (mid, mode, "2026-01-02T00:00:00Z"),
    )


def _write_probe_report(
    path: Path,
    *,
    mid: str,
    snapshot_id: str,
    universe_manifest_id: str = "6" * 64,
    postbuild_readiness_id: str = "4" * 64,
) -> Path:
    coverage = [
        {
            "ticker": ticker,
            "ok": True,
            "eligible_count": 1,
            "filing_v3_count": 1,
            "future_count": 0,
            "reason": None,
        }
        for ticker in RATIFIED_TICKERS
    ]
    lexical = [
        {
            "ticker": ticker,
            "ok": True,
            "query_terms": ["earnings"],
            "anchor_chunk_id": f"anchor_{ticker}",
            "anchor_document_id": f"document_{ticker}",
            "cutoff": "2025-08-01T00:00:00Z",
            "hit_mode": "anchor",
            "mode_served": "fts5",
            "failure_reason": None,
            "is_quality_metric": False,
        }
        for ticker in RATIFIED_TICKERS
    ]
    body = {
        "schema_version": PROBE_REPORT_SCHEMA_VERSION,
        "policy_revision": PROBE_POLICY_REVISION,
        "universe_manifest_id": universe_manifest_id,
        "snapshot_id": snapshot_id,
        "corpus_manifest_id": mid,
        "postbuild_readiness_id": postbuild_readiness_id,
        "ordered_tickers": list(RATIFIED_TICKERS),
        "probe_cutoff": "2025-12-31T00:00:00Z",
        "coverage_results": coverage,
        "lexical_results": lexical,
        "coverage_pass_count": 40,
        "lexical_pass_count": 40,
        "overall_pass": True,
    }
    body["probe_report_id"] = compute_probe_report_id(body)
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def _write_postbuild_readiness_report(
    conn: sqlite3.Connection,
    path: Path,
    *,
    mid: str,
    snapshot_id: str,
    universe_manifest_id: str = UNIVERSE_MANIFEST_ID,
) -> Path:
    from catalyst_data.manifests.universe import sha256_identity

    columns = {row[1] for row in conn.execute("PRAGMA table_info(corpus_chunks)")}
    if "eligibility" not in columns:
        conn.execute(
            "ALTER TABLE corpus_chunks ADD COLUMN eligibility TEXT DEFAULT 'eligible'"
        )
    document_ids = [
        str(row[0])
        for row in conn.execute(
            """SELECT DISTINCT document_id FROM corpus_chunks
               WHERE manifest_id=? AND chunk_profile_version='filing_v3'
               ORDER BY document_id""",
            (mid,),
        )
        if row[0]
    ]
    readiness = {
        "schema_version": "pre_b6_postbuild_readiness_v1",
        "postbuild_evidence_ready": True,
        "universe_manifest_id": universe_manifest_id,
        "inventory_id": "1" * 64,
        "source_snapshot_id": "2" * 64,
        "snapshot_id": snapshot_id,
        "corpus_manifest_id": mid,
        "lexical_manifest_id": mid,
        "b2o_terminal_run_id": "b2o",
        "s1_terminal_run_id": "s1",
        "s2_terminal_run_id": "s2",
        "s4_terminal_run_id": "s4",
        "mandatory_document_ids": document_ids or ["missing_document"],
        "expected_mandatory_count": len(document_ids) or 1,
        "chunked_count": len(document_ids) or 1,
    }
    readiness["postbuild_readiness_id"] = sha256_identity(readiness)
    binding = {
        key: readiness[key]
        for key in (
            "b2o_terminal_run_id", "s1_terminal_run_id", "s2_terminal_run_id",
            "s4_terminal_run_id", "inventory_id", "universe_manifest_id",
            "source_snapshot_id",
        )
    }
    path.write_text(
        json.dumps({
            "postbuild_readiness": readiness,
            "b2o_readiness": {"readiness_binding": binding},
        }),
        encoding="utf-8",
    )
    conn.commit()
    return path


def _can_build_real_probe(
    conn: sqlite3.Connection, *, mid: str, snapshot_id: str
) -> bool:
    try:
        manifest = conn.execute(
            "SELECT manifest_json, is_current FROM corpus_manifest WHERE manifest_id=?",
            (mid,),
        ).fetchone()
        lexical = conn.execute(
            """SELECT corpus_manifest_id, mode_served FROM lexical_index_state
               WHERE singleton_id=1"""
        ).fetchone()
        filing_count = conn.execute(
            """SELECT COUNT(*) FROM corpus_chunks
               WHERE manifest_id=? AND chunk_profile_version='filing_v3'""",
            (mid,),
        ).fetchone()[0]
    except sqlite3.Error:
        return False
    if manifest is None or int(manifest[1] or 0) != 1:
        return False
    try:
        certified = json.loads(manifest[0] or "{}").get(
            "certified_snapshot_identity"
        )
    except json.JSONDecodeError:
        return False
    return (
        certified == snapshot_id
        and lexical == (mid, "fts5")
        and int(filing_count) > 0
        and len(snapshot_id) == 64
        and all(ch in "0123456789abcdef" for ch in snapshot_id)
    )


def _write_real_probe_report(
    conn: sqlite3.Connection,
    path: Path,
    *,
    mid: str,
    snapshot_id: str,
    postbuild_readiness_path: Path,
) -> Path:
    from catalyst_data.pre_b6_probes import run_pre_b6_probe_gates

    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(corpus_chunks)")
    }
    if "eligibility" not in columns:
        conn.execute(
            "ALTER TABLE corpus_chunks ADD COLUMN eligibility TEXT DEFAULT 'eligible'"
        )
    if "source_class" not in columns:
        conn.execute(
            "ALTER TABLE corpus_chunks ADD COLUMN source_class TEXT "
            "DEFAULT 'official_government'"
        )
    lexical_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(lexical_index_state)")
    }
    if "fallback_reason" not in lexical_columns:
        conn.execute(
            "ALTER TABLE lexical_index_state ADD COLUMN fallback_reason TEXT"
        )
    conn.execute(
        """CREATE VIRTUAL TABLE IF NOT EXISTS corpus_chunks_fts
           USING fts5(manifest_id UNINDEXED, chunk_id UNINDEXED, content_text)"""
    )
    content = (
        "quarterly earnings guidance revenue growth product demand outlook "
        "operating margin"
    )
    for ordinal, ticker in enumerate(RATIFIED_TICKERS):
        chunk_id = f"cert_{ordinal:02d}"
        if conn.execute(
            "SELECT 1 FROM corpus_chunks WHERE chunk_id=?", (chunk_id,)
        ).fetchone():
            continue
        conn.execute(
            """INSERT INTO corpus_chunks (
                chunk_id, document_id, content_text, content_hash, metadata_hash,
                available_at, ticker_associations, manifest_id,
                chunk_profile_version, status, eligibility, source_class
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'filing_v3',
                      'pending_embedding', 'eligible', 'official_government')""",
            (
                chunk_id,
                f"cert_document_{ordinal:02d}",
                content,
                _ch(content),
                _mh(f"cert-{ordinal}"),
                "2025-08-01T00:00:00Z",
                json.dumps([ticker]),
                mid,
            ),
        )
    conn.execute("DELETE FROM corpus_chunks_fts")
    conn.execute(
        """INSERT INTO corpus_chunks_fts (manifest_id, chunk_id, content_text)
           SELECT manifest_id, chunk_id, content_text FROM corpus_chunks
           WHERE manifest_id=? AND eligibility='eligible'""",
        (mid,),
    )
    conn.execute(
        """UPDATE lexical_index_state
           SET corpus_manifest_id=?, mode_served='fts5', fallback_reason=NULL
           WHERE singleton_id=1""",
        (mid,),
    )
    conn.commit()
    readiness_path = _write_postbuild_readiness_report(
        conn,
        postbuild_readiness_path,
        mid=mid,
        snapshot_id=snapshot_id,
    )
    run_pre_b6_probe_gates(
        conn,
        universe_manifest_id=UNIVERSE_MANIFEST_ID,
        snapshot_id=snapshot_id,
        corpus_manifest_id=mid,
        ordered_tickers=list(RATIFIED_TICKERS),
        probe_cutoff=PROBE_CUTOFF,
        output_path=path,
        postbuild_readiness_report_path=readiness_path,
    )
    return path


def export_source_bundle(
    conn: sqlite3.Connection,
    *,
    corpus_manifest_id: str,
    snapshot_id: str,
    output_dir: Path,
) -> str:
    """Test convenience wrapper; production API still requires explicit proof."""
    output_dir = Path(output_dir)
    proof_path = (
        output_dir.parent
        / f"probe_{corpus_manifest_id[:8]}_{snapshot_id[:8]}.json"
    )
    readiness_path = (
        output_dir.parent
        / f"postbuild_{corpus_manifest_id[:8]}_{snapshot_id[:8]}.json"
    )
    if _can_build_real_probe(
        conn, mid=corpus_manifest_id, snapshot_id=snapshot_id
    ):
        proof = _write_real_probe_report(
            conn,
            proof_path,
            mid=corpus_manifest_id,
            snapshot_id=snapshot_id,
            postbuild_readiness_path=readiness_path,
        )
    else:
        readiness_path = _write_postbuild_readiness_report(
            conn,
            readiness_path,
            mid=corpus_manifest_id,
            snapshot_id=snapshot_id,
        )
        readiness_id = json.loads(readiness_path.read_text(encoding="utf-8"))[
            "postbuild_readiness"
        ]["postbuild_readiness_id"]
        proof = _write_probe_report(
            proof_path,
            mid=corpus_manifest_id,
            snapshot_id=snapshot_id,
            postbuild_readiness_id=readiness_id,
        )
    return production_export_source_bundle(
        conn,
        universe_manifest_id=UNIVERSE_MANIFEST_ID,
        corpus_manifest_id=corpus_manifest_id,
        snapshot_id=snapshot_id,
        probe_cutoff=PROBE_CUTOFF,
        output_dir=output_dir,
        probe_report_path=proof,
        postbuild_readiness_report_path=readiness_path,
    )


def test_source_bundle_api_requires_certified_probe_report() -> None:
    assert "probe_report_path" in inspect.signature(
        production_export_source_bundle
    ).parameters
    assert "postbuild_readiness_report_path" in inspect.signature(
        production_export_source_bundle
    ).parameters


@pytest.mark.parametrize(
    ("ready", "universe_manifest_id", "message"),
    [
        (False, UNIVERSE_MANIFEST_ID, "postbuild_evidence_ready"),
        (True, "7" * 64, "universe_manifest_id"),
    ],
)
def test_source_bundle_rejects_false_or_mismatched_postbuild_readiness(
    tmp_path: Path,
    ready: bool,
    universe_manifest_id: str,
    message: str,
) -> None:
    from catalyst_data.manifests.universe import sha256_identity

    conn = sqlite3.connect(":memory:")
    mid, snap = "a" * 64, "5" * 64
    readiness = {
        "schema_version": "pre_b6_postbuild_readiness_v1",
        "postbuild_evidence_ready": ready,
        "universe_manifest_id": universe_manifest_id,
        "inventory_id": "1" * 64,
        "source_snapshot_id": "s" * 64,
        "snapshot_id": snap,
        "corpus_manifest_id": mid,
        "lexical_manifest_id": mid,
        "b2o_terminal_run_id": "b2o",
        "s1_terminal_run_id": "s1",
        "s2_terminal_run_id": "s2",
        "s4_terminal_run_id": "s4",
        "mandatory_document_ids": ["document_00"],
        "expected_mandatory_count": 1,
        "chunked_count": 1 if ready else 0,
    }
    readiness["postbuild_readiness_id"] = sha256_identity(readiness)
    readiness_path = tmp_path / "postbuild.json"
    readiness_path.write_text(
        json.dumps({"postbuild_readiness": readiness}), encoding="utf-8"
    )

    with pytest.raises(ValueError, match=message):
        production_export_source_bundle(
            conn,
            universe_manifest_id=UNIVERSE_MANIFEST_ID,
            corpus_manifest_id=mid,
            snapshot_id=snap,
            probe_cutoff=PROBE_CUTOFF,
            output_dir=tmp_path / "bundle",
            probe_report_path=tmp_path / "probe.json",
            postbuild_readiness_report_path=readiness_path,
        )
    assert not (tmp_path / "bundle").exists()
    conn.close()


def test_source_bundle_rejects_forged_probe_report(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE corpus_chunks (
            chunk_id TEXT, document_id TEXT, content_text TEXT, content_hash TEXT,
            metadata_hash TEXT, available_at TEXT, ticker_associations TEXT,
            manifest_id TEXT, chunk_profile_version TEXT, status TEXT
        )"""
    )
    mid, snap = "a" * 64, "5" * 64
    _seed_manifest(conn, mid, snap)
    _seed_lex(conn, mid)
    body = "filing body"
    conn.execute(
        "INSERT INTO corpus_chunks VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "c1",
            "d",
            body,
            _ch(body),
            _mh(),
            "2025-08-01T00:00:00Z",
            '["AAPL"]',
            mid,
            "filing_v3",
            "active",
        ),
    )
    readiness = _write_postbuild_readiness_report(
        conn, tmp_path / "postbuild.json", mid=mid, snapshot_id=snap
    )
    readiness_id = json.loads(readiness.read_text(encoding="utf-8"))[
        "postbuild_readiness"
    ]["postbuild_readiness_id"]
    proof = _write_probe_report(
        tmp_path / "probe.json",
        mid=mid,
        snapshot_id=snap,
        postbuild_readiness_id=readiness_id,
    )
    report = json.loads(proof.read_text(encoding="utf-8"))
    report["probe_report_id"] = "0" * 64
    proof.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError, match="identity"):
        production_export_source_bundle(
            conn,
            universe_manifest_id=UNIVERSE_MANIFEST_ID,
            corpus_manifest_id=mid,
            snapshot_id=snap,
            probe_cutoff=PROBE_CUTOFF,
            output_dir=tmp_path / "bundle",
            probe_report_path=proof,
            postbuild_readiness_report_path=readiness,
        )
    conn.close()


def test_source_bundle_rejects_self_consistent_forged_40_of_40_report(
    tmp_path: Path,
) -> None:
    """A public-formula hash cannot certify evidence absent from the live DB."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE corpus_chunks (
            chunk_id TEXT, document_id TEXT, content_text TEXT, content_hash TEXT,
            metadata_hash TEXT, available_at TEXT, ticker_associations TEXT,
            manifest_id TEXT, chunk_profile_version TEXT, status TEXT
        )"""
    )
    mid, snap = "8" * 64, "9" * 64
    _seed_manifest(conn, mid, snap)
    _seed_lex(conn, mid)
    body = "quarterly earnings"
    conn.execute(
        "INSERT INTO corpus_chunks VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "only_aapl",
            "document_aapl",
            body,
            _ch(body),
            _mh(),
            "2025-08-01T00:00:00Z",
            '["AAPL"]',
            mid,
            "filing_v3",
            "active",
        ),
    )
    readiness = _write_postbuild_readiness_report(
        conn, tmp_path / "postbuild.json", mid=mid, snapshot_id=snap
    )
    readiness_id = json.loads(readiness.read_text(encoding="utf-8"))[
        "postbuild_readiness"
    ]["postbuild_readiness_id"]
    proof = _write_probe_report(
        tmp_path / "self_consistent_forgery.json",
        mid=mid,
        snapshot_id=snap,
        postbuild_readiness_id=readiness_id,
    )

    with pytest.raises(ValueError, match="DB-backed"):
        production_export_source_bundle(
            conn,
            universe_manifest_id=UNIVERSE_MANIFEST_ID,
            corpus_manifest_id=mid,
            snapshot_id=snap,
            probe_cutoff=PROBE_CUTOFF,
            output_dir=tmp_path / "bundle",
            probe_report_path=proof,
            postbuild_readiness_report_path=readiness,
        )
    assert not (tmp_path / "bundle").exists()
    conn.close()


def test_export_sorted_excludes_tombstones(tmp_path: Path):
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE corpus_chunks (
            chunk_id TEXT PRIMARY KEY, document_id TEXT, content_text TEXT,
            content_hash TEXT, metadata_hash TEXT, available_at TEXT,
            ticker_associations TEXT, manifest_id TEXT, chunk_profile_version TEXT,
            status TEXT
        );
        CREATE TABLE corpus_tombstones (chunk_id TEXT PRIMARY KEY);
        """
    )
    mid = "a" * 64
    sid = "d" * 64
    _seed_manifest(conn, mid, sid)
    _seed_lex(conn, mid)
    for cid, text in (("chunk_b", "body_b"), ("chunk_a", "body_a")):
        conn.execute(
            """INSERT INTO corpus_chunks VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                cid, "doc", text, _ch(text), _mh(cid),
                "2025-08-01T00:00:00Z", '["AAPL"]', mid, "filing_v3", "pending_embedding",
            ),
        )
    conn.execute("INSERT INTO corpus_tombstones VALUES ('chunk_b')")
    bid = export_source_bundle(
        conn, corpus_manifest_id=mid, snapshot_id=sid, output_dir=tmp_path,
    )
    text = (tmp_path / f"source_{bid}" / "chunks.jsonl").read_text()
    assert "chunk_a" in text
    assert "chunk_b" not in text
    conn.close()


def test_source_bundle_id_formula(tmp_path: Path):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE corpus_chunks (
            chunk_id TEXT, document_id TEXT, content_text TEXT, content_hash TEXT,
            metadata_hash TEXT, available_at TEXT, ticker_associations TEXT,
            manifest_id TEXT, chunk_profile_version TEXT, status TEXT
        )"""
    )
    mid, snap = "b" * 64, "d" * 64
    _seed_manifest(conn, mid, snap)
    _seed_lex(conn, mid)
    body = "hello"
    conn.execute(
        "INSERT INTO corpus_chunks VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("c1", "d", body, _ch(body), _mh(), "2025-08-01T00:00:00Z", "[]", mid, "filing_v3", "active"),
    )
    s1 = export_source_bundle(
        conn, corpus_manifest_id=mid, snapshot_id=snap, output_dir=tmp_path / "a",
    )
    s2 = export_source_bundle(
        conn, corpus_manifest_id=mid, snapshot_id=snap, output_dir=tmp_path / "b",
    )
    assert s1 == s2
    conn.close()


def test_export_contains_no_vectors_field(tmp_path: Path):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE corpus_chunks (
            chunk_id TEXT, document_id TEXT, content_text TEXT, content_hash TEXT,
            metadata_hash TEXT, available_at TEXT, ticker_associations TEXT,
            manifest_id TEXT, chunk_profile_version TEXT, status TEXT
        )"""
    )
    mid, snap = "c" * 64, "d" * 64
    _seed_manifest(conn, mid, snap)
    _seed_lex(conn, mid)
    body = "t"
    conn.execute(
        "INSERT INTO corpus_chunks VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("c1", "d", body, _ch(body), _mh(), "2025-08-01T00:00:00Z", "[]", mid, "filing_v3", "active"),
    )
    bid = export_source_bundle(
        conn, corpus_manifest_id=mid, snapshot_id=snap, output_dir=tmp_path,
    )
    man = (tmp_path / f"source_{bid}" / "source_bundle_manifest.json").read_text()
    assert "vector" not in man
    assert "embedding" not in man
    conn.close()


def test_export_refuses_wrong_corpus_manifest(tmp_path: Path):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE corpus_chunks (
            chunk_id TEXT, document_id TEXT, content_text TEXT, content_hash TEXT,
            metadata_hash TEXT, available_at TEXT, ticker_associations TEXT,
            manifest_id TEXT, chunk_profile_version TEXT, status TEXT
        )"""
    )
    mid, snap = "a" * 64, "d" * 64
    _seed_manifest(conn, mid, snap)
    _seed_lex(conn, mid)
    body = "t"
    conn.execute(
        "INSERT INTO corpus_chunks VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("c1", "d", body, _ch(body), _mh(), "t", "[]", mid, "filing_v3", "active"),
    )
    with pytest.raises(ValueError):
        export_source_bundle(
            conn, corpus_manifest_id="f" * 64, snapshot_id=snap, output_dir=tmp_path,
        )
    conn.close()


def test_missing_corpus_manifest_table_rejects(tmp_path: Path):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE corpus_chunks (
            chunk_id TEXT, document_id TEXT, content_text TEXT, content_hash TEXT,
            metadata_hash TEXT, available_at TEXT, ticker_associations TEXT,
            manifest_id TEXT, chunk_profile_version TEXT, status TEXT
        )"""
    )
    with pytest.raises(ValueError, match="corpus_manifest"):
        export_source_bundle(
            conn, corpus_manifest_id="a" * 64, snapshot_id="d" * 64, output_dir=tmp_path
        )
    conn.close()


def test_non_current_corpus_rejects(tmp_path: Path):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE corpus_chunks (
            chunk_id TEXT, document_id TEXT, content_text TEXT, content_hash TEXT,
            metadata_hash TEXT, available_at TEXT, ticker_associations TEXT,
            manifest_id TEXT, chunk_profile_version TEXT, status TEXT
        )"""
    )
    mid, snap = "d" * 64, "d" * 64
    _seed_manifest(conn, mid, snap, is_current=0)
    body = "x"
    conn.execute(
        "INSERT INTO corpus_chunks VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("c1", "d", body, _ch(body), _mh(), "2025-08-01T00:00:00Z", "[]", mid, "filing_v3", "active"),
    )
    _seed_lex(conn, mid)
    with pytest.raises(ValueError, match="is_current"):
        export_source_bundle(
            conn, corpus_manifest_id=mid, snapshot_id=snap, output_dir=tmp_path
        )
    conn.close()


def test_empty_production_bundle_rejects(tmp_path: Path):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE corpus_chunks (
            chunk_id TEXT, document_id TEXT, content_text TEXT, content_hash TEXT,
            metadata_hash TEXT, available_at TEXT, ticker_associations TEXT,
            manifest_id TEXT, chunk_profile_version TEXT, status TEXT
        )"""
    )
    mid, snap = "e" * 64, "d" * 64
    _seed_manifest(conn, mid, snap)
    _seed_lex(conn, mid)
    with pytest.raises(ValueError, match="mandatory SEC document"):
        export_source_bundle(
            conn, corpus_manifest_id=mid, snapshot_id=snap, output_dir=tmp_path
        )
    conn.close()


def test_bundle_without_filing_v3_rejects(tmp_path: Path):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE corpus_chunks (
            chunk_id TEXT, document_id TEXT, content_text TEXT, content_hash TEXT,
            metadata_hash TEXT, available_at TEXT, ticker_associations TEXT,
            manifest_id TEXT, chunk_profile_version TEXT, status TEXT
        )"""
    )
    mid, snap = "1" * 64, "d" * 64
    _seed_manifest(conn, mid, snap)
    _seed_lex(conn, mid)
    body = "news only"
    conn.execute(
        "INSERT INTO corpus_chunks VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("c1", "d", body, _ch(body), _mh(), "2025-08-01T00:00:00Z", "[]", mid, "news_v2", "active"),
    )
    with pytest.raises(ValueError, match="filing_v3"):
        export_source_bundle(
            conn, corpus_manifest_id=mid, snapshot_id=snap, output_dir=tmp_path
        )
    conn.close()


def test_stale_non_fts_lexical_rejects(tmp_path: Path):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE corpus_chunks (
            chunk_id TEXT, document_id TEXT, content_text TEXT, content_hash TEXT,
            metadata_hash TEXT, available_at TEXT, ticker_associations TEXT,
            manifest_id TEXT, chunk_profile_version TEXT, status TEXT
        )"""
    )
    mid, snap = "2" * 64, "d" * 64
    _seed_manifest(conn, mid, snap)
    _seed_lex(conn, mid, mode="sql_like")
    body = "filing body"
    conn.execute(
        "INSERT INTO corpus_chunks VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("c1", "d", body, _ch(body), _mh(), "2025-08-01T00:00:00Z", "[]", mid, "filing_v3", "active"),
    )
    with pytest.raises(ValueError, match="fts5"):
        export_source_bundle(
            conn, corpus_manifest_id=mid, snapshot_id=snap, output_dir=tmp_path
        )
    conn.close()


def test_production_export_ok(tmp_path: Path):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE corpus_chunks (
            chunk_id TEXT, document_id TEXT, content_text TEXT, content_hash TEXT,
            metadata_hash TEXT, available_at TEXT, ticker_associations TEXT,
            manifest_id TEXT, chunk_profile_version TEXT, status TEXT
        )"""
    )
    mid, snap = "3" * 64, "d" * 64
    _seed_manifest(conn, mid, snap)
    _seed_lex(conn, mid)
    body = "filing body text"
    conn.execute(
        "INSERT INTO corpus_chunks VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("c1", "d", body, _ch(body), _mh(), "2025-08-01T00:00:00Z", "[]", mid, "filing_v3", "active"),
    )
    bid = export_source_bundle(
        conn, corpus_manifest_id=mid, snapshot_id=snap, output_dir=tmp_path
    )
    assert (tmp_path / f"source_{bid}" / "chunks.jsonl").is_file()
    manifest = json.loads(
        (tmp_path / f"source_{bid}" / "source_bundle_manifest.json").read_text()
    )
    assert manifest["probe_report_id"]
    assert manifest["schema_version"] == "1.1.0"
    conn.close()


def test_existing_corrupted_chunks_rejected(tmp_path: Path):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE corpus_chunks (
            chunk_id TEXT, document_id TEXT, content_text TEXT, content_hash TEXT,
            metadata_hash TEXT, available_at TEXT, ticker_associations TEXT,
            manifest_id TEXT, chunk_profile_version TEXT, status TEXT
        )"""
    )
    mid, snap = "4" * 64, "d" * 64
    _seed_manifest(conn, mid, snap)
    _seed_lex(conn, mid)
    body = "hello"
    conn.execute(
        "INSERT INTO corpus_chunks VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("c1", "d", body, _ch(body), _mh(), "2025-08-01T00:00:00Z", "[]", mid, "filing_v3", "active"),
    )
    bid = export_source_bundle(
        conn, corpus_manifest_id=mid, snapshot_id=snap, output_dir=tmp_path,
    )
    chunks = tmp_path / f"source_{bid}" / "chunks.jsonl"
    chunks.write_text(chunks.read_text() + "CORRUPT\n")
    with pytest.raises(ValueError):
        export_source_bundle(
            conn, corpus_manifest_id=mid, snapshot_id=snap, output_dir=tmp_path,
        )
    conn.close()


def test_existing_identical_bundle_reused(tmp_path: Path):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE corpus_chunks (
            chunk_id TEXT, document_id TEXT, content_text TEXT, content_hash TEXT,
            metadata_hash TEXT, available_at TEXT, ticker_associations TEXT,
            manifest_id TEXT, chunk_profile_version TEXT, status TEXT
        )"""
    )
    mid, snap = "5" * 64, "d" * 64
    _seed_manifest(conn, mid, snap)
    _seed_lex(conn, mid)
    body = "same"
    conn.execute(
        "INSERT INTO corpus_chunks VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("c1", "d", body, _ch(body), _mh(), "2025-08-01T00:00:00Z", "[]", mid, "filing_v3", "active"),
    )
    s1 = export_source_bundle(
        conn, corpus_manifest_id=mid, snapshot_id=snap, output_dir=tmp_path,
    )
    s2 = export_source_bundle(
        conn, corpus_manifest_id=mid, snapshot_id=snap, output_dir=tmp_path,
    )
    assert s1 == s2
    conn.close()


def test_certified_snapshot_mismatch_rejected(tmp_path: Path):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE corpus_chunks (
            chunk_id TEXT, document_id TEXT, content_text TEXT, content_hash TEXT,
            metadata_hash TEXT, available_at TEXT, ticker_associations TEXT,
            manifest_id TEXT, chunk_profile_version TEXT, status TEXT
        )"""
    )
    mid = "6" * 64
    _seed_manifest(conn, mid, "z" * 64)
    _seed_lex(conn, mid)
    body = "x"
    conn.execute(
        "INSERT INTO corpus_chunks VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("c1", "d", body, _ch(body), _mh(), "2025-08-01T00:00:00Z", "[]", mid, "filing_v3", "active"),
    )
    with pytest.raises(ValueError, match="certified_snapshot"):
        export_source_bundle(
            conn, corpus_manifest_id=mid, snapshot_id="d" * 64, output_dir=tmp_path,
        )
    conn.close()


def test_checksums_file_covers_all_artifacts(tmp_path: Path):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE corpus_chunks (
            chunk_id TEXT, document_id TEXT, content_text TEXT, content_hash TEXT,
            metadata_hash TEXT, available_at TEXT, ticker_associations TEXT,
            manifest_id TEXT, chunk_profile_version TEXT, status TEXT
        )"""
    )
    mid, snap = "7" * 64, "d" * 64
    _seed_manifest(conn, mid, snap)
    _seed_lex(conn, mid)
    body = "t"
    conn.execute(
        "INSERT INTO corpus_chunks VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("c1", "d", body, _ch(body), _mh(), "2025-08-01T00:00:00Z", "[]", mid, "filing_v3", "active"),
    )
    bid = export_source_bundle(
        conn, corpus_manifest_id=mid, snapshot_id=snap, output_dir=tmp_path,
    )
    text = (tmp_path / f"source_{bid}" / "checksums.sha256").read_text()
    assert "chunks.jsonl" in text
    assert "source_bundle_manifest.json" in text
    conn.close()


def test_existing_bundle_rejects_manifest_schema_or_universe_drift(tmp_path: Path):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE corpus_chunks (
            chunk_id TEXT, document_id TEXT, content_text TEXT, content_hash TEXT,
            metadata_hash TEXT, available_at TEXT, ticker_associations TEXT,
            manifest_id TEXT, chunk_profile_version TEXT, status TEXT
        )"""
    )
    mid, snap = "8" * 64, "d" * 64
    _seed_manifest(conn, mid, snap)
    _seed_lex(conn, mid)
    body = "stable"
    conn.execute(
        "INSERT INTO corpus_chunks VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("c1", "d", body, _ch(body), _mh(), "2025-08-01T00:00:00Z", "[]", mid, "filing_v3", "active"),
    )
    bid = export_source_bundle(conn, corpus_manifest_id=mid, snapshot_id=snap, output_dir=tmp_path)
    bundle = tmp_path / f"source_{bid}"
    manifest_path = bundle / "source_bundle_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["schema_version"] = "0.0.0"
    manifest["universe_manifest_id"] = "f" * 64
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")))
    manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    checksum_path = bundle / "checksums.sha256"
    checksum_lines = []
    for line in checksum_path.read_text().splitlines():
        if line.endswith("  source_bundle_manifest.json"):
            line = f"{manifest_sha}  source_bundle_manifest.json"
        checksum_lines.append(line)
    checksum_path.write_text("\n".join(checksum_lines) + "\n")
    with pytest.raises(ValueError, match="schema_version|universe_manifest_id"):
        export_source_bundle(conn, corpus_manifest_id=mid, snapshot_id=snap, output_dir=tmp_path)
    conn.close()


def test_existing_bundle_rejects_vector_artifact_even_when_checksummed(tmp_path: Path):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE corpus_chunks (
            chunk_id TEXT, document_id TEXT, content_text TEXT, content_hash TEXT,
            metadata_hash TEXT, available_at TEXT, ticker_associations TEXT,
            manifest_id TEXT, chunk_profile_version TEXT, status TEXT
        )"""
    )
    mid, snap = "9" * 64, "d" * 64
    _seed_manifest(conn, mid, snap)
    _seed_lex(conn, mid)
    body = "stable"
    conn.execute(
        "INSERT INTO corpus_chunks VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("c1", "d", body, _ch(body), _mh(), "2025-08-01T00:00:00Z", "[]", mid, "filing_v3", "active"),
    )
    bid = export_source_bundle(conn, corpus_manifest_id=mid, snapshot_id=snap, output_dir=tmp_path)
    bundle = tmp_path / f"source_{bid}"
    vector_path = bundle / "embeddings.bin"
    vector_path.write_bytes(b"vector payload")
    vector_sha = hashlib.sha256(vector_path.read_bytes()).hexdigest()
    checksum_path = bundle / "checksums.sha256"
    checksum_path.write_text(
        checksum_path.read_text() + f"{vector_sha}  embeddings.bin\n"
    )
    with pytest.raises(ValueError, match="artifact set|vector|embedding"):
        export_source_bundle(conn, corpus_manifest_id=mid, snapshot_id=snap, output_dir=tmp_path)

    vector_path.unlink()
    checksum_path.write_text(
        "\n".join(
            line
            for line in checksum_path.read_text().splitlines()
            if not line.endswith("  embeddings.bin")
        )
        + "\n"
    )
    (bundle / "vectors").mkdir()
    with pytest.raises(ValueError, match="artifact set|vector|embedding"):
        export_source_bundle(conn, corpus_manifest_id=mid, snapshot_id=snap, output_dir=tmp_path)
    conn.close()


def test_production_exporter_is_bounded_and_streams_large_artifacts():
    source = inspect.getsource(source_bundle_module)
    assert "fetchall(" not in source
    assert ".read_bytes(" not in source
    assert "splitlines(" not in source
    assert "fetchmany(" in source
    for forbidden in ("seen_ids", "records =", "hashes ="):
        assert forbidden not in source


def test_chunk_cursor_reads_are_capped_at_500():
    class Cursor:
        def __init__(self, rows):
            self.rows = rows
            self.position = 0
            self.sizes = []

        def fetchmany(self, size):
            self.sizes.append(size)
            batch = self.rows[self.position : self.position + size]
            self.position += len(batch)
            return batch

    class Connection:
        def __init__(self, cursor):
            self.cursor = cursor

        def execute(self, *_args):
            return self.cursor

    rows = [(f"chunk_{i:04d}",) for i in range(1201)]
    cursor = Cursor(rows)
    yielded = list(
        source_bundle_module._iter_chunk_rows(
            Connection(cursor), "corpus_chunks", "a" * 64
        )
    )
    assert yielded == rows
    assert cursor.sizes
    assert max(cursor.sizes) <= 500


def test_streamed_records_retain_only_bounded_pages():
    class Page(list):
        live = 0
        peak = 0

        def __init__(self, rows):
            super().__init__(rows)
            type(self).live += 1
            type(self).peak = max(type(self).peak, type(self).live)

        def __del__(self):
            type(self).live -= 1

    class Cursor:
        def __init__(self, total):
            self.total = total
            self.position = 0

        def fetchmany(self, size):
            start = self.position
            stop = min(start + size, self.total)
            self.position = stop
            return Page(
                [
                    (
                        f"chunk_{index:05d}",
                        "document",
                        "body",
                        _ch("body"),
                        _mh(),
                        "2025-08-01T00:00:00Z",
                        "[]",
                        "a" * 64,
                        "filing_v3",
                    )
                    for index in range(start, stop)
                ]
            )

    class Connection:
        def __init__(self, cursor):
            self.cursor = cursor

        def execute(self, *_args):
            return self.cursor

    cursor = Cursor(3001)
    records = source_bundle_module._iter_valid_records(
        Connection(cursor), "corpus_chunks", "a" * 64
    )
    count = sum(1 for _ in records)
    assert count == 3001
    assert Page.peak <= 2


def test_streamed_source_bundle_id_matches_legacy_formula():
    from catalyst_data.manifests.universe import sha256_identity

    hashes = [f"{index:064x}" for index in range(3)]
    expected = sha256_identity(
        {
            "schema_version": "1.1.0",
            "corpus_manifest_id": "a" * 64,
            "snapshot_id": "b" * 64,
            "probe_report_id": "c" * 64,
            "postbuild_readiness_id": "d" * 64,
            "ordered_chunk_record_hashes": hashes,
        }
    )
    actual = source_bundle_module._stream_source_bundle_id(
        corpus_manifest_id="a" * 64,
        snapshot_id="b" * 64,
        probe_report_id="c" * 64,
        postbuild_readiness_id="d" * 64,
        record_hashes=iter(hashes),
    )
    assert actual == expected
