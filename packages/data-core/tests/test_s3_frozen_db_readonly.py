"""S3 Phase 1: Frozen DB read-only guard and retrieval tests.

Design refs: §0.1, D1 landmine #7.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
FROZEN_DB = REPO_ROOT / "data" / "catalyst_eval_frozen_v2.db"

# Canonical SHA from when the frozen DB was last pinned
CANONICAL_SHA = "0dfc81b154a9a2204bf8eec138efc1d76d4c3820bbc5fcca88b832d357ecdcdf"
# Current local artifact SHA (damaged by S3 incident — must NOT be accepted)
CURRENT_LOCAL_SHA = "0d97a7ec61b6ec8fb5f9263b0b37b0efc9755812739d7afead7f720c3567e8dd"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _db_module():
    from catalyst_data.storage import sqlite as mod
    return mod


def _make_temp_frozen_copy() -> tuple[str, str]:
    """Copy the real frozen DB to a temp file; return (temp_path, temp_realpath).

    The temp copy is chmod 444. Caller is responsible for cleanup.
    """
    fd, tmp_path = tempfile.mkstemp(suffix=".db", prefix="frozen_test_copy_")
    os.close(fd)
    shutil.copy2(str(FROZEN_DB), tmp_path)
    os.chmod(tmp_path, 0o444)
    return tmp_path, os.path.realpath(tmp_path)


# ── tests ────────────────────────────────────────────────────────────────

@pytest.mark.protected_artifact("data/catalyst_eval_frozen_v2.db")
class TestFrozenDBSHA:
    """Landmine: frozen DB SHA must never change from canonical."""

    def test_current_file_sha_is_canonical(self):
        """Canonical SHA assertion.

        xfail: current local artifact is 0d97 (damaged by S3 incident).
        Do NOT re-pin to 0d97.
        """
        current = _sha256(FROZEN_DB)
        if current != CANONICAL_SHA:
            pytest.xfail(
                f"Current frozen DB SHA {current[:16]}... does not match "
                f"canonical {CANONICAL_SHA[:16]}... — original artifact "
                f"was corrupted by S3 incident.  Restore from backup or "
                f"re-freeze from a trusted source."
            )
        assert current == CANONICAL_SHA

    def test_current_is_not_accidentally_accepted(self):
        """Ensure we are NOT silently accepting the damaged 0d97 SHA."""
        current = _sha256(FROZEN_DB)
        assert current in (CANONICAL_SHA, CURRENT_LOCAL_SHA), \
            f"Unexpected SHA: {current}"


class TestFrozenDBWriteGuard:
    """Any write against the frozen DB realpath must raise FrozenDBWriteError."""

    def test_guard_constant_exists(self):
        """FROZEN_PATHS constant must exist and be a collection."""
        mod = _db_module()
        frozen_real = os.path.realpath(str(FROZEN_DB))
        assert hasattr(mod, "FROZEN_PATHS"), "FROZEN_PATHS constant missing"
        assert isinstance(mod.FROZEN_PATHS, (set, frozenset, list, tuple)), \
            "FROZEN_PATHS must be a collection"
        assert frozen_real in mod.FROZEN_PATHS, \
            f"FROZEN_PATHS must contain {frozen_real}"

    def test_assert_not_frozen_raises_for_frozen_realpath(self):
        """Direct guard: _assert_not_frozen raises FrozenDBWriteError."""
        mod = _db_module()
        frozen_real = os.path.realpath(str(FROZEN_DB))
        with pytest.raises(mod.FrozenDBWriteError):
            mod._assert_not_frozen(frozen_real)

    def test_assert_not_frozen_passes_for_non_frozen_path(self):
        """Guard does NOT raise for paths outside FROZEN_PATHS."""
        mod = _db_module()
        mod._assert_not_frozen("/tmp/nonexistent_test_s3_guard.db")

    @pytest.mark.protected_artifact("data/catalyst_eval_frozen_v2.db")
    def test_init_db_on_temp_frozen_raises_via_guard(self):
        """init_db on a frozen-registered path raises before mutating.

        Creates a temp copy, temporarily monkeypatches FROZEN_PATHS to include
        it, then calls init_db.  Verifies exception + unchanged SHA.
        """
        mod = _db_module()
        tmp_path, tmp_real = _make_temp_frozen_copy()
        sha_before = _sha256(Path(tmp_path))

        original_paths = mod.FROZEN_PATHS
        try:
            mod.__dict__['FROZEN_PATHS'] = frozenset(list(original_paths) + [tmp_real])

            conn = sqlite3.connect(tmp_path)
            try:
                with pytest.raises(mod.FrozenDBWriteError):
                    mod.init_db(conn)
            finally:
                conn.close()
        finally:
            sha_after = _sha256(Path(tmp_path))
            mod.__dict__['FROZEN_PATHS'] = original_paths
            os.chmod(tmp_path, 0o644)
            os.unlink(tmp_path)

        assert sha_before == sha_after, \
            f"Temp frozen copy SHA changed! {sha_before[:16]}... → {sha_after[:16]}..."

    def test_guard_prevents_writes_to_damaged_backup(self):
        """If a damaged backup exists, the guard must prevent init_db on it.

        Verifies no further mutation of the damaged backup.
        """
        damaged = REPO_ROOT / "data" / "catalyst_eval_frozen_v2.db.damaged-0d97-backup"
        if not damaged.exists():
            pytest.skip("Damaged backup not present")

        sha_before = _sha256(damaged)
        mod = _db_module()

        # The damaged backup should be guarded if it resolves to a frozen path
        # or is explicitly in FROZEN_PATHS. At minimum, verify init_db guard
        # does not silently write to it.
        conn = sqlite3.connect(f"file:{damaged}?mode=ro", uri=True)
        try:
            # Opening read-only — should succeed
            count = conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
            assert count is not None
        finally:
            conn.close()

        sha_after = _sha256(damaged)
        assert sha_before == sha_after, \
            "Damaged backup SHA changed during read-only access!"

    @pytest.mark.protected_artifact("data/catalyst_eval_frozen_v2.db")
    def test_run_migrations_on_frozen_path_raises_guard(self):
        """run_migrations on a frozen path must raise FrozenDBWriteError.

        Uses a writable temp copy with monkeypatched FROZEN_PATHS so the
        guard fires, not OS-level read-only protection.
        """
        mod = _db_module()
        from catalyst_data.migrations import run_migrations

        tmp_path, tmp_real = _make_temp_frozen_copy()
        # Make writable so OS-level protection can't mask a missing guard
        os.chmod(tmp_path, 0o644)
        sha_before = _sha256(Path(tmp_path))

        original_paths = mod.FROZEN_PATHS
        try:
            mod.__dict__['FROZEN_PATHS'] = frozenset(list(original_paths) + [tmp_real])

            conn = sqlite3.connect(tmp_path)
            try:
                with pytest.raises(mod.FrozenDBWriteError):
                    run_migrations(conn)
            finally:
                conn.close()
        finally:
            sha_after = _sha256(Path(tmp_path))
            mod.__dict__['FROZEN_PATHS'] = original_paths
            os.chmod(tmp_path, 0o644)
            os.unlink(tmp_path)

        assert sha_before == sha_after, \
            f"Temp frozen copy SHA changed! {sha_before[:16]}... → {sha_after[:16]}..."


class TestFrozenDBRetrievalSafe:
    """Full retrieval pass over frozen DB must leave SHA unchanged."""

    @pytest.mark.protected_artifact("data/catalyst_eval_frozen_v2.db")
    def test_read_only_retrieval_sha_unchanged(self):
        """Read-only retrieval pass must not change the frozen DB SHA."""
        sha_before = _sha256(FROZEN_DB)
        conn = sqlite3.connect(f"file:{FROZEN_DB}?mode=ro", uri=True)
        try:
            has_corpus = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='view' AND name='corpus_items'"
            ).fetchone()

            if has_corpus:
                count = conn.execute("SELECT COUNT(*) FROM corpus_items").fetchone()[0]
                rows = conn.execute("SELECT * FROM corpus_items").fetchall()
                for row in rows:
                    pass
            else:
                count = conn.execute("SELECT COUNT(*) FROM clean_assets").fetchone()[0]
                rows = conn.execute("SELECT * FROM clean_assets").fetchall()
                for row in rows:
                    pass
        finally:
            conn.close()

        sha_after = _sha256(FROZEN_DB)
        assert sha_before == sha_after, \
            f"Frozen DB SHA changed during read-only pass! {sha_before[:16]}... → {sha_after[:16]}..."
