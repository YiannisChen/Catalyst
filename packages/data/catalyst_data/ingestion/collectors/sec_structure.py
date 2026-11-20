"""v2.1: an SEC exhibit (EX-99 press release) kept as structure, not only as the flattened text block.

``parse(html)`` returns the release's units in document order: its title (the release headline), bullets and
paragraphs. The blocks are the same innermost p/div/li/td blocks ``sec.exhibit_text`` keeps (so every unit's text
is in the stored body), classified by layout: a list item or a block opening with a bullet glyph is a bullet; the
first short block without a full stop that announces or reports something (sec_structure.is_title) is the title; the rest
are paragraphs. The units are stored per exhibit URL in ``sec_exhibit_units`` in the bodies store (the body text
itself is unchanged), and read back by URL with ``units_for``.

The quote rules use the units for SEC evidence when the v2 rule ``filing`` is on: a headline is quoted whole, and
a body quote must start and end inside one bullet or paragraph (attribution/quotes.py).
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone

from catalyst_data.read import sec_units as _sec_units
from catalyst_data.storage import connect as store_connect  # noqa: F401

filing_units_disabled = _sec_units.filing_units_disabled
title_for = _sec_units.title_for
units_count = _sec_units.units_count
units_for = _sec_units.units_for

BULLET = re.compile(r"^[•◦▪■●○▫◆◇►▶➢✓\-–—*]\s*")
TITLE_WORDS = re.compile(r"\b(reports?|reported|announces?|delivers|posts|declares)\b", re.I)
DANGLING = re.compile(r"(\b(and|or|of|for|the|a|an|to|with|in|on|by)|[,;:–—-])$", re.I)
MIN_UNIT, MIN_TITLE, MAX_TITLE = 40, 20, 240
TABLE = """CREATE TABLE IF NOT EXISTS sec_exhibit_units (
    url TEXT PRIMARY KEY, accession TEXT, units_json TEXT NOT NULL, parsed_at TEXT NOT NULL)"""


def _blocks(html: str) -> list[tuple[str, str, bool]]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    tags = ["p", "div", "li", "td"]
    out = []
    for block in soup.find_all(tags):
        if block.find(tags) is not None:
            continue
        text = " ".join(block.get_text(" ", strip=True).split())
        if len(text) < MIN_TITLE:
            continue
        bold = bool(block.find(["b", "strong"])) and len(" ".join(
            " ".join(b.get_text(" ", strip=True) for b in block.find_all(["b", "strong"])).split())) >= 0.8 * len(text)
        style = (block.get("style") or "").lower().replace(" ", "")
        bold = bold or "font-weight:bold" in style or "font-weight:700" in style
        if not out or out[-1][1] != text:
            out.append((block.name, text, bold))
    return out


def parse(html: str) -> dict:
    """{"title": str | None, "units": [{"kind": "title"|"bullet"|"paragraph", "text": str}]}."""
    from catalyst_data.ingestion.collectors.fetch import strip_zero_width
    units, title = [], None
    for name, text, bold in _blocks(html):
        text = strip_zero_width(text).strip()
        letters = sum(c.isalpha() for c in text)
        if not text or letters / len(text) < 0.5:
            continue
        is_bullet = name == "li" or bool(BULLET.match(text))
        if title is None and not is_bullet and is_title(text) and not units_have_paragraph(units):
            title = text
            units.append({"kind": "title", "text": text})
            continue
        if len(text) < MIN_UNIT:
            continue
        if is_bullet:
            units.append({"kind": "bullet", "text": BULLET.sub("", text)})
        else:
            units.append({"kind": "paragraph", "text": text})
    return {"title": title, "units": units}


def is_title(text: str) -> bool:
    """A release headline: one short line, no full stop, not cut off, and it announces or reports something
    ("Analog Devices Reports Record ... Results"). A deck's cover ("Q2 2026 Earnings Presentation") is not one."""
    return (MIN_TITLE <= len(text) <= MAX_TITLE and not re.search(r"[.!?]$", text) and not DANGLING.search(text)
            and bool(TITLE_WORDS.search(text)))


def reclassify(structure: dict) -> dict:
    """Apply the current title rule to stored units (no refetch): a stored title that fails it becomes a paragraph
    when it is long enough to be one, else it is dropped."""
    units, title = [], None
    for u in structure["units"]:
        if u["kind"] == "title":
            if title is None and is_title(u["text"]):
                title = u["text"]
                units.append(u)
            elif len(u["text"]) >= MIN_UNIT:
                units.append({"kind": "paragraph", "text": u["text"]})
            continue
        units.append(u)
    return {"title": title, "units": units}


def units_have_paragraph(units: list[dict]) -> bool:
    """The title comes before the release's first paragraph (contact lines are short and do not count)."""
    return any(u["kind"] == "paragraph" and len(u["text"]) > 160 for u in units)


def write(conn: sqlite3.Connection, url: str, accession: str | None, structure: dict) -> None:
    conn.execute(TABLE)
    conn.execute("INSERT OR REPLACE INTO sec_exhibit_units VALUES (?,?,?,?)",
                 (url, accession, json.dumps(structure, ensure_ascii=False),
                  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
