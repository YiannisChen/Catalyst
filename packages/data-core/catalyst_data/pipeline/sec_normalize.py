"""SEC normalization: submissions → filings rows, document resolution, text extraction.

RELEVANT_8K_ITEMS: attribution-relevant 8-K item set (constant, expandable).
normalize_submissions: filter submissions to forms 8-K/10-Q/10-K in date range.
resolve_filing_documents: fetch documents for attribution-relevant 8-Ks.
extract_text_from_html: stdlib HTMLParser, no deps.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from html import unescape
from typing import Any

logger = logging.getLogger(__name__)

# 8-K items relevant for stock-move attribution
RELEVANT_8K_ITEMS = frozenset({
    "1.01",  # Material Definitive Agreement → customer_partner_contracts
    "1.03",  # Bankruptcy or Receivership → fundamentals_financials
    "2.02",  # Results of Operations → earnings_results
    "2.05",  # Exit/Disposal Costs → fundamentals_financials
    "2.06",  # Material Impairments → fundamentals_financials
    "5.02",  # Management Changes → management_governance
    "7.01",  # Regulation FD → guidance_outlook
    "8.01",  # Other Events → corporate_actions, regulatory_legal
    "9.01",  # Financial Statements/Exhibits → earnings_results
})

ALLOWED_FORMS = frozenset({"8-K", "10-Q", "10-K"})


def _is_relevant_8k(items_str: str | None) -> bool:
    if not items_str:
        return False
    items = [x.strip() for x in items_str.split(",") if x.strip()]
    return any(i in RELEVANT_8K_ITEMS for i in items)


def normalize_submissions(
    raw_data: dict,
    ticker: str,
    cik: str,
    from_date: str,
    to_date: str,
) -> list[dict]:
    """Filter submissions to 8-K/10-Q/10-K in [from_date, to_date].

    Returns list of filing dicts ready for upsert_filing().
    """
    recent = raw_data.get("filings", {}).get("recent", {})
    if not recent:
        return []

    forms = recent.get("form", [])
    filing_dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    report_dates = recent.get("reportDate", [])
    primary_docs = recent.get("primaryDocument", [])
    items_list = recent.get("items", [""] * len(forms))

    filings = []
    for i in range(len(forms)):
        form = forms[i]
        fd = filing_dates[i] if i < len(filing_dates) else ""
        if form not in ALLOWED_FORMS:
            continue
        if fd < from_date or fd > to_date:
            continue

        acc = accessions[i] if i < len(accessions) else ""
        period = report_dates[i] if i < len(report_dates) else ""
        prim = primary_docs[i] if i < len(primary_docs) else ""
        items_str = items_list[i] if i < len(items_list) else ""

        cik_int = cik.lstrip("0") or "0"
        acc_no_dash = acc.replace("-", "")
        url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_no_dash}/{acc}-index.htm"

        filing_id = f"sec:{cik}:{acc}"
        dedup = hashlib.sha256(f"sec:{acc}".encode()).hexdigest()[:16]

        is_relevant = _is_relevant_8k(items_str) if form == "8-K" else True
        items_json_val = json.dumps([x.strip() for x in items_str.split(",") if x.strip()]) if items_str else "[]"

        filings.append({
            "filing_id": filing_id,
            "cik": cik,
            "ticker": ticker,
            "form_type": form,
            "filed_at": fd,
            "period": period or None,
            "accession_number": acc,
            "primary_document": prim or None,
            "url": url,
            "items_json": items_json_val,
            "source_tier": 1,
            "dedup_group_id": dedup,
            "is_rag_eligible": 1 if is_relevant else 0,
        })

    return filings


async def resolve_filing_documents(
    fetcher,
    filing_dict: dict[str, Any],
) -> list[dict]:
    """Fetch all prose documents for a filing.

    For rag-eligible 8-Ks: fetch primaryDocument AND enumerate index-headers
    for EX-99.* exhibits.  Return both; the record builder picks the best one.
    10-Q/10-K and non-relevant 8-Ks: return empty list.
    """
    form_type = filing_dict["form_type"]
    is_8k = form_type == "8-K"
    is_relevant = filing_dict.get("is_rag_eligible", 0) == 1

    if not is_8k or not is_relevant:
        return []

    cik = filing_dict["cik"]
    acc = filing_dict["accession_number"]
    prim_doc = filing_dict.get("primary_document", "")
    cik_int = cik.lstrip("0") or "0"
    acc_no_dash = acc.replace("-", "")
    filing_id_val = filing_dict["filing_id"]

    docs = []

    # 1. Fetch primary document
    if prim_doc:
        prim_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_no_dash}/{prim_doc}"
        result = await fetcher.fetch_document(prim_url)
        if result.status == 200 and result.data:
            d = result.data
            text = d.get("text", "")
            docs.append({
                "filing_id": filing_id_val,
                "document_url": prim_url,
                "document_type": "primary_doc",
                "text": text,
                "char_len": len(text) if text else 0,
                "content_type": d.get("content_type", ""),
                "byte_size": d.get("byte_size", 0),
                "extraction_status": d.get("extraction_status", "fetch_failed"),
                "raw_bytes": d.get("raw_bytes", b""),
            })

    # 2. For ALL rag-eligible 8-Ks, enumerate index-headers for EX-99.* exhibits
    idx_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_no_dash}/{acc}-index-headers.htm"
    idx_result = await fetcher.fetch_document(idx_url)
    if idx_result.status == 200 and idx_result.data:
        idx_text = idx_result.data.get("text", "") or ""
        ex99_files = _parse_ex99_from_index(idx_text)
        for ex_fn in ex99_files:
            if ex_fn == prim_doc:
                continue  # skip if same as primary
            ex_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_no_dash}/{ex_fn}"
            ex_result = await fetcher.fetch_document(ex_url)
            if ex_result.status == 200 and ex_result.data:
                d = ex_result.data
                text = d.get("text", "")
                docs.append({
                    "filing_id": filing_id_val,
                    "document_url": ex_url,
                    "document_type": "exhibit_99_1",
                    "text": text,
                    "char_len": len(text) if text else 0,
                    "content_type": d.get("content_type", ""),
                    "byte_size": d.get("byte_size", 0),
                    "extraction_status": d.get("extraction_status", "fetch_failed"),
                    "raw_bytes": d.get("raw_bytes", b""),
                })

    return docs


def _parse_ex99_from_index(html_or_text: str) -> list[str]:
    """Extract EX-99 filenames from an index-headers page."""
    filenames = []
    # Try escaped SGML: split on &lt;TYPE&gt; and parse each block
    for block in html_or_text.split("&lt;TYPE&gt;")[1:]:
        dtype = block.split("&lt;")[0].strip()
        if not dtype.startswith("EX-99"):
            continue
        fn_match = re.search(r'&lt;FILENAME&gt;(\S+?)\s*&lt;', block)
        if fn_match:
            fn = fn_match.group(1)
            if fn.endswith((".htm", ".html")):
                filenames.append(fn)
    # Also try raw XML format
    if not filenames:
        for block in html_or_text.split("<TYPE>")[1:]:
            dtype = block.split("<")[0].strip()
            if not dtype.startswith("EX-99"):
                continue
            fn_match = re.search(r'<FILENAME>(\S+?)\s*<', block)
            if fn_match:
                fn = fn_match.group(1)
                if fn.endswith((".htm", ".html")):
                    filenames.append(fn)
    return filenames


# ---------------------------------------------------------------------------
# HTML text extraction (stdlib only)
# ---------------------------------------------------------------------------

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
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        raw = re.sub(r"[ \t]+", " ", raw)
        text = unescape(raw).strip()
        # Strip iXBRL metadata lines
        lines = [
            l for l in text.split("\n")
            if not re.match(r'^[\w-]+(false|true)?\s*\d{7,}', l.strip())
            and not l.strip().startswith(("us-gaap:", "dei:", "xbrli:", "country:"))
        ]
        return "\n".join(lines)


def extract_text_from_html(html: str) -> str:
    extractor = _TextExtractor()
    extractor.feed(html)
    return extractor.get_text()
