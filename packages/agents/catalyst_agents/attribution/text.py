"""Text normalization and matching primitives used by attribution rules."""
from __future__ import annotations

import hashlib
import html
import re

_ABBREVIATIONS = (
    "Inc.", "Corp.", "Co.", "Ltd.", "U.S.", "No.", "vs.", "approx.", "e.g.",
    "i.e.", "Mr.", "Ms.", "Dr.", "Jan.", "Feb.", "Mar.", "Apr.", "Aug.",
    "Sept.", "Oct.", "Nov.", "Dec.", "U.K.", "St.",
)
# "(i)", "(a)", "(2)" or ";" open an enumerated clause of a legal sentence.
_LIST_MARKER = re.compile(r"(?:\((?:[ivx]+|[a-z]|\d+)\)|;)$")
_QUOTE_MARKS = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"})
_WORD = re.compile(r"[A-Za-z]{2,}")
_NUMERIC = re.compile(r"^[\$\(\)\-–—%.,0-9]+$")
# Raw markup only: a tag name right after "<", so "a < b" or "<NYSE: ABT>" is not a tag,
# and entity-escaped markup ("&lt;b&gt;") is literal text, never stripped.
_HTML_TAG = re.compile(r"<!--.*?-->|</?([A-Za-z][A-Za-z0-9-]*)(?:\s[^<>]*)?/?>", re.DOTALL)
# Block tags separate text; inline tags (b, span, a) can sit inside a word.
_HTML_BLOCK_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt", "figcaption",
    "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "ol", "p",
    "pre", "section", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
})


def normalize_text(text: str) -> str:
    """Decode HTML entities and collapse whitespace (the display text)."""
    return " ".join(html.unescape(text).replace(" ", " ").split())


def _provenance_fingerprint(excerpt: str) -> tuple[str, ...]:
    """Hash normalized excerpt sentences for copy detection, retaining at most 60."""
    body = normalize_text(excerpt)
    for abbreviation in _ABBREVIATIONS:
        body = body.replace(abbreviation, abbreviation.replace(".", ""))
    body = re.sub(r"(?<=\d)\.(?=\d)", "", body)
    sentences = re.split(r"(?<=[.!?])(?:[\"'”’)]*)\s+", body)
    fingerprints = []
    for sentence in sentences:
        normalized = normalize_text(sentence).casefold()
        if normalized:
            fingerprints.append(hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:10])
        if len(fingerprints) >= 60:
            break
    return tuple(fingerprints)


def _match_key(text: str) -> str:
    """Comparison form: HTML tags dropped, quote marks and punctuation spacing
    unified, so markup, "13 %" vs "13%" or curly vs straight quotes never decide a match."""
    if _HTML_TAG.search(text):
        text = _HTML_TAG.sub(
            lambda tag: " " if (tag.group(1) or "").lower() in _HTML_BLOCK_TAGS else "", text
        )
    value = normalize_text(text).translate(_QUOTE_MARKS)
    value = re.sub(r"\s+([,.;:%)\]])", r"\1", value)
    return re.sub(r"([(\[$])\s+", r"\1", value)


def _count_sentences(quote: str) -> int:
    body = quote
    for abbreviation in _ABBREVIATIONS:
        body = body.replace(abbreviation, abbreviation.replace(".", ""))
    body = re.sub(r"(?<=\d)\.(?=\d)", "", body)
    return max(1, len(re.findall(r"[.!?][\"'”’)\]]*(?:\s|$)", body)))


def _is_prose(quote: str) -> bool:
    tokens = quote.split()
    if len(_WORD.findall(quote)) < 5:
        return False
    numeric = sum(1 for token in tokens if _NUMERIC.match(token) or "$" in token)
    return numeric <= 0.4 * len(tokens)


def _starts_at_sentence(excerpt: str, start: int) -> bool:
    if start == 0:
        return True
    before = excerpt[:start].rstrip()
    if not before:
        return True
    if before.endswith(("•", "·", ":", ";")) or _LIST_MARKER.search(before):
        return True
    # A table cell (number, percent, closing parenthesis) right before an
    # uppercase start is a flattened-table boundary, not a mid-sentence cut.
    if re.search(r"[0-9%)]$", before) and excerpt[start : start + 1].isupper():
        return True
    return bool(re.search(r"[.!?][\"'”’)\]]*$", before)) and not any(
        before.endswith(abbreviation) for abbreviation in _ABBREVIATIONS
    )
