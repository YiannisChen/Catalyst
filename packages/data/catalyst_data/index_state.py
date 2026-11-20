"""Persistence of pending article and filing index state."""

from __future__ import annotations

from catalyst_data.index_text import _split_sentences, compute_content_hash


def persist_index_state(conn, *, min_l2_chars: int = 800) -> dict:
    """Build eligible article + filing records and UPSERT into index_state.

    Article eligibility: is_rag_eligible=1 AND has >=1 canonical article_tickers
    association (per-association canonicality from S1).

    UPSERT semantics keyed by chunk_id:
      - Existing row with same content_hash → skip (idempotent).
      - Existing row with different content_hash → mark status='stale'.
      - New chunk_id → INSERT as status='pending'.
      - After stale marking, INSERT the new row as status='pending'.
      Exactly one pending row per chunk_id after each run.

    Returns dict with counts for observability.
    """
    from catalyst_data.eligibility import eligible_article_ids
    from catalyst_data.storage.sqlite import _migrate_index_state_step4a

    _migrate_index_state_step4a(conn)

    total_new = 0
    article_l1 = 0
    article_l2 = 0

    # --- Article records (eligible only) ---
    eligible_ids = eligible_article_ids(conn)
    if eligible_ids:
        placeholders = ",".join("?" for _ in eligible_ids)
        rows = conn.execute(
            f"""SELECT
                    a.article_id, a.provider, a.source_type, a.ticker,
                    a.reference_date, a.published_utc,
                    a.title, a.description,
                    a.article_url, a.image_url, a.author,
                    a.publisher_name, a.publisher_logo_url,
                    a.source_tier, a.dedup_group_id,
                    GROUP_CONCAT(at.ticker, ',') AS tickers_csv
                FROM articles a
                LEFT JOIN article_tickers at ON a.article_id = at.article_id AND at.is_canonical = 1
                WHERE a.article_id IN ({placeholders})
                GROUP BY a.article_id
                ORDER BY a.article_id""",
            eligible_ids,
        ).fetchall()

        for row in rows:
            (
                article_id,
                provider,
                source_type,
                scalar_ticker,
                reference_date,
                published_utc,
                title,
                description,
                article_url,
                image_url,
                author,
                publisher_name,
                publisher_logo_url,
                source_tier,
                dedup_group_id,
                tickers_csv,
            ) = row

            tickers = (
                sorted(set(t for t in (tickers_csv or "").split(",") if t))
                if tickers_csv
                else [scalar_ticker] if scalar_ticker else []
            )

            body = description or ""
            normalized_body = body.strip()
            content_text = f"{title}\n{normalized_body}" if normalized_body else title
            content_hash = compute_content_hash(title, description)

            # UPSERT L1
            if _upsert_index_row(
                conn,
                f"{article_id}::l1",
                "l1",
                article_id,
                "article",
                content_hash,
                content_text,
                provider,
                source_type,
                source_tier or 4,
                tickers,
                reference_date,
                published_utc,
                publisher_name,
                publisher_logo_url,
                article_url,
                image_url,
                author,
                dedup_group_id,
            ):
                total_new += 1
                article_l1 += 1

            # L2 chunks
            if len(body) >= min_l2_chars:
                sentences = _split_sentences(body)
                for idx, sent in enumerate(sentences):
                    if not sent.strip():
                        continue
                    chunk_id = f"{article_id}::l2s{idx:04d}"
                    sent_hash = compute_content_hash(sent, None)
                    if _upsert_index_row(
                        conn,
                        chunk_id,
                        "l2",
                        article_id,
                        "article",
                        sent_hash,
                        sent,
                        provider,
                        source_type,
                        source_tier or 4,
                        tickers,
                        reference_date,
                        published_utc,
                        publisher_name,
                        publisher_logo_url,
                        article_url,
                        image_url,
                        author,
                        dedup_group_id,
                    ):
                        total_new += 1
                        article_l2 += 1

    conn.commit()

    # --- Filing records (existing behavior: is_rag_eligible=1 only) ---
    filing_l1, filing_l2 = _persist_filing_records(conn, min_l2_chars)
    total_new += filing_l1 + filing_l2

    # Count pending rows from DB (reflects actual state, not just new inserts)
    article_l1_db = conn.execute(
        "SELECT COUNT(*) FROM index_state WHERE source_kind='article' AND chunk_level='l1' AND status='pending'"
    ).fetchone()[0]
    article_l2_db = conn.execute(
        "SELECT COUNT(*) FROM index_state WHERE source_kind='article' AND chunk_level='l2' AND status='pending'"
    ).fetchone()[0]

    return {
        "article_l1_pending": article_l1_db,
        "article_l2_pending": article_l2_db,
        "filing_l1_pending": filing_l1,
        "filing_l2_pending": filing_l2,
        "total_new_rows": total_new,
        "eligible_article_count": len(eligible_ids),
    }


def _upsert_index_row(
    conn,
    chunk_id,
    chunk_level,
    corpus_item_id,
    source_kind,
    content_hash,
    content_text,
    provider,
    source_type,
    source_tier,
    tickers,
    reference_date,
    published_utc,
    publisher_name,
    publisher_logo_url,
    article_url,
    image_url,
    author,
    dedup_group_id,
) -> bool:
    """UPSERT one index_state row by chunk_id. Returns True if a new pending row was inserted."""
    import json

    existing = conn.execute(
        "SELECT content_hash, status FROM index_state WHERE chunk_id = ?", (chunk_id,)
    ).fetchall()

    # If existing row with same hash → skip
    for ex_hash, ex_status in existing:
        if ex_hash == content_hash and ex_status == "pending":
            return False

    # Mark all existing rows for this chunk_id as stale
    conn.execute(
        "UPDATE index_state SET status = 'stale' WHERE chunk_id = ? AND status = 'pending'",
        (chunk_id,),
    )

    # Insert new pending row (REPLACE handles stale row with same chunk_id)
    conn.execute(
        """INSERT INTO index_state
           (chunk_id, chunk_level, corpus_item_id, source_kind,
            content_hash, content_text, status,
            provider, source_type, source_tier,
            tickers_json, reference_date, published_utc,
            publisher_name, publisher_logo_url,
            article_url, image_url, author, dedup_group_id)
           VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            chunk_id,
            chunk_level,
            corpus_item_id,
            source_kind,
            content_hash,
            content_text,
            provider,
            source_type,
            source_tier,
            json.dumps(tickers) if tickers else "[]",
            reference_date,
            published_utc,
            publisher_name,
            publisher_logo_url,
            article_url,
            image_url,
            author,
            dedup_group_id,
        ),
    )
    return True


def _persist_filing_records(conn, min_l2_chars):
    """Build and persist filing index_state records. Returns (l1_count, l2_count)."""

    rows = conn.execute(
        """
        SELECT
            f.filing_id, f.cik, f.ticker, f.form_type, f.filed_at,
            f.accession_number, f.url, f.source_tier,
            fd.text AS doc_text, fd.extraction_status, fd.document_type
        FROM filings f
        LEFT JOIN filing_documents fd
            ON f.filing_id = fd.filing_id
            AND fd.extraction_status = 'success'
        WHERE f.is_rag_eligible = 1
        ORDER BY f.filing_id, fd.document_type ASC
    """
    ).fetchall()

    # Deduplicate: one row per filing_id (exhibit preferred)
    best_doc = {}
    null_rows = {}
    for row in rows:
        fid = row[0]
        if row[9] is not None:
            if fid not in best_doc:
                best_doc[fid] = row
        else:
            if fid not in null_rows:
                null_rows[fid] = row
    for fid, null_row in null_rows.items():
        if fid not in best_doc:
            best_doc[fid] = null_row

    l1_count = 0
    l2_count = 0

    for fid, row in best_doc.items():
        (
            filing_id,
            cik,
            ticker,
            form_type,
            filed_at,
            accession_number,
            url,
            source_tier,
            doc_text,
            extraction_status,
            doc_type,
        ) = row

        title = f"{form_type} filed {filed_at}"
        body = doc_text or ""
        content_hash = compute_content_hash(title, body)
        normalized_body = body.strip()
        content_text = f"{title}\n{normalized_body}" if normalized_body else title

        import json

        tickers_json = json.dumps([ticker]) if ticker else "[]"

        if _upsert_index_row(
            conn,
            f"{filing_id}::l1",
            "l1",
            filing_id,
            "filing",
            content_hash,
            content_text,
            "sec",
            "sec_filing",
            source_tier or 1,
            [ticker] if ticker else [],
            filed_at,
            filed_at,
            None,
            None,
            url,
            None,
            None,
            None,
        ):
            l1_count += 1

        if extraction_status == "success" and len(body) >= min_l2_chars:
            sentences = _split_sentences(body)
            for idx, sent in enumerate(sentences):
                if not sent.strip():
                    continue
                chunk_id = f"{filing_id}::l2s{idx:04d}"
                sent_hash = compute_content_hash(sent, None)
                if _upsert_index_row(
                    conn,
                    chunk_id,
                    "l2",
                    filing_id,
                    "filing",
                    sent_hash,
                    sent,
                    "sec",
                    "sec_filing",
                    source_tier or 1,
                    [ticker] if ticker else [],
                    filed_at,
                    filed_at,
                    None,
                    None,
                    url,
                    None,
                    None,
                    None,
                ):
                    l2_count += 1

    conn.commit()
    return l1_count, l2_count
