from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from catalyst_data.migrations import run_migrations


class ResourceLimitError(RuntimeError):
    pass


@dataclass(frozen=True)
class BootstrapResult:
    candidate_path: Path
    source_sha256_before: str
    source_sha256_after: str
    user_version: int


@dataclass(frozen=True)
class PromotionResult:
    final_path: Path
    active_pointer_path: Path
    snapshot_id: str
    final_sha256: str


@dataclass(frozen=True)
class ResourceEstimate:
    source_utf8_bytes: int
    eligible_document_count: int
    estimated_chunks: int
    estimated_peak_bytes: int
    required_headroom: int
    largest_source_document_utf8_bytes: int = 0
    phase_headroom_bytes: dict[str, int] | None = None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _fsync_path(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_parent(path: Path) -> None:
    _fsync_path(path.parent)


def _check_free_space(target_dir: Path, required_bytes: int) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(target_dir).free
    if free < required_bytes:
        raise ResourceLimitError(f"free disk {free} < required {required_bytes}")


def bootstrap_candidate(
    *,
    source_path: Path,
    expected_source_sha256: str,
    candidate_path: Path,
    min_free_bytes: int | None = None,
) -> BootstrapResult:
    source_path = Path(source_path)
    candidate_path = Path(candidate_path)
    if candidate_path.exists():
        raise FileExistsError(candidate_path)
    required = min_free_bytes if min_free_bytes is not None else max(10 * 1024**3, 3 * source_path.stat().st_size)
    _check_free_space(candidate_path.parent, required)
    before = sha256_file(source_path)
    if before != expected_source_sha256:
        raise ValueError("protected source SHA does not match expected_source_sha256")
    tmp_path = candidate_path.with_suffix(candidate_path.suffix + ".tmp")
    if tmp_path.exists():
        tmp_path.unlink()
    try:
        ro_uri = f"{source_path.resolve().as_uri()}?mode=ro"
        source_conn = sqlite3.connect(ro_uri, uri=True)
        dest_conn = sqlite3.connect(tmp_path)
        try:
            source_conn.backup(dest_conn)
            dest_conn.commit()
        finally:
            dest_conn.close()
            source_conn.close()
        _fsync_path(tmp_path)
        _fsync_parent(tmp_path)
        conn = sqlite3.connect(tmp_path)
        try:
            from catalyst_data.migrations import CURRENT_SCHEMA_VERSION
            run_migrations(conn)
            user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if user_version != CURRENT_SCHEMA_VERSION:
                raise RuntimeError(
                    f"candidate reached user_version={user_version}, "
                    f"expected {CURRENT_SCHEMA_VERSION}"
                )
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"integrity_check failed: {integrity}")
            fk_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
            if fk_rows:
                raise RuntimeError(f"foreign_key_check failed: {fk_rows[:3]}")
            conn.commit()
        finally:
            conn.close()
        after = sha256_file(source_path)
        if after != before:
            raise RuntimeError("protected source DB SHA changed during bootstrap")
        os.replace(tmp_path, candidate_path)
        _fsync_path(candidate_path)
        _fsync_parent(candidate_path)
        return BootstrapResult(candidate_path, before, after, user_version)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise


def promote_candidate(
    *,
    candidate_path: Path,
    snapshot_id: str,
    snapshots_dir: Path,
    active_pointer_path: Path,
) -> PromotionResult:
    candidate_path = Path(candidate_path)
    snapshots_dir = Path(snapshots_dir)
    active_pointer_path = Path(active_pointer_path)
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    active_pointer_path.parent.mkdir(parents=True, exist_ok=True)
    final_path = snapshots_dir / f"catalyst_b2o_{snapshot_id}.db"
    if final_path.exists():
        raise FileExistsError(final_path)
    if candidate_path.resolve() == final_path.resolve():
        raise FileExistsError(final_path)
    os.replace(candidate_path, final_path)
    _fsync_path(final_path)
    _fsync_parent(final_path)
    digest = sha256_file(final_path)
    pointer_tmp = active_pointer_path.with_suffix(active_pointer_path.suffix + ".tmp")
    pointer_tmp.write_text(
        json.dumps(
            {"schema_version": "1.0.0", "snapshot_id": snapshot_id, "db_path": str(final_path.resolve()), "sha256": digest},
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    _fsync_path(pointer_tmp)
    os.replace(pointer_tmp, active_pointer_path)
    _fsync_parent(active_pointer_path)
    return PromotionResult(final_path, active_pointer_path, snapshot_id, digest)


def publish_corpus_with_resource_gate(
    conn: sqlite3.Connection,
    *,
    snapshot_id: str,
    clock: Callable[[], str],
    min_free_bytes: int | None = None,
    max_rss_bytes: int = 6 * 1024**3,
    current_rss_bytes: int | None = None,
    free_disk_bytes: int | None = None,
    protected_db_size: int | None = None,
):
    """Run the historical list-returning corpus and lexical publication API."""
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    db_size = protected_db_size
    if db_size is None and db_path:
        db_size = Path(db_path).stat().st_size
    estimate = estimate_publication_resources(conn)
    if free_disk_bytes is None:
        target = Path(db_path).parent if db_path else Path(".")
        free_disk_bytes = shutil.disk_usage(target).free
    if current_rss_bytes is None:
        current_rss_bytes = _current_rss_bytes()
    check_publication_resources(
        estimate,
        current_rss_bytes=current_rss_bytes,
        free_disk_bytes=free_disk_bytes,
        protected_db_size=db_size or 0,
        max_rss_bytes=max_rss_bytes,
    )
    if min_free_bytes is not None:
        _check_free_space(Path(db_path).parent if db_path else Path("."), min_free_bytes)
    from catalyst_data.index_builder import build_corpus_and_lexical_index

    return build_corpus_and_lexical_index(
        conn,
        certified_snapshot_identity=snapshot_id,
        clock=clock,
    )


def estimate_publication_resources(conn: sqlite3.Connection) -> ResourceEstimate:
    """Estimate resources using the historical legacy publication model."""
    docs: list[int] = []
    article_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='articles'"
    ).fetchone()
    if article_exists:
        for row in conn.execute(
            """SELECT COALESCE(title, '') || char(10) || COALESCE(description, '')
               FROM articles"""
        ):
            docs.append(len((row[0] or "").encode("utf-8")))
    filing_docs_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='filing_documents'"
    ).fetchone()
    if filing_docs_exists:
        for row in conn.execute(
            """SELECT COALESCE(text, '') FROM filing_documents
               WHERE COALESCE(extraction_status, 'success') = 'success'"""
        ):
            docs.append(len((row[0] or "").encode("utf-8")))
    source_bytes = sum(docs)
    chunks = sum(max(1, math.ceil(size / 320)) for size in docs)
    peak = source_bytes * 8 + len(docs) * 4096 + chunks * 8192
    headroom = peak + 512 * 1024**2
    return ResourceEstimate(
        source_utf8_bytes=source_bytes,
        eligible_document_count=len(docs),
        estimated_chunks=chunks,
        estimated_peak_bytes=peak,
        required_headroom=headroom,
    )


def publish_streaming_corpus_with_resource_gate(
    conn: sqlite3.Connection,
    *,
    snapshot_id: str,
    clock: Callable[[], str],
    min_free_bytes: int | None = None,
    max_rss_bytes: int = 6 * 1024**3,
    current_rss_bytes: int | None = None,
    free_disk_bytes: int | None = None,
    protected_db_size: int | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    progress_interval: float = 30.0,
):
    """Run the bounded streaming publication API used by Pre-B6 only."""
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    db_size = protected_db_size
    if db_size is None and db_path:
        db_size = Path(db_path).stat().st_size
    from catalyst_data.corpus.streaming_publication import (
        estimate_streaming_publication_resources,
    )

    streaming_estimate = estimate_streaming_publication_resources(conn)
    estimate = ResourceEstimate(
        source_utf8_bytes=streaming_estimate.source_utf8_bytes,
        eligible_document_count=streaming_estimate.eligible_document_count,
        estimated_chunks=streaming_estimate.estimated_chunks,
        estimated_peak_bytes=streaming_estimate.estimated_peak_bytes,
        required_headroom=streaming_estimate.required_headroom,
        largest_source_document_utf8_bytes=(
            streaming_estimate.largest_source_document_utf8_bytes
        ),
        phase_headroom_bytes=streaming_estimate.phase_headroom_bytes,
    )
    if free_disk_bytes is None:
        target = Path(db_path).parent if db_path else Path(".")
        free_disk_bytes = shutil.disk_usage(target).free
    if current_rss_bytes is None:
        current_rss_bytes = _current_rss_bytes()
    check_publication_resources(
        estimate,
        current_rss_bytes=current_rss_bytes,
        free_disk_bytes=free_disk_bytes,
        protected_db_size=db_size or 0,
        max_rss_bytes=max_rss_bytes,
    )
    if min_free_bytes is not None:
        _check_free_space(Path(db_path).parent if db_path else Path("."), min_free_bytes)
    from catalyst_data.corpus.streaming_publication import (
        build_streaming_corpus_and_lexical_index,
    )

    return build_streaming_corpus_and_lexical_index(
        conn,
        certified_snapshot_identity=snapshot_id,
        clock=clock,
        progress_callback=progress_callback,
        progress_interval=progress_interval,
    )


def check_publication_resources(
    estimate: ResourceEstimate,
    *,
    current_rss_bytes: int,
    free_disk_bytes: int,
    protected_db_size: int,
    max_rss_bytes: int = 6 * 1024**3,
) -> None:
    required_disk = max(10 * 1024**3, 3 * protected_db_size)
    if free_disk_bytes < required_disk:
        raise ResourceLimitError(f"free disk {free_disk_bytes} < required {required_disk}")
    if current_rss_bytes + estimate.required_headroom >= max_rss_bytes:
        raise ResourceLimitError(
            f"RSS plus required headroom {current_rss_bytes + estimate.required_headroom} >= limit {max_rss_bytes}"
        )


def _current_rss_bytes() -> int:
    from catalyst_data.corpus.streaming_publication import _current_rss_bytes as current

    return current()
