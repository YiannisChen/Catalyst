"""SEC EDGAR connector for submissions metadata and document fetching.

Endpoints:
- sec_submissions: /submissions/CIK{}.json (all filings for a CIK)
- fetch_document(url): any SEC document URL

No API key required. Descriptive User-Agent required per SEC fair-access policy.
Rate limit: <= 5 req/s (configurable via limiter).
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Callable, Awaitable

import httpx

from catalyst_data.connectors.base import FetchResult
from catalyst_data.cik_map import ticker_to_cik
from catalyst_data.retry import with_retry

SEC_BASE_URL = "https://data.sec.gov/submissions"


def _build_submissions_url(ticker: str, _date: str) -> tuple[str, dict[str, str]]:
    cik = ticker_to_cik(ticker)
    url = f"{SEC_BASE_URL}/CIK{cik}.json"
    return url, {}


_ENDPOINT_BUILDERS = {
    "sec_submissions": _build_submissions_url,
}


def create_sec_fetcher(
    user_agent: str,
    limiter=None,
    client: httpx.AsyncClient | None = None,
):
    """Return a namespace with fetch() and fetch_document() for SEC EDGAR.

    fetch(ticker, endpoint, date) -> FetchResult
    fetch_document(url) -> FetchResult

    Both share the same user_agent, limiter, and client.
    """
    async def fetch(ticker: str, endpoint: str, date: str) -> FetchResult:
        builder = _ENDPOINT_BUILDERS.get(endpoint)
        if builder is None:
            return FetchResult(
                status=0,
                error=f"Unknown SEC endpoint: {endpoint}",
                source_label=f"sec:{endpoint}",
            )

        url, params = builder(ticker, date)
        start = time.monotonic()
        own_client = client is None
        c = client if not own_client else httpx.AsyncClient(timeout=30.0)
        try:
            headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
            if limiter is None:
                resp = await c.get(url, params=params, headers=headers)
            else:
                async with limiter.acquire():
                    resp = await c.get(url, params=params, headers=headers)
            latency = (time.monotonic() - start) * 1000

            if resp.status_code == 200:
                return FetchResult(
                    status=200,
                    data=resp.json(),
                    latency_ms=latency,
                    source_label=f"sec:{endpoint}",
                )

            retry_after = None
            if resp.status_code == 429:
                ra = resp.headers.get("retry-after")
                if ra:
                    try:
                        retry_after = float(ra)
                    except (ValueError, TypeError):
                        pass

            return FetchResult(
                status=resp.status_code,
                error=f"SEC {resp.status_code}: {resp.text[:200]}",
                latency_ms=latency,
                source_label=f"sec:{endpoint}",
                retry_after_seconds=retry_after,
            )
        except (httpx.TimeoutException, httpx.ReadTimeout) as exc:
            latency = (time.monotonic() - start) * 1000
            return FetchResult(
                status=0,
                error=f"Timeout: {exc}",
                latency_ms=latency,
                source_label=f"sec:{endpoint}",
            )
        except httpx.HTTPError as exc:
            latency = (time.monotonic() - start) * 1000
            return FetchResult(
                status=0,
                error=str(exc),
                latency_ms=latency,
                source_label=f"sec:{endpoint}",
            )
        finally:
            if own_client:
                await c.aclose()

    async def fetch_document(url: str) -> FetchResult:
        start = time.monotonic()
        own_client = client is None
        c = client if not own_client else httpx.AsyncClient(timeout=30.0)
        try:
            headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
            if limiter is None:
                resp = await c.get(url, headers=headers)
            else:
                async with limiter.acquire():
                    resp = await c.get(url, headers=headers)
            latency = (time.monotonic() - start) * 1000

            content_type = resp.headers.get("content-type", "")
            raw_bytes = resp.content
            byte_size = len(raw_bytes)

            if resp.status_code == 200:
                text = None
                extraction_status = "success"
                if "text/html" in content_type:
                    try:
                        text = _extract_text_from_html(raw_bytes.decode("latin-1", errors="replace"))
                        if not text or len(text.strip()) < 50:
                            extraction_status = "empty"
                    except Exception:
                        extraction_status = "fetch_failed"
                        text = None
                elif "application/pdf" in content_type:
                    extraction_status = "pdf_skipped"
                else:
                    extraction_status = "success" if text else "empty"

                return FetchResult(
                    status=200,
                    data={
                        "url": url,
                        "text": text,
                        "content_type": content_type,
                        "byte_size": byte_size,
                        "extraction_status": extraction_status,
                    },
                    latency_ms=latency,
                    source_label="sec:primary_doc",
                )

            return FetchResult(
                status=resp.status_code,
                error=f"SEC doc {resp.status_code}",
                latency_ms=latency,
                source_label="sec:primary_doc",
            )
        except (httpx.TimeoutException, httpx.ReadTimeout) as exc:
            return FetchResult(status=0, error=f"Timeout: {exc}", latency_ms=(time.monotonic()-start)*1000, source_label="sec:primary_doc")
        except httpx.HTTPError as exc:
            return FetchResult(status=0, error=str(exc), latency_ms=(time.monotonic()-start)*1000, source_label="sec:primary_doc")
        finally:
            if own_client:
                await c.aclose()

    fetch_wrapped = with_retry(fetch, provider="sec")
    fetch_doc_wrapped = with_retry(fetch_document, provider="sec")

    return SimpleNamespace(fetch=fetch_wrapped, fetch_document=fetch_doc_wrapped)


# ---------------------------------------------------------------------------
# HTML text extraction (stdlib only, no deps)
# ---------------------------------------------------------------------------

from html.parser import HTMLParser
from html import unescape
import re as _re


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._text: list[str] = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip = True

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self._skip = False
        if tag in ("p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"):
            self._text.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self._text.append(data)

    def get_text(self) -> str:
        raw = "".join(self._text)
        raw = _re.sub(r"\n{3,}", "\n\n", raw)
        raw = _re.sub(r"[ \t]+", " ", raw)
        text = unescape(raw).strip()
        # Strip iXBRL metadata lines (generated by XBRL processors)
        lines = [
            l for l in text.split("\n")
            if not _re.match(r'^[\w-]+(false|true)?\s*\d{7,}', l.strip())
            and not l.strip().startswith(("us-gaap:", "dei:", "xbrli:", "country:"))
        ]
        return "\n".join(lines)


def _extract_text_from_html(html: str) -> str:
    extractor = _TextExtractor()
    extractor.feed(html)
    return extractor.get_text()
