from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from catalyst_data.ingestion.ops import daily_jobs


def test_open_jobs_creates_job_tables(tmp_path: Path) -> None:
    conn = daily_jobs.open_jobs(tmp_path / "jobs.sqlite3")
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert {"jobs", "job_items", "job_assets"} <= tables
    finally:
        conn.close()


def test_acquire_lease_clears_dead_holder_and_claims_job(tmp_path: Path) -> None:
    conn = daily_jobs.open_jobs(tmp_path / "jobs.sqlite3")
    try:
        conn.execute(
            "INSERT INTO jobs (job_id,plan_key,plan_json,status,created_at,lease_owner,lease_expires_at) "
            "VALUES ('job','key','{}','RUNNING','2026-01-01T00:00:00Z','dead:1','2099-01-01T00:00:00Z')"
        )
        assert daily_jobs.acquire_lease(
            conn, "job", "runner:2", now="2026-01-01T00:00:00Z",
            lease_seconds=60, owner_dead=lambda holder: holder == "dead:1",
        )
        owner, status = conn.execute(
            "SELECT lease_owner,status FROM jobs WHERE job_id='job'"
        ).fetchone()
        assert (owner, status) == ("runner:2", "RUNNING")
    finally:
        conn.close()


def test_acquire_lease_uses_injected_time_and_rejects_missing_job(tmp_path: Path) -> None:
    conn = daily_jobs.open_jobs(tmp_path / "jobs.sqlite3")
    try:
        conn.execute(
            "INSERT INTO jobs (job_id,plan_key,plan_json,status,created_at) "
            "VALUES ('job','key','{}','PENDING','2026-01-01T00:00:00Z')"
        )
        assert daily_jobs.acquire_lease(
            conn, "job", "runner:2", now="2026-01-01T00:00:00Z", lease_seconds=60,
            owner_dead=lambda _holder: False,
        )
        assert conn.execute(
            "SELECT lease_expires_at FROM jobs WHERE job_id='job'"
        ).fetchone()[0] == "2026-01-01T00:01:00Z"
        assert not daily_jobs.acquire_lease(
            conn, "missing", "runner:2", now="2026-01-01T00:00:00Z", lease_seconds=60,
            owner_dead=lambda _holder: False,
        )
    finally:
        conn.close()


def test_acquire_lease_blocks_another_job_owned_by_same_runner(tmp_path: Path) -> None:
    conn = daily_jobs.open_jobs(tmp_path / "jobs.sqlite3")
    try:
        conn.executemany(
            "INSERT INTO jobs (job_id,plan_key,plan_json,status,created_at) "
            "VALUES (?, 'key', '{}', 'PENDING', '2026-01-01T00:00:00Z')",
            [("first",), ("second",)],
        )
        assert daily_jobs.acquire_lease(
            conn, "first", "runner:1", now="2026-01-01T00:00:00Z", lease_seconds=60,
            owner_dead=lambda _holder: False,
        )
        assert not daily_jobs.acquire_lease(
            conn, "second", "runner:1", now="2026-01-01T00:00:00Z", lease_seconds=60,
            owner_dead=lambda _holder: False,
        )
        assert conn.execute(
            "SELECT lease_owner FROM jobs WHERE job_id='second'"
        ).fetchone()[0] is None
    finally:
        conn.close()


def test_acquire_lease_rejects_same_job_and_same_owner_reentry(tmp_path: Path) -> None:
    conn = daily_jobs.open_jobs(tmp_path / "jobs.sqlite3")
    try:
        conn.execute(
            "INSERT INTO jobs (job_id,plan_key,plan_json,status,created_at) "
            "VALUES ('job','key','{}','PENDING','2026-01-01T00:00:00Z')"
        )
        assert daily_jobs.acquire_lease(
            conn, "job", "runner:1", now="2026-01-01T00:00:00Z", lease_seconds=60,
            owner_dead=lambda _holder: False,
        )
        assert not daily_jobs.acquire_lease(
            conn, "job", "runner:1", now="2026-01-01T00:00:01Z", lease_seconds=60,
            owner_dead=lambda _holder: False,
        )
        assert conn.execute(
            "SELECT lease_owner FROM jobs WHERE job_id='job'"
        ).fetchone() == ("runner:1",)
    finally:
        conn.close()


def test_renew_lease_fails_after_takeover_and_preserves_new_owner(tmp_path: Path) -> None:
    conn = daily_jobs.open_jobs(tmp_path / "jobs.sqlite3")
    try:
        conn.execute(
            "INSERT INTO jobs (job_id,plan_key,plan_json,status,created_at,lease_owner,lease_expires_at) "
            "VALUES ('job','key','{}','RUNNING','2026-01-01T00:00:00Z','old:1','2099-01-01T00:00:00Z')"
        )
        assert daily_jobs.acquire_lease(
            conn, "job", "new:2", now="2098-12-31T23:59:00Z", lease_seconds=120,
            owner_dead=lambda holder: holder == "old:1",
        )
        with pytest.raises(RuntimeError, match="lease was lost"):
            daily_jobs.renew_lease(conn, "job", "old:1", lease_seconds=60)
        assert conn.execute(
            "SELECT lease_owner FROM jobs WHERE job_id='job'"
        ).fetchone() == ("new:2",)
    finally:
        conn.close()


def test_acquire_lease_reserves_writer_before_checking_other_jobs(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.sqlite3"
    first = daily_jobs.open_jobs(db_path)
    second = daily_jobs.open_jobs(db_path)
    try:
        first.executemany(
            "INSERT INTO jobs (job_id,plan_key,plan_json,status,created_at) "
            "VALUES (?, 'key', '{}', 'PENDING', '2026-01-01T00:00:00Z')",
            [("first",), ("second",)],
        )
        first.commit()
        first.execute("BEGIN IMMEDIATE")
        first.execute(
            "UPDATE jobs SET lease_owner='runner:1', lease_expires_at='2026-01-01T00:15:00Z' "
            "WHERE job_id='first'"
        )
        second.execute("PRAGMA busy_timeout=0")

        with pytest.raises(sqlite3.OperationalError, match="locked"):
            daily_jobs.acquire_lease(
                second, "second", "runner:2", now="2026-01-01T00:00:00Z", lease_seconds=60,
                owner_dead=lambda _holder: False,
            )

        first.commit()
        assert not daily_jobs.acquire_lease(
            second, "second", "runner:2", now="2026-01-01T00:00:00Z", lease_seconds=60,
            owner_dead=lambda _holder: False,
        )
        assert second.execute(
            "SELECT lease_owner FROM jobs WHERE job_id='second'"
        ).fetchone()[0] is None
    finally:
        if first.in_transaction:
            first.rollback()
        first.close()
        second.close()


def test_acquire_lease_rolls_back_when_owner_check_raises(tmp_path: Path) -> None:
    conn = daily_jobs.open_jobs(tmp_path / "jobs.sqlite3")
    try:
        conn.executemany(
            "INSERT INTO jobs (job_id,plan_key,plan_json,status,created_at,lease_owner,lease_expires_at) "
            "VALUES (?, 'key', '{}', 'RUNNING', '2026-01-01T00:00:00Z', ?, '2026-01-01T00:15:00Z')",
            [("first", "dead:1"), ("second", "dead:2"), ("target", None)],
        )
        calls = 0

        def owner_dead(_holder: str) -> bool:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("owner check failed")
            return True

        with pytest.raises(RuntimeError, match="owner check failed"):
            daily_jobs.acquire_lease(
                conn, "target", "runner:3", now="2026-01-01T00:00:00Z", lease_seconds=60,
                owner_dead=owner_dead,
            )

        assert not conn.in_transaction
        assert conn.execute(
            "SELECT lease_owner FROM jobs WHERE job_id IN ('first','second') ORDER BY job_id"
        ).fetchall() == [("dead:1",), ("dead:2",)]
    finally:
        conn.close()
