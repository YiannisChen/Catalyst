"""Best-effort publication of the daily corpus lexical artifact."""

from __future__ import annotations

import json
import math
import shutil
import sqlite3
from pathlib import Path
from typing import Callable

from catalyst_data.storage.connect import connect_readonly


def current_certified_snapshot_identity(conn: sqlite3.Connection) -> tuple[str, str]:
    row = conn.execute(
        """SELECT b.build_id,b.header_json
           FROM corpus_manifest m JOIN corpus_publication_builds b
             ON b.manifest_id=m.manifest_id AND b.status='published'
           WHERE m.is_current=1
           ORDER BY b.published_at DESC,b.created_at DESC,b.build_id COLLATE BINARY DESC
           LIMIT 1"""
    ).fetchone()
    if row is None:
        raise RuntimeError("served corpus has no current published build")
    header_value = row[1].decode("utf-8") if isinstance(row[1], bytes) else row[1]
    header = json.loads(header_value)
    identity = header.get("certified_snapshot_identity")
    if not isinstance(identity, str) or not identity:
        raise RuntimeError("current published build has no certified snapshot identity")
    return str(row[0]), identity


def sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def generation_payload_bytes(conn: sqlite3.Connection, build_id: str) -> int:
    chunk_bytes = conn.execute(
        "SELECT COALESCE(SUM(length(CAST(content_text AS BLOB))),0) "
        "FROM corpus_build_chunks WHERE build_id=?", (build_id,)
    ).fetchone()[0]
    fts_bytes = conn.execute(
        "SELECT COALESCE(SUM(length(CAST(content_text AS BLOB))),0) "
        "FROM corpus_build_chunks_fts WHERE build_id=?", (build_id,)
    ).fetchone()[0]
    return int(chunk_bytes or 0) + int(fts_bytes or 0)


def build_corpus_fts_best_effort(
    paths,
    log: Callable[[str], None],
    *,
    safe_log: Callable,
    build_locked: Callable,
) -> dict:
    if not paths.corpus_fts_rebuild_enabled:
        return {"status": "skipped", "reason_code": "disabled"}
    corpus_path, alpaca_path, bodies_path = paths.corpus_db, paths.alpaca_news_db, paths.bodies_db
    if corpus_path is None or alpaca_path is None:
        result = {"status": "failed", "reason_code": "not_configured",
                  "reason": "CATALYST_CORPUS_DB and CATALYST_ALPACA_NEWS_DB are required"}
        safe_log(log, {"corpus_fts": result})
        return result
    if not corpus_path.parent.is_dir():
        result = {"status": "failed", "reason_code": "missing_corpus_directory",
                  "reason": "served corpus directory does not exist"}
        safe_log(log, {"corpus_fts": result})
        return result
    if not corpus_path.is_file() or not alpaca_path.is_file() or not bodies_path.is_file():
        result = {"status": "failed", "reason_code": "missing_input",
                  "reason": "served corpus, Alpaca news archive, or CATALYST_NEWS_BODIES_DB is missing"}
        safe_log(log, {"corpus_fts": result})
        return result

    from catalyst_data.corpus.locking import acquire_corpus_lock, release_corpus_lock

    lock_fd = acquire_corpus_lock(corpus_path)
    if lock_fd is None:
        return {"status": "skipped", "reason_code": "locked"}
    try:
        return build_locked(paths, log)
    finally:
        release_corpus_lock(lock_fd)


def build_corpus_fts_locked(
    paths,
    log: Callable[[str], None],
    *,
    current_identity: Callable,
    generation_bytes_for: Callable,
    sha256: Callable,
    safe_log: Callable,
    disk_free_gb: Callable,
    now: Callable[[], str],
) -> dict:
    def emit(payload: dict) -> None:
        safe_log(log, payload)

    corpus_path, alpaca_path, bodies_path = paths.corpus_db, paths.alpaca_news_db, paths.bodies_db
    assert corpus_path is not None and alpaca_path is not None
    corpus_conn = alpaca_conn = readonly_conn = sec_bodies_conn = None
    previous_build_id = expected_build_id = prior_status = None
    free_before = required_free_bytes = generation_bytes = None
    try:
        from catalyst_data.corpus import streaming_publication as publication

        readonly_conn = sqlite3.connect(corpus_path.as_uri() + "?mode=ro", uri=True, timeout=30)
        previous_build_id, certified_identity = current_identity(readonly_conn)
        generation_bytes = generation_bytes_for(readonly_conn, previous_build_id)
        alpaca_identity = f"sha256:{sha256(alpaca_path)}"
        sec_bodies_conn = connect_readonly(bodies_path, timeout_ms=30_000, immutable=False)
        sec_bodies_identity = publication.sec_bodies_snapshot_identity(
            sec_bodies_conn, readonly_conn,
        )
        header = publication._header(certified_identity, "1.0.0")
        header["alpaca_news_snapshot_identity"] = alpaca_identity
        header.update({
            "sec_bodies_snapshot_identity": f"sha256:{sec_bodies_identity.identity}",
            "sec_bodies_included_count": sec_bodies_identity.included_count,
            "sec_bodies_excluded_count": sec_bodies_identity.excluded_count,
            "sec_bodies_duplicate_count": sec_bodies_identity.duplicate_count,
            "sec_bodies_deduplication_rule": (
                "exclude exact accession_number + document_url matches in snapshot filings/filing_documents"
            ),
        })
        expected_build_id = publication._build_id(header)
        prior_status_row = readonly_conn.execute(
            "SELECT status FROM corpus_publication_builds WHERE build_id=?", (expected_build_id,)
        ).fetchone()
        prior_status = None if prior_status_row is None else prior_status_row[0]
        readonly_conn.close()
        readonly_conn = None

        if prior_status == "published":
            return {"status": "skipped", "reason_code": "already_published", "build_id": expected_build_id}

        minimum_free_bytes = int(paths.corpus_min_free_gb * 1_000_000_000)
        required_free_bytes = math.ceil(generation_bytes * 1.2) + minimum_free_bytes
        free_before = shutil.disk_usage(corpus_path.parent).free
        if free_before < required_free_bytes:
            result = {"status": "failed", "reason_code": "low_disk",
                      "free_gb": round(free_before / 1_000_000_000, 3),
                      "generation_bytes": generation_bytes,
                      "required_free_bytes": required_free_bytes,
                      "minimum_free_gb": paths.corpus_min_free_gb,
                      "reason": "available disk space is below prior generation size plus safety margin"}
            emit({"corpus_fts": result})
            return result

        alpaca_conn = sqlite3.connect(alpaca_path.as_uri() + "?mode=ro", uri=True, timeout=30)
        corpus_conn = sqlite3.connect(corpus_path, timeout=60)
        result = publication.build_streaming_corpus_and_lexical_index(
            corpus_conn,
            certified_snapshot_identity=certified_identity,
            alpaca_news_conn=alpaca_conn,
            alpaca_source_snapshot_identity=alpaca_identity,
            sec_bodies_conn=sec_bodies_conn,
            sec_bodies_identity=sec_bodies_identity,
            clock=now,
            progress_callback=lambda event: emit({"corpus_fts_progress": event}),
        )
        if result.build_id != expected_build_id:
            raise RuntimeError("corpus builder returned an unexpected build id")
        published = corpus_conn.execute(
            "SELECT status FROM corpus_publication_builds WHERE build_id=?", (result.build_id,)
        ).fetchone()
        if published is None or published[0] != "published":
            raise RuntimeError("corpus builder returned before publishing the new build")
        selected_build_id, _ = current_identity(corpus_conn)
        if selected_build_id != result.build_id:
            return {"status": "skipped", "reason_code": "not_current", "build_id": result.build_id,
                    "current_build_id": selected_build_id}
        from catalyst_data.corpus.retirement import prune_corpus_builds
        retired = prune_corpus_builds(corpus_conn, previous_current_build_id=previous_build_id)
        if paths.corpus_vacuum_after_prune:
            corpus_conn.execute("VACUUM")
        return {"status": "ok", "build_id": result.build_id,
                "document_count": result.corpus.document_count,
                "chunk_count": result.corpus.chunk_count,
                "sec_bodies_included_count": getattr(result, "sec_bodies_included_count", 0),
                "sec_bodies_excluded_count": getattr(result, "sec_bodies_excluded_count", 0),
                "sec_bodies_duplicate_count": getattr(result, "sec_bodies_duplicate_count", 0),
                "lexical_mode": result.lexical.mode_served,
                "lexical_row_count": result.lexical.row_count,
                "previous_build_id": previous_build_id,
                "retired_build_ids": retired,
                "generation_bytes": generation_bytes,
                "free_gb_before": round(free_before / 1_000_000_000, 3),
                "free_gb_after": disk_free_gb(corpus_path.parent)}
    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[:300]}"
        if (corpus_conn is not None and previous_build_id is not None and expected_build_id is not None
                and prior_status != "published"):
            try:
                current_build_id, _ = current_identity(corpus_conn)
                if current_build_id == expected_build_id and current_build_id != previous_build_id:
                    corpus_conn.rollback()
                    from catalyst_data.corpus.rollback import rollback_database
                    rollback_database(corpus_path, to_build_id=previous_build_id,
                                      expected_current_build_id=expected_build_id, lock_held=True)
            except Exception as rollback_exc:
                error += f"; current-build restore failed: {type(rollback_exc).__name__}: {str(rollback_exc)[:200]}"
            try:
                from catalyst_data.corpus.retirement import purge_incomplete_builds
                purge_incomplete_builds(corpus_conn)
            except Exception as cleanup_exc:
                error += f"; staging cleanup failed: {type(cleanup_exc).__name__}: {str(cleanup_exc)[:200]}"
        result = {"status": "failed", "reason_code": "build_failed", "reason": error,
                  "generation_bytes": generation_bytes, "required_free_bytes": required_free_bytes,
                  "free_gb_before": None if free_before is None else round(free_before / 1_000_000_000, 3),
                  "free_gb_after": disk_free_gb(corpus_path.parent)}
        emit({"corpus_fts": result})
        return result
    finally:
        if readonly_conn is not None:
            readonly_conn.close()
        if sec_bodies_conn is not None:
            sec_bodies_conn.close()
        if alpaca_conn is not None:
            alpaca_conn.close()
        if corpus_conn is not None:
            corpus_conn.close()
