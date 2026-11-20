"""B2 — read-only smoke of the served-build resolver on the real derivative.

The persisted M8-A derivative (``data/snapshots/catalyst_m8a_derivative_7a004acc.db``)
holds the only body-bearing corpus, build ``bec845b4…``. That build is
``lexical_ready`` but **not published**, so it is not a served build and the
B2 resolver must refuse it (Frozen-A3 §2.2; WP-B B2). This test asserts that
refusal on the real file; it never works around it and never writes.

The DB is opened ``mode=ro`` with ``PRAGMA query_only=ON``. When the derivative
is absent the test skips with a stated reason.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from catalyst_data.canonical.identity import DataRuntimeIdentity
from catalyst_data.canonical.temporal import TemporalIdentity
from catalyst_data.retrieval.lexical_retriever import (
    LexicalDisplayRetriever,
    resolve_served_build,
)
from catalyst_data.retrieval.result import RetrievalContractError

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
DB_PATH = REPO_ROOT / "data" / "snapshots" / "catalyst_m8a_derivative_7a004acc.db"

# The only body-bearing corpus, published nowhere (Frozen-A3 F1).
BEC_BUILD_ID = "bec845b416114d412ccd1ce15888b8294ce4ebda5287be3ff03366f50f54d8e1"


def _open_db() -> sqlite3.Connection:
    if not DB_PATH.is_file():
        pytest.skip(
            "persisted M8-A derivative absent, so served-build resolution "
            f"cannot be smoke-tested: {DB_PATH}"
        )
    # immutable=1: never create a SQLite side file next to the served DB.
    conn = sqlite3.connect(f"{DB_PATH.as_uri()}?mode=ro&immutable=1", uri=True)
    conn.execute("PRAGMA query_only=ON")
    return conn


def _identity_for(conn: sqlite3.Connection) -> tuple[DataRuntimeIdentity, TemporalIdentity]:
    manifest_id, digest = conn.execute(
        "SELECT corpus_manifest_id, lexical_digest FROM lexical_index_state "
        "WHERE singleton_id = 1"
    ).fetchone()
    identity = DataRuntimeIdentity(
        data_snapshot_id="catalyst_m8a_derivative_7a004acc",
        corpus_manifest_id=str(manifest_id),
        fts_index_version=str(digest),
        query_policy_version="qp:v1",
    )
    cutoff_at = datetime(2026, 8, 2, tzinfo=timezone.utc)
    temporal = TemporalIdentity(
        session_date="2026-08-02",
        market_timezone="America/New_York",
        session_open_at=cutoff_at,
        session_close_at=cutoff_at,
        information_window_start_at=cutoff_at,
        cutoff_at=cutoff_at,
    )
    return identity, temporal


def test_resolver_refuses_the_unpublished_body_bearing_build():
    conn = _open_db()
    try:
        build_row = conn.execute(
            "SELECT status, lexical_ready, manifest_id FROM corpus_publication_builds "
            "WHERE build_id = ?",
            (BEC_BUILD_ID,),
        ).fetchone()
        assert build_row is not None, "the body-bearing build must exist in the derivative"
        status, lexical_ready, bec_manifest_id = build_row
        if status != "lexical_ready":
            # The premise (this build is unpublished) is a fact about the snapshot,
            # and the snapshot has since published it.
            pytest.skip(f"the body-bearing build is now {status!r}; the unpublished premise no longer holds")
        assert status == "lexical_ready"
        assert int(lexical_ready) == 1

        # The served build is the *published* generation of the current manifest.
        resolution = resolve_served_build(conn)
        assert resolution.build_id != BEC_BUILD_ID
        assert resolution.matched_row_count == 1
        assert resolution.lexical_digest
        assert resolution.lexical_row_count > 0
        current = conn.execute(
            "SELECT manifest_id FROM corpus_manifest WHERE is_current = 1"
        ).fetchone()[0]
        assert resolution.corpus_manifest_id == current

        # …and the resolver refuses the unpublished build, both by id and by
        # treating its manifest as the requested one.
        with pytest.raises(RetrievalContractError):
            resolve_served_build(conn, expected_build_id=BEC_BUILD_ID)
        with pytest.raises(RetrievalContractError):
            resolve_served_build(conn, expected_manifest_id=str(bec_manifest_id))

        identity, temporal = _identity_for(conn)
        retriever = LexicalDisplayRetriever(
            db=conn, data_runtime_identity=identity, temporal_identity=temporal
        )
        assert retriever.resolve_served_build().build_id == resolution.build_id
        with pytest.raises(RetrievalContractError):
            retriever.resolve_served_build(expected_build_id=BEC_BUILD_ID)
    finally:
        conn.close()
