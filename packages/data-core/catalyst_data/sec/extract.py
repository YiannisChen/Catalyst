"""SEC document text extraction and PDF policy outcomes."""

from __future__ import annotations

import re
from dataclasses import dataclass

from catalyst_data.config import RAG_MIN_CHAR_COUNT

PDF_MAGIC = b"%PDF"


@dataclass(frozen=True)
class ExtractOutcome:
    status: str  # success | empty_extract | pdf_skipped | mandatory_failed
    text: str
    requiredness_effect: str | None = None
    error_class: str | None = None


def _looks_like_pdf(raw: bytes, content_type: str | None) -> bool:
    if raw[:4] == PDF_MAGIC:
        return True
    ct = (content_type or "").lower()
    return "pdf" in ct


def _strip_html(text: str) -> str:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", text)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_document_text(
    raw: bytes,
    *,
    content_type: str | None = None,
    is_primary: bool = False,
    requiredness: str = "mandatory",
) -> ExtractOutcome:
    if _looks_like_pdf(raw, content_type):
        if is_primary or requiredness == "mandatory":
            return ExtractOutcome(
                status="mandatory_failed",
                text="",
                requiredness_effect="mandatory_failed",
                error_class="pdf_skipped",
            )
        return ExtractOutcome(
            status="pdf_skipped",
            text="",
            requiredness_effect="optional_degraded",
            error_class="pdf_skipped",
        )
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        text = ""
    if "<" in text and ">" in text:
        text = _strip_html(text)
    else:
        text = text.strip()
    if not text:
        return ExtractOutcome(status="empty_extract", text="", error_class="empty_extract")
    if len(text) < RAG_MIN_CHAR_COUNT:
        return ExtractOutcome(status="empty_extract", text=text, error_class="below_min_chars")
    return ExtractOutcome(status="success", text=text)
