"""Alpaca Benzinga source adaptation for corpus publication."""

from __future__ import annotations

import html
import json
import re
import sqlite3
from collections.abc import Iterator
from html.parser import HTMLParser

from catalyst_data.corpus.source_document import _SourceDocument
from catalyst_data.ingestion.time import timefields


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _article_text(title: object, description: object) -> str:
    title_text = str(title or "")
    description_text = str(description or "")
    return f"{title_text}\n{description_text}" if description_text else title_text


def _iter_alpaca_news_documents(
    conn: sqlite3.Connection, *, page_size: int
) -> Iterator[_SourceDocument]:
    """Adapt current Alpaca Benzinga rows into the ordinary news chunk profile.

    Other publishers in Alpaca's mixed news archive are excluded. The archive
    is caller-owned and separate from the primary snapshot, so its snapshot
    identity is required by the corpus builder.
    """
    if not _table_exists(conn, "news"):
        raise ValueError("Alpaca news connection has no news table")
    columns = _table_columns(conn, "news")
    required = {
        "id", "created_at", "updated_at", "headline", "content", "source", "symbols_json",
    }
    if not required <= columns:
        raise ValueError("Alpaca news table is missing required columns")

    last_id = -1
    while True:
        cursor = conn.execute(
            """SELECT id, created_at, updated_at, headline, content, symbols_json
               FROM news WHERE id > ? AND LOWER(TRIM(COALESCE(source, ''))) = 'benzinga'
               ORDER BY id LIMIT ?""",
            (last_id, page_size),
        )
        rows = cursor.fetchmany(page_size)
        if not rows:
            break
        for row in rows:
            raw_id, created_at, updated_at, headline, content, symbols_json = row
            if isinstance(raw_id, bool) or not isinstance(raw_id, int):
                raise ValueError("Alpaca news id must be an integer")
            last_id = raw_id
            document_id = f"alpaca_news:{raw_id}"
            available_at = timefields.external_stamp(
                created_at, source="alpaca", field="created_at"
            )
            if available_at is None:
                raise ValueError(f"Alpaca article {raw_id} has no valid created_at")
            provider_updated_at = (
                timefields.external_stamp(
                    updated_at, source="alpaca", field="updated_at"
                )
                if updated_at not in (None, "") else None
            )
            try:
                symbols = json.loads(symbols_json or "[]")
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError(f"Alpaca article {raw_id} has invalid symbols_json") from exc
            if not isinstance(symbols, list) or any(not isinstance(item, str) for item in symbols):
                raise ValueError(f"Alpaca article {raw_id} has invalid symbols_json")
            tickers = sorted({item.strip().upper() for item in symbols if item.strip()})
            title = _alpaca_news_html_to_text(str(headline or ""))
            body = _alpaca_news_html_to_text(str(content or ""))
            eligibility = "eligible" if body.strip() else "ineligible"
            document = {
                "document_id": document_id,
                "title": title,
                "description": body,
                "available_at": available_at,
                "provider_updated_at": provider_updated_at,
                "ticker_associations": json.dumps(tickers, separators=(",", ":")),
                "source_class": "reported_news",
                "dedup_cluster_id": None,
                "cluster_first_available_at": available_at,
                "representative_document_id": document_id,
                "eligibility": eligibility,
            }
            yield _SourceDocument(
                source_kind="article",
                source_key=document_id,
                document_id=document_id,
                source_utf8_bytes=len(_article_text(title, body).encode("utf-8"))
                if eligibility == "eligible" else 0,
                document=document,
                provider="alpaca",
                source_type="benzinga_news",
                eligibility=eligibility,
            )


class _AlpacaNewsHTMLTextParser(HTMLParser):
    _BLOCK_TAGS = {
        "address", "article", "aside", "blockquote", "br", "dd", "div", "dl",
        "dt", "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5",
        "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section",
        "table", "td", "th", "tr", "ul",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []
        self._skipped_content_tag: str | None = None
        self._skipped_content_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self._skipped_content_tag is not None:
            return
        if tag in {"script", "style"}:
            self._skipped_content_tag = tag
            self._skipped_content_parts.clear()
        elif tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._skipped_content_tag is not None:
            if tag == self._skipped_content_tag:
                self._skipped_content_tag = None
                self._skipped_content_parts.clear()
            return
        if tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skipped_content_tag is None:
            self.parts.append(data)
        else:
            self._skipped_content_parts.append(data)

    def handle_entityref(self, name: str) -> None:
        if self._skipped_content_tag is None:
            self.parts.append(f"&{name};")
        else:
            self._skipped_content_parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self._skipped_content_tag is None:
            self.parts.append(f"&#{name};")
        else:
            self._skipped_content_parts.append(f"&#{name};")

    def close(self) -> None:
        super().close()
        if self._skipped_content_tag is None:
            return
        # HTMLParser treats script/style as raw text even when their closing tag
        # is missing. Reparse that buffered tail as ordinary markup so malformed
        # upstream tags cannot hide the following article body from FTS.
        tail = "".join(self._skipped_content_parts) + self.rawdata
        tail = re.sub(r"</?(?:script|style)\b[^>]*>", "", tail, flags=re.IGNORECASE)
        fallback = _AlpacaNewsHTMLTextParser()
        fallback.feed(tail)
        fallback.close()
        self.parts.extend(fallback.parts)
        self._skipped_content_tag = None
        self._skipped_content_parts.clear()


def _alpaca_news_html_to_text(value: str) -> str:
    """Convert Alpaca-delivered Benzinga HTML into searchable plain text."""
    parser = _AlpacaNewsHTMLTextParser()
    parser.feed(value)
    parser.close()
    return html.unescape("".join(parser.parts))


def _validate_alpaca_snapshot_identity(
    *,
    alpaca_news_conn: sqlite3.Connection | None,
    alpaca_source_snapshot_identity: str | None,
) -> str | None:
    if alpaca_news_conn is None:
        if alpaca_source_snapshot_identity is not None:
            raise ValueError("Alpaca snapshot identity requires an Alpaca news connection")
        return None
    if not isinstance(alpaca_source_snapshot_identity, str) or not alpaca_source_snapshot_identity.strip():
        raise ValueError("Alpaca news input requires an explicit snapshot identity")
    return alpaca_source_snapshot_identity.strip()
