"""Pre-B6 chunk overflow regression — body-byte-equivalence, gap-proof, determinism.

RED history: the superseded ``news_v2._chunk_text`` implementation silently
truncated ``content_text`` after window formation using character-ratio (0.95)
and fixed-char fallback (1000/500) without updating ``body_token_end`` or
``next_start``, causing silent token loss.  The current working tree passes all
contracts below; the RED failure was reproduced against the pre-fix code and is
now GREEN.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from catalyst_data.corpus.news_v2 import (
    MAX_TOKENS, MAX_PREFIX_TOKENS, MAX_OVERLAP,
    _chunk_text, _prefix, _encoding, _normalize_text, count_tokens,
)


# ── helpers ──────────────────────────────────────────────────────────────────

def _wrap_doc(text: str, title: str = "Test Title", doc_id: str | None = None, **kwargs) -> dict:
    d = {
        "document_id": doc_id or hashlib.sha256(text.encode()).hexdigest(),
        "document_url": "test",
        "title": title,
        "ticker_associations": '["TEST"]',
        "available_at": "2026-07-23T00:00:00Z",
        **kwargs,
    }
    return d

def _chunk(title: str, body: str, section_key: str = "test", profile: str = "filing_v3"):
    return _chunk_text(
        document=_wrap_doc(body, title=title),
        text=body,
        prefix_title=title,
        profile_version=profile,
        section_key=section_key,
        short_document_has_no_prefix=False,
        section_parse_degraded=False,
    )

def _source_body_slice(chunk, source_text: str) -> str:
    """Exact byte/character slice from source_text given body_token_start..end."""
    normalized = _normalize_text(source_text)
    _, offsets = _encoding(normalized)
    return normalized[offsets[chunk.body_token_start][0]:offsets[chunk.body_token_end - 1][1]]

def _strip_prefix(content_text: str, title: str) -> str:
    """Remove the exact prefix from content_text to get pure body."""
    if content_text:
        prefix, _, _ = _prefix(title)
        if prefix and content_text.startswith(prefix):
            return content_text[len(prefix):]
    return content_text


class TestBodyByteEquivalence:
    """Each chunk body after stripping prefix MUST be byte-identical to the
    exact source slice represented by body_token_start..body_token_end."""

    @pytest.mark.parametrize("body,title", [
        ("The quick brown fox. " * 50, "Short Report"),
        ("Financial results for Q2 2026. " * 30, "Quarterly Report Q2 2026"),
        ("データ開示に関する報告書。\n\n第１四半期の業績は次のとおりです。\n\n" * 10, "有価証券報告書"),
    ])
    def test_body_equals_source_slice(self, body, title):
        chunks = _chunk(title, body)
        assert len(chunks) >= 1
        for c in chunks:
            expected = _source_body_slice(c, body)
            actual = _strip_prefix(c.content_text, title)
            assert actual == expected, (
                f"Chunk {c.ordinal}: body mismatch "
                f"(expected len={len(expected)}, got len={len(actual)})"
            )

    def test_body_identity_across_patterns(self):
        cases = [
            ("Title", "Single paragraph text. " * 60),
            ("Long Title With Many Words", ("Multi-paragraph.\n\nSecond.\n\nThird. " * 15)),
        ]
        for title, body in cases:
            chunks = _chunk(title, body)
            for c in chunks:
                expected = _source_body_slice(c, body)
                actual = _strip_prefix(c.content_text, title)
                assert actual == expected, \
                    f"Title={title!r} chunk {c.ordinal}: body mismatch"


class TestNoGaps:
    """Every body token [0, total_tokens) must appear in at least one chunk.
    Consecutive chunks must not have gaps between body ranges."""

    def test_token_coverage_is_complete(self):
        body = ("Section 1. Overview.\n\n" + "Operations. " * 40 + "\n\n" +
                "Section 2. Risk.\n\n" + "Market conditions analysis report. " * 40 + "\n\n" +
                "Section 3. Data.\n\n" + "Revenue from continuing operations. " * 60)
        chunks = _chunk("Annual Report", body)
        assert len(chunks) > 1, (
            f"Need multiple chunks to test gaps, got {len(chunks)}. "
            f"Body token count: {count_tokens(_normalize_text(body))}"
        )

        normalized = _normalize_text(body)
        _, offsets = _encoding(normalized)
        total_tokens = len(offsets)

        covered = set()
        for c in chunks:
            for t in range(c.body_token_start, c.body_token_end):
                covered.add(t)

        missing = sorted(set(range(total_tokens)) - covered)[:20]
        assert not missing, f"Missing body tokens: {missing}..."

    def test_consecutive_chunks_no_gap(self):
        body = "AAA " * 300
        chunks = _chunk("Test", body)
        sorted_chunks = sorted(chunks, key=lambda c: c.body_token_start)
        for i in range(1, len(sorted_chunks)):
            prev = sorted_chunks[i - 1]
            curr = sorted_chunks[i]
            assert curr.body_token_start <= prev.body_token_end, (
                f"Gap: chunk {prev.ordinal} ends {prev.body_token_end}, "
                f"chunk {curr.ordinal} starts {curr.body_token_start}"
            )

    def test_last_chunk_covers_final_token(self):
        body = "Last token. " * 80
        chunks = _chunk("Report", body)
        normalized = _normalize_text(body)
        _, offsets = _encoding(normalized)
        total = len(offsets)
        last = max(chunks, key=lambda c: int(c.ordinal))
        assert last.body_token_end == total, \
            f"Last body_token_end={last.body_token_end}, expected {total}"


class TestOverlapCorrectness:
    """body_overlap_tokens must equal max(0, prev_end - curr_start)."""

    def test_overlap_equals_intersection(self):
        body = "ABCD EFGH IJKL MNOP QRST UVWX YZ01 2345 6789 " * 40
        chunks = _chunk("Test Title With Enough Tokens", body)
        sorted_chunks = sorted(chunks, key=lambda c: c.body_token_start)
        for i in range(1, len(sorted_chunks)):
            prev = sorted_chunks[i - 1]
            curr = sorted_chunks[i]
            expected = max(0, prev.body_token_end - curr.body_token_start)
            assert curr.body_overlap_tokens == expected, (
                f"Chunk {curr.ordinal}: overlap={curr.body_overlap_tokens}, "
                f"expected={expected}"
            )

    def test_first_chunk_zero_overlap(self):
        chunks = _chunk("Title", "Text. " * 60)
        sorted_chunks = sorted(chunks, key=lambda c: int(c.ordinal))
        assert sorted_chunks[0].body_overlap_tokens == 0

    def test_overlap_never_exceeds_max(self):
        chunks = _chunk("Report", "Paragraph text. " * 150)
        for c in chunks:
            assert c.body_overlap_tokens <= MAX_OVERLAP, \
                f"Chunk {c.ordinal}: overlap {c.body_overlap_tokens} > {MAX_OVERLAP}"


class TestDeterminism:
    """Same input → identical chunk_ids, hashes, ranges every call."""

    def test_chunk_ids_and_hashes_deterministic(self):
        body = "Financial results for the quarter ended September 30, 2026. " * 40
        chunks1 = _chunk("10-Q Report", body)
        chunks2 = _chunk("10-Q Report", body)
        assert len(chunks1) == len(chunks2)
        for c1, c2 in zip(chunks1, chunks2):
            assert c1.chunk_id == c2.chunk_id
            assert c1.content_hash == c2.content_hash
            assert c1.metadata_hash == c2.metadata_hash
            assert c1.body_token_start == c2.body_token_start
            assert c1.body_token_end == c2.body_token_end
            assert c1.body_overlap_tokens == c2.body_overlap_tokens

    def test_unicode_deterministic(self):
        body = "データ開示に関する報告書。\n\n第１四半期の業績は次のとおりです。\n\n" * 12
        chunks1 = _chunk("有価証券報告書", body)
        chunks2 = _chunk("有価証券報告書", body)
        assert len(chunks1) == len(chunks2)
        for c1, c2 in zip(chunks1, chunks2):
            assert c1.chunk_id == c2.chunk_id
            assert c1.content_hash == c2.content_hash


class TestMaxTokenInvariant:
    """Every chunk's content_text must not exceed MAX_TOKENS (384)."""

    def test_all_chunks_within_limit(self):
        body = ("The Company hereby certifies that these financial statements " * 25 + "\n\n") * 100
        chunks = _chunk("Long Annual Report on Form 10-K", body)
        assert len(chunks) > 0
        for c in chunks:
            assert count_tokens(c.content_text) <= MAX_TOKENS, \
                f"Chunk {c.ordinal}: {count_tokens(c.content_text)} > {MAX_TOKENS}"

    def test_long_prefix_capped(self):
        title = "AAAA " * 50
        body = "BBBB " * 500
        prefix, prefix_count, _ = _prefix(title)
        assert prefix_count <= MAX_PREFIX_TOKENS
        chunks = _chunk(title, body)
        for c in chunks:
            assert c.prefix_token_count <= MAX_PREFIX_TOKENS
            assert count_tokens(c.content_text) <= MAX_TOKENS

    def test_content_text_starts_with_prefix(self):
        title = "Quarterly Report Q2 2026"
        body = "Analysis text. " * 100
        prefix, prefix_count, _ = _prefix(title)
        if prefix_count > 0:
            chunks = _chunk(title, body)
            for c in chunks:
                if c.prefix_token_count > 0:
                    assert c.content_text.startswith(prefix), \
                        f"Chunk {c.ordinal}: content_text missing prefix"


class TestSourceCodeAudit:
    """Production source must not contain forbidden truncation patterns."""

    def test_no_char_ratio_truncation(self):
        src = Path("packages/data-core/catalyst_data/corpus/news_v2.py").read_text()
        assert "len(content_text) * 0." not in src
        assert not re.search(r'content_text\[:\d+\]', src)

    def test_no_while_loop_truncation(self):
        src = Path("packages/data-core/catalyst_data/corpus/news_v2.py").read_text()
        assert "while count_tokens(content_text) > MAX_TOKENS:" not in src


class TestProgressLogging:
    """Progress callback emits at 1000-doc intervals or ~30s."""

    def test_callback_emits_on_event(self):
        from catalyst_data.index_builder import _CorpusProgress
        import time
        events = []
        p = _CorpusProgress(callback=events.append, clock=time.time,
                            rss_reader=lambda: 1024, interval=0.0)
        p.emit(phase="articles", processed=500, total=1000, chunks_generated=200)
        assert len(events) == 1
        assert events[0]["phase"] == "articles"
        assert "elapsed_seconds" in events[0]

    def test_emits_at_1000_threshold(self):
        from catalyst_data.index_builder import _CorpusProgress
        import time
        events = []
        p = _CorpusProgress(callback=events.append, clock=time.time,
                            rss_reader=lambda: 0, interval=60.0)
        p.emit(phase="articles", processed=1000, total=2000, chunks_generated=400)
        assert len(events) == 1

    def test_final_always_emits(self):
        from catalyst_data.index_builder import _CorpusProgress
        import time
        events = []
        p = _CorpusProgress(callback=events.append, clock=time.time,
                            rss_reader=lambda: 0, interval=60.0)
        p.emit(phase="filings", processed=42, total=42, chunks_generated=10, final=True)
        assert len(events) == 1

    def test_null_callback_noop(self):
        from catalyst_data.index_builder import _CorpusProgress
        import time
        p = _CorpusProgress(callback=None, clock=time.time,
                            rss_reader=lambda: 0, interval=0.0)
        p.emit(phase="x", processed=1000, total=2000, chunks_generated=0, final=True)

    def test_negative_interval_raises(self):
        from catalyst_data.index_builder import _CorpusProgress
        import time
        with pytest.raises(ValueError, match="progress_interval"):
            _CorpusProgress(callback=None, clock=time.time,
                            rss_reader=lambda: 0, interval=-1.0)

    def test_callback_exception_logged(self, caplog):
        from catalyst_data.index_builder import _CorpusProgress
        import time, logging
        caplog.set_level(logging.WARNING)
        p = _CorpusProgress(
            callback=lambda e: (_ for _ in ()).throw(RuntimeError("boom")),
            clock=time.time, rss_reader=lambda: 0, interval=0.0)
        p.emit(phase="articles", processed=1, total=10, chunks_generated=0)
        assert "corpus progress callback failed" in caplog.text
