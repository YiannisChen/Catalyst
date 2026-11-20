"""Bounded, resumable inactive FTS construction for streaming publication."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any


class ResumableResourceStop(RuntimeError):
    """A bounded publication stopped without invalidating committed staging."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.resumable = True
        super().__init__(f"[{code}] {message}")


@dataclass(frozen=True)
class StreamingLexicalResult:
    manifest_id: str
    mode_served: str
    row_count: int
    digest: str


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _update_lexical_digest(digest: Any, chunk_id: object, content_text: object) -> None:
    digest.update(_canonical_bytes([str(chunk_id), str(content_text)]))
    digest.update(b"\n")


def _lexical_source_digest(conn: sqlite3.Connection, build_id: str) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    cursor = conn.execute(
        """SELECT chunk_id, content_text FROM corpus_build_chunks
           WHERE build_id=? AND eligibility='eligible'
             AND status IN ('active','pending_embedding','embedded','metadata_only')
           ORDER BY chunk_id COLLATE BINARY""",
        (build_id,),
    )
    for chunk_id, content_text in cursor:
        _update_lexical_digest(digest, chunk_id, content_text)
        count += 1
    return count, digest.hexdigest()


def _persisted_lexical_digest(
    conn: sqlite3.Connection, build_id: str
) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    cursor = conn.execute(
        """SELECT chunk_id, content_text FROM corpus_build_chunks_fts
           WHERE build_id=? ORDER BY chunk_id COLLATE BINARY""",
        (build_id,),
    )
    for chunk_id, content_text in cursor:
        _update_lexical_digest(digest, chunk_id, content_text)
        count += 1
    return count, digest.hexdigest()


def _lexical_rows_digest(rows: Iterable[tuple[object, object]]) -> str:
    digest = hashlib.sha256()
    for chunk_id, content_text in rows:
        _update_lexical_digest(digest, chunk_id, content_text)
    return digest.hexdigest()


def _iter_cursor_rows(cursor: Any, *, page_size: int = 500) -> Iterator[Any]:
    if not 1 <= page_size <= 500:
        raise ValueError("page_size must be between 1 and 500")
    while True:
        page = cursor.fetchmany(page_size)
        if not page:
            return
        yield from page


def _validated_fts_checkpoint(
    conn: sqlite3.Connection, build_id: str
) -> tuple[int, str | None]:
    valid_batches = 0
    checkpoint: str | None = None
    batches = conn.execute(
        """SELECT batch_no, row_count, text_utf8_bytes, first_chunk_id,
                  last_chunk_id, batch_digest, checkpoint_chunk_id
           FROM corpus_build_fts_batches
           WHERE build_id=? ORDER BY batch_no""",
        (build_id,),
    )
    for batch in _iter_cursor_rows(batches):
        batch_no, row_count, text_bytes, first_chunk_id, last_chunk_id = batch[:5]
        batch_digest, batch_checkpoint = batch[5:]
        if batch_no != valid_batches + 1 or batch_checkpoint != last_chunk_id:
            break
        if not 1 <= int(row_count) <= 500:
            break
        if checkpoint is not None and str(first_chunk_id).encode("utf-8") <= checkpoint.encode("utf-8"):
            break
        lower_sql = "" if checkpoint is None else "AND chunk_id > ? COLLATE BINARY"
        params: list[object] = [build_id]
        if checkpoint is not None:
            params.append(checkpoint)
        params.append(batch_checkpoint)
        persisted_cursor = conn.execute(
            f"""SELECT chunk_id, content_text FROM corpus_build_chunks_fts
                WHERE build_id=? {lower_sql}
                  AND chunk_id <= ? COLLATE BINARY
                ORDER BY chunk_id COLLATE BINARY""",
            params,
        )
        source_cursor = conn.execute(
            f"""SELECT chunk_id, content_text FROM corpus_build_chunks
                WHERE build_id=? AND eligibility='eligible'
                  AND status IN ('active','pending_embedding','embedded','metadata_only')
                  {lower_sql} AND chunk_id <= ? COLLATE BINARY
                ORDER BY chunk_id COLLATE BINARY""",
            params,
        )
        persisted_rows = _iter_cursor_rows(persisted_cursor)
        source_rows = _iter_cursor_rows(source_cursor)
        digest = hashlib.sha256()
        observed_count = 0
        observed_text_bytes = 0
        observed_first: object | None = None
        observed_last: object | None = None
        rows_match = True
        while True:
            persisted_row = next(persisted_rows, None)
            source_row = next(source_rows, None)
            if persisted_row is None and source_row is None:
                break
            if persisted_row is None or source_row is None or persisted_row != source_row:
                rows_match = False
                break
            if observed_first is None:
                observed_first = persisted_row[0]
            observed_last = persisted_row[0]
            observed_count += 1
            observed_text_bytes += len(str(persisted_row[1]).encode("utf-8"))
            _update_lexical_digest(digest, persisted_row[0], persisted_row[1])
        if (
            not rows_match
            or observed_count != int(row_count)
            or observed_text_bytes != int(text_bytes)
            or observed_first != first_chunk_id
            or observed_last != last_chunk_id
            or digest.hexdigest() != batch_digest
        ):
            break
        valid_batches += 1
        checkpoint = str(batch_checkpoint)
    return valid_batches, checkpoint


def _discard_invalid_fts_suffix(
    conn: sqlite3.Connection,
    *,
    build_id: str,
    valid_batches: int,
    checkpoint: str | None,
    failure_injector: Callable[[str], None] | None,
) -> None:
    repair_state = conn.execute(
        """SELECT lexical_repair_checkpoint, lexical_repair_cursor
           FROM corpus_publication_builds WHERE build_id=?""",
        (build_id,),
    ).fetchone()
    stored_checkpoint = repair_state[0] if repair_state is not None else None
    cursor = repair_state[1] if repair_state is not None else None
    if stored_checkpoint != checkpoint or cursor is None:
        predicate = "" if checkpoint is None else "AND chunk_id > ? COLLATE BINARY"
        params: tuple[object, ...] = (
            (build_id,) if checkpoint is None else (build_id, checkpoint)
        )
        cursor_row = conn.execute(
            f"""SELECT MAX(chunk_id COLLATE BINARY)
                FROM corpus_build_chunks_fts
                WHERE build_id=? {predicate}""",
            params,
        ).fetchone()
        cursor = None if cursor_row is None else cursor_row[0]
        with conn:
            conn.execute(
                """UPDATE corpus_publication_builds
                   SET lexical_repair_checkpoint=?, lexical_repair_cursor=?
                   WHERE build_id=?""",
                (checkpoint, cursor, build_id),
            )

    while cursor is not None:
        predicate = "" if checkpoint is None else "AND chunk_id > ? COLLATE BINARY"
        params: list[object] = [build_id]
        if checkpoint is not None:
            params.append(checkpoint)
        params.append(cursor)
        rows = conn.execute(
            f"""SELECT rowid, chunk_id FROM corpus_build_chunks_fts
                WHERE build_id=? {predicate}
                  AND chunk_id <= ? COLLATE BINARY
                ORDER BY chunk_id COLLATE BINARY DESC
                LIMIT 500""",
            params,
        ).fetchmany(500)
        if not rows:
            cursor = None
            break
        rowids = [int(row[0]) for row in rows]
        placeholders = ",".join("?" for _ in rowids)
        next_cursor = str(rows[-1][1])
        with conn:
            conn.execute(
                f"DELETE FROM corpus_build_chunks_fts WHERE rowid IN ({placeholders})",
                rowids,
            )
            has_more_params: list[object] = [build_id]
            if checkpoint is not None:
                has_more_params.append(checkpoint)
            has_more_params.append(next_cursor)
            has_more = conn.execute(
                f"""SELECT 1 FROM corpus_build_chunks_fts
                    WHERE build_id=? {predicate}
                      AND chunk_id < ? COLLATE BINARY LIMIT 1""",
                has_more_params,
            ).fetchone()
            cursor = next_cursor if has_more is not None else None
            conn.execute(
                """UPDATE corpus_publication_builds
                   SET lexical_repair_cursor=? WHERE build_id=?""",
                (cursor, build_id),
            )
        if failure_injector is not None:
            failure_injector("after_fts_cleanup_batch")

    while True:
        batch_rows = conn.execute(
            """SELECT rowid FROM corpus_build_fts_batches
               WHERE build_id=? AND batch_no>? ORDER BY batch_no LIMIT 500""",
            (build_id, valid_batches),
        ).fetchmany(500)
        if not batch_rows:
            break
        rowids = [int(row[0]) for row in batch_rows]
        placeholders = ",".join("?" for _ in rowids)
        with conn:
            conn.execute(
                f"DELETE FROM corpus_build_fts_batches WHERE rowid IN ({placeholders})",
                rowids,
            )
    with conn:
        conn.execute(
            """UPDATE corpus_publication_builds
               SET lexical_repair_checkpoint=NULL, lexical_repair_cursor=NULL
               WHERE build_id=?""",
            (build_id,),
        )


def _fts_phase(
    conn: sqlite3.Connection,
    *,
    build_id: str,
    manifest_id: str,
    limits: Any,
    now: str,
    failure_injector: Callable[[str], None] | None,
    stats: Any,
) -> StreamingLexicalResult:
    fts_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='corpus_build_chunks_fts'"
    ).fetchone()
    if fts_exists is None:
        raise RuntimeError("FTS5 table corpus_build_chunks_fts is unavailable")
    expected_count, expected_digest = _lexical_source_digest(conn, build_id)
    valid_batches, checkpoint = _validated_fts_checkpoint(conn, build_id)
    _discard_invalid_fts_suffix(
        conn,
        build_id=build_id,
        valid_batches=valid_batches,
        checkpoint=checkpoint,
        failure_injector=failure_injector,
    )
    with conn:
        conn.execute(
            """UPDATE corpus_publication_builds
               SET lexical_ready=0, lexical_expected_digest=?, lexical_digest=NULL,
                   lexical_row_count=0, updated_at=? WHERE build_id=?""",
            (expected_digest, now, build_id),
        )

    checkpoint_predicate = "" if checkpoint is None else "AND chunk_id > ? COLLATE BINARY"
    cursor_params: tuple[object, ...] = (
        (build_id,) if checkpoint is None else (build_id, checkpoint)
    )
    cursor = conn.execute(
        f"""SELECT chunk_id, content_text, content_hash
           FROM corpus_build_chunks
           WHERE build_id=? AND eligibility='eligible'
             AND status IN ('active','pending_embedding','embedded','metadata_only')
             {checkpoint_predicate}
           ORDER BY chunk_id COLLATE BINARY""",
        cursor_params,
    )
    batch_no = valid_batches

    def commit_batch(rows: list[tuple[object, ...]], text_utf8_bytes: int) -> None:
        nonlocal batch_no
        if not rows:
            return
        batch_no += 1
        with conn:
            conn.executemany(
                """INSERT INTO corpus_build_chunks_fts
                   (build_id, chunk_id, content_text) VALUES (?, ?, ?)""",
                ((build_id, row[0], row[1]) for row in rows),
            )
            conn.execute(
                """INSERT INTO corpus_build_fts_batches
                   (build_id, batch_no, row_count, text_utf8_bytes,
                    first_chunk_id, last_chunk_id, batch_digest,
                    checkpoint_chunk_id, committed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    build_id,
                    batch_no,
                    len(rows),
                    text_utf8_bytes,
                    rows[0][0],
                    rows[-1][0],
                    _lexical_rows_digest(
                        (row[0], row[1]) for row in rows
                    ),
                    rows[-1][0],
                    now,
                ),
            )
        if failure_injector is not None:
            failure_injector("after_fts_batch")

    fts_batch: list[tuple[object, ...]] = []
    fts_batch_bytes = 0
    for row in cursor:
        text_bytes = len(str(row[1]).encode("utf-8"))
        if text_bytes > limits.chunk_text_utf8_bytes:
            raise ResumableResourceStop(
                "fts_chunk_text_too_large",
                f"{row[0]} requires {text_bytes} UTF-8 bytes; "
                f"budget is {limits.chunk_text_utf8_bytes}",
            )
        if fts_batch and (
            len(fts_batch) >= limits.max_chunks
            or fts_batch_bytes + text_bytes > limits.chunk_text_utf8_bytes
        ):
            commit_batch(fts_batch, fts_batch_bytes)
            fts_batch.clear()
            fts_batch_bytes = 0
        fts_batch.append(tuple(row))
        fts_batch_bytes += text_bytes
        stats.peak_chunks = max(stats.peak_chunks, len(fts_batch))
        stats.peak_chunk_text_utf8_bytes = max(
            stats.peak_chunk_text_utf8_bytes, fts_batch_bytes
        )
    commit_batch(fts_batch, fts_batch_bytes)

    actual_count, digest_hex = _persisted_lexical_digest(conn, build_id)
    if actual_count != expected_count or digest_hex != expected_digest:
        raise RuntimeError("FTS source count/digest parity failure")
    with conn:
        conn.execute(
            """UPDATE corpus_publication_builds
               SET status='lexical_ready', lexical_ready=1,
                   lexical_digest=?, lexical_row_count=?, updated_at=?
               WHERE build_id=?""",
            (digest_hex, actual_count, now, build_id),
        )
    return StreamingLexicalResult(
        manifest_id=manifest_id,
        mode_served="fts5",
        row_count=actual_count,
        digest=digest_hex,
    )
