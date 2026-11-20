"""App test fixtures (M6 corrective).

The V1.1 router fail-closes when a server_env provider-specific env key is
missing, so app tests that POST server_env runs simulate a configured runtime
by default. Tests that exercise the fail-closed path explicitly delete the
key with monkeypatch.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _drop_env_loader_writes():
    """Drop keys ``load_env_files`` writes after each test.

    The loader assigns canonical names directly and copies them onto lowercase
    aliases with ``setdefault``. Monkeypatch does not record those writes, so a
    later ``load_env_files`` copies the leftover alias back onto the canonical
    name. This fixture is defined first so it runs after monkeypatch undo.
    """
    yield
    from catalyst_app.env_loader import _ENV_KEY_ALIASES

    for canonical, aliases in _ENV_KEY_ALIASES.items():
        os.environ.pop(canonical, None)
        for alias in aliases:
            os.environ.pop(alias, None)


@pytest.fixture(autouse=True)
def _configured_provider_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide a provider-specific env key for server_env test requests.

    ``load_env_files`` mirrors canonical keys to lowercase aliases
    (``openai_api_key``) via ``setdefault``; the fixture sets both so the
    canonical and alias views agree and later ``load_env_files`` calls cannot
    resurrect a stale value from a prior test.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai-provider")
    monkeypatch.setenv("openai_api_key", "sk-test-openai-provider")


# --------------------------------------------------------------------------- #
# WP-B B7a: loader wiring tests that use a minimal served-build DB (the three
# resolver tables only) cannot build a real served projection. They opt in to
# this stub; the projection itself is tested against production-shaped
# fixtures in packages/data/tests/test_served_projection*.py.
# --------------------------------------------------------------------------- #
@pytest.fixture()
def stub_served_projection(monkeypatch):
    import sqlite3 as _sqlite3
    from types import MappingProxyType

    from catalyst_data.retrieval import runtime_binding as _runtime_binding
    from catalyst_data.retrieval.lexical_retriever import resolve_served_build
    from catalyst_data.retrieval.served_projection import ServedProjectionHandle

    built: list[ServedProjectionHandle] = []

    def _stub(served_conn_factory, *, expected_manifest_id, projection_dir):
        conn = served_conn_factory()
        try:
            resolution = resolve_served_build(conn, expected_manifest_id=expected_manifest_id)
        finally:
            conn.close()
        path = projection_dir / "served_projection_stub.db"
        _sqlite3.connect(path).close()
        handle = ServedProjectionHandle(
            path=path,
            meta=MappingProxyType(
                {
                    "build_id": resolution.build_id,
                    "manifest_id": resolution.corpus_manifest_id,
                    "row_count": 0,
                    "projection_sha256": "0" * 64,
                    "schema_version": "served_projection:v1",
                }
            ),
        )
        built.append(handle)
        return handle

    monkeypatch.setattr(_runtime_binding, "build_served_projection", _stub)
    return built


@pytest.fixture(autouse=True)
def _primary_verifier_off_unless_tested(monkeypatch):
    """Graph tests use fake analyst models that cannot answer the same-event
    verifier (on by default in production); tests of the verifier call it directly."""
    monkeypatch.setenv("CATALYST_PRIMARY_VERIFIER", "0")


@pytest.fixture(autouse=True)
def _production_evidence_window_policy(monkeypatch, tmp_path):
    """Production runtime composition uses the sole supported cascade policy."""
    monkeypatch.setenv("CATALYST_EVIDENCE_WINDOW_POLICY", "desk_cascade_v1")
    monkeypatch.setenv("CATALYST_WEB_EVIDENCE", "off")
    market_store = tmp_path / "market_context.sqlite3"
    market_store.touch()
    monkeypatch.setenv("CATALYST_MARKET_CONTEXT_DB", str(market_store))
