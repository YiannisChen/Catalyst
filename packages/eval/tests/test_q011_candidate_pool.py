"""Q-011 candidate evidence-pool CLI tests (fixture-only, no model calls).

Proves the candidate pool runner is pointer-free and pre-approval:
- loads exactly c01..c12 from the unsigned packet;
- rejects active-pointer-derived identities and active-path aliases;
- generates one identity-bound pool per case and a bounded annotation packet;
- marks every output unsigned/non-authoritative;
- leaves active-generation pointer bytes unchanged;
- uses an injectable fixture retriever so no provider/model call occurs.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from catalyst_data.config import (
    BGE_M3_DIMENSION,
    BGE_M3_MODEL,
    BGE_M3_REVISION,
    BGE_RERANKER_MODEL,
    BGE_RERANKER_REVISION,
)
from catalyst_data.retrieval.pool import load_union_pool

from tests.post_import_fixtures import make_result, make_result_set


def _hex(value: str = "a") -> str:
    return (value * 64)[:64]


def _identities() -> dict[str, str]:
    return {
        "build_id": _hex("b"),
        "corpus_manifest_id": _hex("c"),
        "source_bundle_id": _hex("d"),
        "snapshot_id": _hex("e"),
        "probe_report_id": _hex("f"),
        "postbuild_readiness_id": _hex("0"),
        "index_manifest_id": _hex("1"),
    }


def _packet(tmp_path: Path, *, unsigned: bool = True, count: int = 12) -> Path:
    path = tmp_path / "q011_packet.json"
    cases = []
    for index in range(1, count + 1):
        slot = f"c{index:02d}"
        cases.append(
            {
                "slot": slot,
                "ticker": "AAPL",
                "session_date": "2025-05-12",
                "question": f"Why did {slot} move?",
                "cutoff_utc": "2025-05-12T20:00:00Z",
            }
        )
    path.write_text(
        json.dumps(
            {
                "schema_version": "q011_final_12_case_packet_v1",
                "unsigned_q011": bool(unsigned),
                "non_authoritative": bool(unsigned),
                "cases": cases,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return path


def _candidate_lancedb(tmp_path: Path, identities: dict[str, str]) -> Path:
    """Legacy empty-ish candidate dir for mutation tests (no manifest file)."""
    candidate_dir = tmp_path / "candidate_lancedb"
    candidate_dir.mkdir(exist_ok=True)
    payload = {
        "schema_version": "candidate_generation_v1",
        "status": "inactive",
        "corpus_manifest_id": identities["corpus_manifest_id"],
        "index_manifest_id": identities["index_manifest_id"],
        "table_name": "candidate_table",
        "chunk_count": 2,
    }
    (candidate_dir / "candidate_generation.json").write_text(
        json.dumps(payload, sort_keys=True), encoding="utf-8"
    )
    return candidate_dir


def _derivative(tmp_path: Path, *, is_current: int = 0) -> Path:
    db = tmp_path / "derivative.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE corpus_manifest (manifest_id TEXT PRIMARY KEY, "
        "manifest_json TEXT, is_current INTEGER NOT NULL)"
    )
    conn.execute(
        "INSERT INTO corpus_manifest (manifest_id, manifest_json, is_current) "
        "VALUES (?, ?, ?)",
        (_hex("c"), json.dumps({}), is_current),
    )
    conn.commit()
    conn.close()
    return db


def _derivative_other(tmp_path: Path, *, name: str = "other.db", is_current: int = 1) -> Path:
    db = tmp_path / name
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE corpus_manifest (manifest_id TEXT PRIMARY KEY, "
        "manifest_json TEXT, is_current INTEGER NOT NULL)"
    )
    conn.execute(
        "INSERT INTO corpus_manifest (manifest_id, manifest_json, is_current) "
        "VALUES (?, ?, ?)",
        (_hex("c"), json.dumps({}), is_current),
    )
    conn.commit()
    conn.close()
    return db


def _load_cli():
    path = Path(__file__).resolve().parents[1] / "scripts" / "run_q011_candidate_pool.py"
    spec = importlib.util.spec_from_file_location("run_q011_candidate_pool", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _base_argv(mode: str, tmp_path: Path, identities: dict[str, str], *, with_active: bool = False) -> list[str]:
    candidate = _candidate_dir_with_records(tmp_path, identities)
    argv = [
        mode,
        "--packet", str(_packet(tmp_path)),
        "--derivative", str(_derivative(tmp_path)),
        "--build-id", identities["build_id"],
        "--corpus-manifest-id", identities["corpus_manifest_id"],
        "--source-bundle-id", identities["source_bundle_id"],
        "--snapshot-id", identities["snapshot_id"],
        "--probe-report-id", identities["probe_report_id"],
        "--postbuild-readiness-id", identities["postbuild_readiness_id"],
        "--lancedb-dir", str(candidate),
        "--index-manifest-id", identities["index_manifest_id"],
        "--table-name", identities.get("table_name", "candidate_table"),
        "--output-dir", str(tmp_path / "pool-out"),
        "--run-id", "q011-candidate-pool",
        "--embedding-mode", "mock_unit_test",
    ]
    if with_active:
        active = tmp_path / "active_lancedb"
        active.mkdir(exist_ok=True)
        pointer = active / "active_generation.json"
        if not pointer.exists():
            pointer.write_text(
                json.dumps({"schema_version": "active_generation_v1", "table_name": "live"}),
                encoding="utf-8",
            )
        argv += ["--active-lancedb-dir", str(active), "--active-generation-pointer", str(pointer)]
    return argv


def _candidate_dir_with_records(tmp_path: Path, identities: dict[str, str], *, count: int = 2) -> Path:
    """Candidate dir with generation + index manifest + real LanceDB table."""
    import numpy as np
    from catalyst_data.config import BGE_M3_DIMENSION
    from catalyst_data.corpus.tokenizer import TOKENIZER_REVISION
    from catalyst_data.retrieval.index_manifest import IndexManifest

    candidate_dir = tmp_path / "candidate_lancedb"
    candidate_dir.mkdir()
    vectors = np.zeros((count, BGE_M3_DIMENSION), dtype=np.float32)
    for index in range(count):
        vectors[index, 0] = 1.0 + index
    checksums = {
        "vectors.npy": hashlib.sha256(b"v").hexdigest(),
        "chunk_ids.json": hashlib.sha256(b"c").hexdigest(),
        "lancedb_table": hashlib.sha256(b"l").hexdigest(),
    }
    manifest = IndexManifest(
        model_name=BGE_M3_MODEL,
        model_revision=BGE_M3_REVISION,
        tokenizer_revision=TOKENIZER_REVISION,
        normalization_mode="l2",
        dtype="float32",
        dimension=BGE_M3_DIMENSION,
        corpus_manifest_id=identities["corpus_manifest_id"],
        source_bundle_id=identities["source_bundle_id"],
        snapshot_id=identities["snapshot_id"],
        probe_report_id=identities["probe_report_id"],
        postbuild_readiness_id=identities["postbuild_readiness_id"],
        artifact_hashes=checksums,
        code_revision="a" * 40,
        vector_count=count,
        artifact_state="vectors_staged",
    )
    (candidate_dir / "index_manifest.json").write_text(
        json.dumps(manifest.to_dict(), sort_keys=True), encoding="utf-8"
    )
    table_name = f"candidate_{manifest.index_manifest_id[:16]}"
    payload = {
        "schema_version": "candidate_generation_v1",
        "status": "inactive",
        "corpus_manifest_id": identities["corpus_manifest_id"],
        "index_manifest_id": manifest.index_manifest_id,
        "table_name": table_name,
        "chunk_count": count,
        "source_bundle_id": identities["source_bundle_id"],
        "embedding_model": BGE_M3_MODEL,
        "embedding_revision": BGE_M3_REVISION,
        "embedding_dimension": BGE_M3_DIMENSION,
    }
    (candidate_dir / "candidate_generation.json").write_text(
        json.dumps(payload, sort_keys=True), encoding="utf-8"
    )
    try:
        import lancedb
        import pyarrow as pa

        table = lancedb.connect(str(candidate_dir)).create_table(
            table_name,
            data=[
                {
                    "chunk_id": f"chunk:{index:04d}",
                    "vector": vectors[index].tolist(),
                }
                for index in range(count)
            ],
            mode="overwrite",
        )
    except Exception:
        pass
    identities["table_name"] = table_name
    identities["index_manifest_id"] = manifest.index_manifest_id
    return candidate_dir


def _make_case_retriever():
    """Deterministic HybridRetrievalResult-shaped fixture per case.

    Each arm returns exactly one genuine result carrying the field values the
    persisted arm artifact requires, proving all four arms were served.
    """
    def _hybrid(case):
        import types

        fts5 = make_result_set("lexical", "fts5", (f"{case.case_id}-a",), ticker=case.ticker)
        dense = make_result_set("dense", "dense", (f"{case.case_id}-b",), ticker=case.ticker)
        fusion = (
            make_result(
                f"{case.case_id}-c",
                ticker=case.ticker,
                available_at="2025-05-01T00:00:00Z",
                mode_requested="hybrid",
                mode_served="hybrid",
                fusion_rank=1,
                fusion_score=0.5,
                lexical_rank=1,
                lexical_raw_score=1.0,
            ),
        )
        final = (
            make_result(
                f"{case.case_id}-d",
                ticker=case.ticker,
                available_at="2025-05-01T00:00:00Z",
                mode_requested="reranked",
                mode_served="reranked",
                reranker_rank=1,
                reranker_score=9.0,
                fusion_rank=1,
                fusion_score=0.5,
            ),
        )
        # normalize fixture result cutoff to the case cutoff
        def _fix_result_set(rs, mode, served):
            fixed = []
            for item in rs.results:
                fixed.append(
                    make_result(
                        item.chunk_id,
                        ticker=case.ticker,
                        available_at=item.available_at,
                        mode_requested=mode,
                        mode_served=served,
                        lexical_rank=item.lexical_rank,
                        dense_rank=item.dense_rank,
                        lexical_raw_score=item.lexical_raw_score,
                        dense_score=item.dense_score,
                    )
                )
            return fixed

        fts5_fixed = tuple(_fix_result_set(fts5, "lexical", "fts5"))
        dense_fixed = tuple(_fix_result_set(dense, "dense", "dense"))
        return types.SimpleNamespace(
            lexical_results=types.SimpleNamespace(
                results=fts5_fixed,
                mode_served="fts5",
                trace=types.SimpleNamespace(total_ms=1.0),
            ),
            dense_results=types.SimpleNamespace(
                results=dense_fixed,
                mode_served="dense",
                trace=types.SimpleNamespace(total_ms=1.0),
            ),
            fusion_results=fusion,
            final_results=final,
            mode_requested="reranked",
            mode_served="reranked",
            degradation_reasons=(),
            latency_ms=1.0,
        )

    return _hybrid


def test_preflight_accepts_inactive_candidate(tmp_path):
    cli = _load_cli()
    identities = _identities()
    code = cli.main(_base_argv("preflight", tmp_path, identities))
    assert code == 0


def test_packet_must_be_unsigned(tmp_path):
    cli = _load_cli()
    identities = _identities()
    argv = _base_argv("preflight", tmp_path, identities)
    argv[2] = str(_packet(tmp_path, unsigned=False))
    code = cli.main(argv)
    assert code == 2


def test_active_corpus_rejected(tmp_path):
    cli = _load_cli()
    identities = _identities()
    argv = _base_argv("preflight", tmp_path, identities)
    other = _derivative_other(tmp_path)
    argv[argv.index("--derivative") + 1] = str(other)
    code = cli.main(argv)
    assert code == 2


def test_active_path_alias_rejected(tmp_path):
    cli = _load_cli()
    identities = _identities()
    argv = _base_argv("preflight", tmp_path, identities)
    active = tmp_path / "active_lancedb"
    active.mkdir()
    argv += ["--active-lancedb-dir", str(active)]
    # candidate lancedb dir aliases active dir via the same resolved path
    argv[argv.index("--lancedb-dir") + 1] = str(active)
    code = cli.main(argv)
    assert code == 2


def test_execute_writes_twelve_pools_and_bounded_packets(tmp_path, monkeypatch):
    cli = _load_cli()
    identities = _identities()
    output = tmp_path / "pool-out"
    argv = _base_argv("execute", tmp_path, identities, with_active=True)
    # ensure packet/derivative/lancedb arguments are consistent
    code = cli.main(argv, retriever_factory=lambda: _make_case_retriever())
    assert code == 0

    pools = sorted((output / "pools").glob("c*.json"))
    packets = sorted((output / "packets").glob("c*.json"))
    assert len(pools) == 12
    assert len(packets) == 12
    manifest = json.loads((output / "candidate_pool_manifest.json").read_text())
    assert manifest["status"] == "inactive_candidate_evidence_pool"
    assert manifest["unsigned_q011"] is True
    assert manifest["non_authoritative"] is True
    assert manifest["case_count"] == 12
    assert manifest["pointer_unchanged"] is True
    assert manifest["identity"]["corpus_manifest_id"] == identities["corpus_manifest_id"]
    assert manifest["identity"]["table_name"] == identities["table_name"]

    for pool_path in pools:
        pool = load_union_pool(pool_path)
        assert pool.case_id in {f"c{i:02d}" for i in range(1, 13)}
        assert set(pool.per_arm_chunk_ids) == {"fts5", "dense", "hybrid", "reranked"}
    for packet_path in packets:
        packet = json.loads(packet_path.read_text())
        assert packet["non_authoritative"] is True
        assert packet["unsigned_q011"] is True
        assert packet["arm_order"] == ["fts5", "dense", "hybrid", "reranked"]
        assert all(
            row["temporal_status"] in {"eligible", "post_cutoff_excluded"}
            for row in packet["rows"]
        )
        assert all(row["excerpt_bounded"] is True for row in packet["rows"])


def test_execute_preserves_active_pointer_bytes(tmp_path):
    cli = _load_cli()
    identities = _identities()
    active = tmp_path / "active_lancedb"
    active.mkdir()
    pointer = active / "active_generation.json"
    pointer.write_text(
        json.dumps({"schema_version": "active_generation_v1", "table_name": "live"}),
        encoding="utf-8",
    )
    before = pointer.read_bytes()
    argv = _base_argv("execute", tmp_path, identities, with_active=True)
    code = cli.main(argv, retriever_factory=lambda: _make_case_retriever())
    assert code == 0
    assert pointer.read_bytes() == before


def test_execute_refuses_nonempty_output(tmp_path):
    cli = _load_cli()
    identities = _identities()
    output = tmp_path / "pool-out"
    output.mkdir()
    (output / "junk").write_text("x", encoding="utf-8")
    argv = _base_argv("execute", tmp_path, identities, with_active=True)
    code = cli.main(argv, retriever_factory=lambda: _make_case_retriever())
    assert code == 2


def test_production_mode_fails_closed_without_cuda(tmp_path):
    """The production-pinned runner is cloud-only: no CUDA means no retriever."""
    from catalyst_eval.v1_1.candidate_pool import build_production_retriever

    with pytest.raises(Exception, match="torch|CUDA|cloud|offline"):
        build_production_retriever(
            derivative=tmp_path / "x.db",
            lancedb_dir=tmp_path / "lancedb",
            table_name="candidate_table",
            index_manifest_id=_hex("1"),
            corpus_manifest_id=_hex("c"),
            reranker_timeout=2.0,
            cuda_available=lambda: False,
        )
