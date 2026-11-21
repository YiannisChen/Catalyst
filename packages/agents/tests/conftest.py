from __future__ import annotations

import sys
from pathlib import Path

import pytest

PACKAGE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_DIR.parents[1]
_FIX_CMD = (
    ".venv/bin/pip install -e \"packages/data[dev]\" "
    "-e packages/agents -e packages/api -e packages/eval"
)


def _refuse_stale_editable_install() -> None:
    """Fail when the installed catalyst_agents points at a deleted worktree.

    This used to live in the repo-root conftest. It runs before the path
    insert below, so it sees the editable install rather than this checkout.
    """
    try:
        import catalyst_agents
    except ImportError:
        return
    resolved = Path(catalyst_agents.__file__).resolve()
    try:
        resolved.relative_to(REPO_ROOT)
    except ValueError:
        sys.stderr.write(
            f"\nSTALE EDITABLE INSTALL: catalyst_agents resolves to\n"
            f"  {resolved}\n"
            f"which is outside the repo root\n"
            f"  {REPO_ROOT}\n"
            f"Fix: cd {REPO_ROOT} && {_FIX_CMD}\n\n"
        )
        pytest.exit(
            f"catalyst_agents resolves outside repo root: {resolved}\n"
            f"Reinstall: {_FIX_CMD}",
            returncode=1,
        )


_refuse_stale_editable_install()

MONOREPO_PACKAGES = [
    REPO_ROOT,
    PACKAGE_DIR,
    PACKAGE_DIR / "tests",
    PACKAGE_DIR.parent / "eval",
    PACKAGE_DIR.parent / "data",
]

for package_path in MONOREPO_PACKAGES:
    package_str = str(package_path)
    if package_str not in sys.path:
        sys.path.insert(0, package_str)


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

    monkeypatch.setattr(
        _runtime_binding,
        "open_or_build_served_projection",
        lambda factory, **kwargs: (_stub(factory, **kwargs), False),
    )
    return built


@pytest.fixture(autouse=True)
def _primary_verifier_off_unless_tested(monkeypatch):
    """Graph tests use fake analyst models that cannot answer the same-event
    verifier (on by default in production); tests of the verifier call it directly."""
    monkeypatch.setenv("CATALYST_PRIMARY_VERIFIER", "0")


@pytest.fixture(autouse=True)
def _legacy_evidence_window_policy(monkeypatch):
    """The production default is desk_cascade_v1 (A′). Graph and runtime tests written for
    pre_close_v1 (P) keep exercising P unless a test sets the policy itself; tests of the
    default call resolve_attribution_policy with an explicit env."""
    monkeypatch.setenv("CATALYST_EVIDENCE_WINDOW_POLICY", "pre_close_v1")
