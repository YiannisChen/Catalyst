"""Parse EDGAR filing index / index-headers HTML into document descriptors."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin


@dataclass(frozen=True)
class IndexParseResult:
    status: str  # success | failed
    documents: tuple[dict[str, Any], ...]
    error: str | None = None


_ROW_RE = re.compile(
    r"<tr[^>]*>\s*"
    r"(?:<td[^>]*>\s*(?P<seq>[^<]*)</td>\s*)?"
    r"<td[^>]*>\s*(?P<desc>[^<]*)</td>\s*"
    r"<td[^>]*>\s*(?:<a[^>]+href=\"(?P<href>[^\"]+)\"[^>]*>)?(?P<name>[^<]+)",
    re.I | re.S,
)
_TYPE_RE = re.compile(r"<td[^>]*>\s*(?P<dtype>EX-99[^<\s]*|EX-\d+[^<\s]*|8-K|10-K|10-Q|20-F|6-K)[^<]*</td>", re.I)


def _ext(filename: str) -> str:
    if "." not in filename:
        return ""
    return filename.rsplit(".", 1)[-1].lower()


def parse_filing_index_html(
    html: str,
    *,
    base_url: str,
    primary_document: str | None = None,
) -> IndexParseResult:
    if not html or not html.strip():
        return IndexParseResult(status="failed", documents=(), error="empty_html")

    docs: list[dict[str, Any]] = []
    # Prefer structured table rows with Sequence / Description / Document / Type
    # Fall back to anchor scraping for index-headers style pages.
    for m in re.finditer(
        r"<a[^>]+href=\"([^\"]+)\"[^>]*>([^<]+)</a>", html, flags=re.I
    ):
        href, name = m.group(1).strip(), m.group(2).strip()
        if not name or name.lower().endswith(".xml") and "ex" not in name.lower():
            # keep exhibits and primary-like docs; skip pure xbrl if clearly not primary
            pass
        lower = name.lower()
        if lower.endswith((".jpg", ".png", ".gif", ".css", ".js")):
            continue
        if "index" in lower and lower.endswith((".htm", ".html")):
            continue
        filename = name.split("/")[-1]
        url = urljoin(base_url if base_url.endswith("/") else base_url + "/", href)
        # Infer type from nearby context
        start = max(0, m.start() - 400)
        ctx = html[start : m.end() + 100]
        dtype_m = re.search(
            r"(EX-99[^\s<]*|EX-\d+[^\s<]*|\b8-K\b|\b10-K\b|\b10-Q\b|\b20-F\b|\b6-K\b)",
            ctx,
            re.I,
        )
        document_type = dtype_m.group(1).upper() if dtype_m else "unknown"
        seq_m = re.search(r">\s*(\d{1,4})\s*<", ctx)
        sequence = int(seq_m.group(1)) if seq_m else None
        is_primary = False
        if primary_document and filename.lower() == primary_document.lower():
            is_primary = True
        elif primary_document is None and not docs:
            is_primary = True
        docs.append(
            {
                "filename": filename,
                "document_file": filename,
                "document_url": url,
                "document_type": document_type,
                "description": filename,
                "sequence": sequence,
                "content_extension": _ext(filename),
                "is_primary": is_primary,
            }
        )

    if not docs:
        return IndexParseResult(status="failed", documents=(), error="no_documents")

    # Ensure exactly one primary if primary_document provided
    if primary_document:
        matched = False
        for d in docs:
            if d["filename"].lower() == primary_document.lower():
                d["is_primary"] = True
                matched = True
            else:
                d["is_primary"] = False
        if not matched:
            return IndexParseResult(
                status="failed", documents=(), error="primary_not_found"
            )
    primaries = [d for d in docs if d.get("is_primary")]
    if len(primaries) != 1:
        # Prefer first HTML as primary if none
        if not primaries:
            for d in docs:
                if d["content_extension"] in {"htm", "html", "txt"}:
                    d["is_primary"] = True
                    break
            primaries = [d for d in docs if d.get("is_primary")]
        if len(primaries) != 1:
            return IndexParseResult(
                status="failed", documents=tuple(docs), error="no_primary"
            )

    return IndexParseResult(status="success", documents=tuple(docs), error=None)


INDEX_URL_CANDIDATES = (
    "{base}{accession}-index.html",
    "{base}{accession}-index-headers.html",
    "{base}{accession}-index-headers.htm",
)


def filing_index_urls(*, cik_int: str, accession: str) -> list[str]:
    nodash = accession.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik_int)}/{nodash}/"
    return [
        f"{base}{accession}-index.html",
        f"{base}{accession}-index-headers.html",
        f"{base}{accession}-index-headers.htm",
    ]
