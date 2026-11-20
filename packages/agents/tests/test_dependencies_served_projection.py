"""WP-B B7a: the loader builds the served projection before any retriever."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from catalyst_agents.runtime import dependencies as deps_module
from catalyst_agents.runtime.dependencies import RuntimeDependencyLoader
from catalyst_data.retrieval.lexical_display import RETRIEVAL_PROFILE
from catalyst_data.retrieval.served_projection import ServedProjectionHandle
from catalyst_data.storage.connect import open_readonly

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "data" / "tests"))
from served_projection_fixtures import (  # noqa: E402
    BUILD,
    MANIFEST,
    make_served_db,
    rich_chunks,
)


def _digest(db: Path) -> str:
    with open_readonly(db) as conn:
        return str(
            conn.execute(
                "SELECT lexical_digest FROM corpus_publication_builds WHERE build_id=?",
                (BUILD,),
            ).fetchone()[0]
        )


def _loader(db: Path, **kwargs) -> RuntimeDependencyLoader:
    return RuntimeDependencyLoader(
        sqlite_db_path=db,
        retrieval_profile=RETRIEVAL_PROFILE,
        expected_fts_digest=kwargs.pop("expected_fts_digest", _digest(db)),
        requested_manifest_id=kwargs.pop("requested_manifest_id", MANIFEST),
        source_bundle_id="c" * 64,
        snapshot_id="s" * 64,
        probe_report_id="p" * 64,
        postbuild_readiness_id="q" * 64,
        require_identity_bound_runtime=True,
        **kwargs,
    )


@pytest.mark.parametrize("switch", ["CATALYST_FILING_UNITS"])
def test_public_runtime_health_fails_when_a_store_reader_is_disabled(tmp_path, monkeypatch, switch):
    monkeypatch.setenv("CATALYST_ALLOW_NON_LOOPBACK", "1")
    monkeypatch.setenv(switch, "off")
    loader = RuntimeDependencyLoader(sqlite_db_path=tmp_path / "not-opened.sqlite3")

    health = loader.health()

    assert health["status"] == "failed"
    assert health["deployment_configuration"]["status"] == "failed"
    assert switch in health["errors"][0]["message"]


@pytest.fixture()
def served_db(tmp_path: Path) -> Path:
    served_dir = tmp_path / "served"
    served_dir.mkdir()
    return make_served_db(served_dir / "served.db", rich_chunks())


def test_loader_builds_and_binds_the_projection(served_db, tmp_path):
    proj_dir = tmp_path / "proj"
    deps = _loader(served_db, projection_dir=proj_dir).get_dependencies()
    assert deps.health["retrieval"]["status"] == "ready"
    retriever = deps.retriever._retriever
    handle = retriever.served_projection
    assert isinstance(handle, ServedProjectionHandle)
    assert handle.path.parent == proj_dir.resolve()
    assert handle.build_id == BUILD
    assert deps.served_projection == handle.identity()


def test_loader_binds_served_digest_and_manifest_dynamically(served_db, tmp_path):
    deps = _loader(
        served_db,
        expected_fts_digest="served",
        requested_manifest_id="served",
        projection_dir=tmp_path / "proj",
    ).get_dependencies()
    assert deps.health["retrieval"]["status"] == "ready"
    assert deps.health["retrieval"]["corpus_manifest_id"] == MANIFEST


def test_loader_defaults_to_a_temporary_directory(served_db, monkeypatch):
    monkeypatch.delenv(deps_module.PROJECTION_DIR_ENV, raising=False)
    deps = _loader(served_db).get_dependencies()
    handle = deps.retriever._retriever.served_projection
    assert deps.health["retrieval"]["status"] == "ready"
    assert handle.path.parent.name.startswith("catalyst-served-projection-")


def test_loader_reads_the_projection_dir_env(served_db, tmp_path, monkeypatch):
    target = tmp_path / "from-env"
    monkeypatch.setenv(deps_module.PROJECTION_DIR_ENV, str(target))
    deps = _loader(served_db).get_dependencies()
    assert deps.retriever._retriever.served_projection.path.parent == target.resolve()


def test_loader_rejects_the_served_db_directory(served_db):
    deps = _loader(served_db, projection_dir=served_db.parent).get_dependencies()
    assert deps.retriever is None
    assert deps.health["retrieval"]["status"] == "failed"
    assert "served_projection_dir_forbidden" in deps.health["retrieval"]["message"]


def test_loader_rejects_a_directory_inside_the_repository(served_db):
    repo_dir = Path(__file__).resolve().parents[3] / "packages" / "tmp-projection"
    deps = _loader(served_db, projection_dir=repo_dir).get_dependencies()
    assert deps.retriever is None
    assert "served_projection_dir_forbidden" in deps.health["retrieval"]["message"]
    assert not repo_dir.exists()


def test_projection_failure_means_no_retriever(served_db, tmp_path, monkeypatch):
    from catalyst_data.retrieval.result import RetrievalContractError

    def boom(*args, **kwargs):
        raise RetrievalContractError("served_projection_row_digest_mismatch", "x")

    from catalyst_data.retrieval import runtime_binding

    monkeypatch.setattr(runtime_binding, "build_served_projection", boom)
    deps = _loader(served_db, projection_dir=tmp_path / "proj").get_dependencies()
    assert deps.retriever is None
    assert deps.served_projection is None
    assert deps.health["status"] == "failed"
    assert "served projection failed" in deps.health["retrieval"]["message"]


def test_retriever_opens_and_closes_one_sidecar_connection_per_call(
    served_db, tmp_path, monkeypatch
):
    from catalyst_data.retrieval import lexical_retriever as lr

    deps = _loader(served_db, projection_dir=tmp_path / "proj").get_dependencies()
    retriever = deps.retriever._retriever
    handle = retriever.served_projection
    opened: list[object] = []
    closed: list[object] = []

    class _Tracked:
        def __init__(self, conn):
            self._conn = conn
            opened.append(self)

        def execute(self, *a, **k):
            return self._conn.execute(*a, **k)

        def executemany(self, *a, **k):
            return self._conn.executemany(*a, **k)

        def close(self):
            closed.append(self)
            self._conn.close()

    real_connect = ServedProjectionHandle.connect
    monkeypatch.setattr(
        ServedProjectionHandle, "connect", lambda self: _Tracked(real_connect(self))
    )
    seen: list[object] = []
    real_retrieve = lr.retrieve_lexical

    def spy(conn, query, **kwargs):
        seen.append((kwargs.get("served_projection"), kwargs.get("allow_late_updates")))
        return real_retrieve(conn, query, **kwargs)

    monkeypatch.setattr(lr, "retrieve_lexical", spy)

    from datetime import datetime, timezone

    from catalyst_data.canonical.temporal import TemporalIdentity

    identity = TemporalIdentity(
        session_date="2026-08-03",
        market_timezone="America/New_York",
        session_open_at=datetime(2026, 8, 3, 13, 30, tzinfo=timezone.utc),
        session_close_at=datetime(2026, 8, 3, 20, 0, tzinfo=timezone.utc),
        information_window_start_at=datetime(2026, 7, 31, 20, 0, tzinfo=timezone.utc),
        cutoff_at=datetime(2026, 8, 3, 20, 0, tzinfo=timezone.utc),
    )
    retriever.retrieve_with_observation(
        "AAPL",
        ticker="AAPL",
        cutoff="2026-08-03T20:00:00Z",
        top_k=lr.DISPLAY_TOP_K,
        candidate_depth=lr.CANDIDATE_DEPTH_CAP,
        temporal_identity=identity,
    )
    expanded_identity = TemporalIdentity.model_validate({
        **identity.model_dump(mode="python"),
        "evidence_window_start_at": datetime(2026, 7, 27, 20, 0, tzinfo=timezone.utc),
        "evidence_window_end_at": identity.cutoff_at,
    })
    retriever.retrieve_with_observation(
        "AAPL",
        ticker="AAPL",
        cutoff="2026-08-03T20:00:00Z",
        top_k=lr.DISPLAY_TOP_K,
        candidate_depth=lr.CANDIDATE_DEPTH_CAP,
        temporal_identity=expanded_identity,
    )
    assert len(opened) == len(closed) == 2
    assert seen == [
        ((handle, opened[0]), False),
        ((handle, opened[1]), True),
    ]
