"""RED: lossless-window contract tests for the Pre-B6 chunk pipeline.

Every test MUST genuinely fail against the current news_v2 implementation
(which uses post-hoc trimming with 0.95 ratio, body-only fallback, and
1000/500-char escape hatches).

Contract violations targeted:
  1. count_tokens(content_text) <= MAX_TOKENS (may pass coincidentally)
  2. body_token_start/end describe exact body slice (stale after post-hoc trim)
  3. Union of [body_token_start,body_token_end) covers [0,token_count) with
     no gaps (stale windows break this)
  4. body_overlap_tokens == actual intersection and <= MAX_OVERLAP
  5. prefix preserved; long prefix truncated only via MAX_PREFIX_TOKENS
  6. No proportional/char-ratio fallback used
  7. Unicode, long title, long paragraph, sentence fallback, short tail
  8. Deterministic chunk IDs / hashes / ordinals / ranges
"""
from __future__ import annotations

import hashlib
import inspect
import pytest

from catalyst_data.corpus.news_v2 import (
    MAX_TOKENS, MAX_OVERLAP, MAX_PREFIX_TOKENS, TARGET_TOKENS,
    _chunk_text, _find_boundaries, _normalize_text, _prefix, _encoding,
    count_tokens,
)


def _wrap_doc(text: str, title: str = "Test Title") -> dict:
    return {
        "document_id": hashlib.sha256(text.encode()).hexdigest(),
        "document_url": "test",
    }


# ---------------------------------------------------------------------------
# Test 1: every chunk count_tokens(content_text) <= MAX_TOKENS
# ---------------------------------------------------------------------------

class TestTokenBudgetPerChunk:
    """Every emitted chunk must fit inside MAX_TOKENS (384)."""

    def test_short_document_fits(self):
        """Single short doc: one chunk, under limit."""
        doc = _wrap_doc("Short sentence.")
        chunks = _chunk_text(document=doc, text="Short sentence.",
                             prefix_title="Test", profile_version="test",
                             section_key="body",
                             short_document_has_no_prefix=True)
        for c in chunks:
            assert count_tokens(c.content_text) <= MAX_TOKENS

    def test_long_paragraph_fits(self):
        """A many-token paragraph must never produce an over-limit chunk."""
        word = "market " * 100
        text = word + "\n\n" + word  # two paragraphs, no sentence breaks
        doc = _wrap_doc(text)
        chunks = _chunk_text(document=doc, text=text,
                             prefix_title="Short Title", profile_version="test",
                             section_key="body",
                             short_document_has_no_prefix=False)
        for c in chunks:
            tc = count_tokens(c.content_text)
            assert tc <= MAX_TOKENS, f"chunk {c.ordinal} has {tc} tokens"

    def test_long_title_prefix_fits(self):
        """A 64-token prefix + large body must stay under MAX_TOKENS."""
        title = "AAAA " * 50  # long title, will hit MAX_PREFIX_TOKENS
        body = "BBBB " * 700
        doc = _wrap_doc(body)
        prefix, prefix_count, _ = _prefix(title)
        assert prefix_count <= MAX_PREFIX_TOKENS
        chunks = _chunk_text(document=doc, text=body, prefix_title=title,
                             profile_version="test", section_key="body",
                             short_document_has_no_prefix=False)
        for c in chunks:
            tc = count_tokens(c.content_text)
            assert tc <= MAX_TOKENS, (
                f"chunk {c.ordinal} exceeds limit: {tc} > {MAX_TOKENS}"
            )

    def test_unicode_multibyte_fits(self):
        """CJK text must chunk without overflow."""
        word = "测试" * 40  # ~80 CJK chars
        text = (word + "\n") * 50
        doc = _wrap_doc(text)
        chunks = _chunk_text(document=doc, text=text,
                             prefix_title="中文测试", profile_version="test",
                             section_key="body",
                             short_document_has_no_prefix=False)
        for c in chunks:
            assert count_tokens(c.content_text) <= MAX_TOKENS


# ---------------------------------------------------------------------------
# Test 2: body_token_start/end describe exact included normalized-body slice
# ---------------------------------------------------------------------------

class TestBodySliceAccuracy:
    """body_token_start/end must reflect the actual content emitted, not
    the pre-trim window boundaries."""

    def test_body_slice_matches_content(self):
        """Every chunk's body_token_start/end recovers the exact body text."""
        text = ("The quick brown fox jumps over the lazy dog. " * 200)
        doc = _wrap_doc(text)
        normalized = _normalize_text(text)
        _, offsets = _encoding(normalized)
        prefix, prefix_count, _ = _prefix("Quick Fox")
        chunks = _chunk_text(document=doc, text=text,
                             prefix_title="Quick Fox",
                             profile_version="test", section_key="body",
                             short_document_has_no_prefix=False)
        for c in chunks:
            start = c.body_token_start
            end = c.body_token_end
            reconstructed_body = normalized[offsets[start][0]:offsets[end - 1][1]]
            expected = c.content_text
            if prefix:
                expected = expected[len(prefix):]
            # The body slice (reconstructed from start/end) must match
            # the body portion of the content_text
            assert reconstructed_body == expected, (
                f"chunk {c.ordinal}: body slice mismatch\n"
                f"  start={start} end={end}\n"
                f"  reconstructed={reconstructed_body!r}\n"
                f"  expected={expected!r}"
            )

    def test_body_token_start_monotonic(self):
        """body_token_start increases strictly across chunks."""
        text = ("Paragraph one about markets. " * 150 +
                "\n\n" +
                "Paragraph two about bonds. " * 150)
        doc = _wrap_doc(text)
        chunks = _chunk_text(document=doc, text=text,
                             prefix_title="Markets", profile_version="test",
                             section_key="body",
                             short_document_has_no_prefix=False)
        assert len(chunks) >= 2, f"Need >=2 chunks, got {len(chunks)}"
        for i in range(1, len(chunks)):
            assert chunks[i].body_token_start > chunks[i-1].body_token_start, (
                f"chunk {i}: start not monotonic"
            )


# ---------------------------------------------------------------------------
# Test 3: union of ranges covers [0, token_count) with no gaps
# ---------------------------------------------------------------------------

class TestRangeCoverage:
    """The union of [body_token_start, body_token_end) must cover the full
    body with no gaps."""

    def test_union_covers_full_range(self):
        """All body_token ranges together cover [0, token_count)."""
        text = ("The Federal Reserve held rates steady. " * 80 +
                "\n\n" +
                "The European Central Bank cut rates. " * 80 +
                "\n\n" +
                "The Bank of Japan maintained policy. " * 80)
        doc = _wrap_doc(text)
        normalized = _normalize_text(text)
        token_ids, _ = _encoding(normalized)
        token_count = len(token_ids)
        chunks = _chunk_text(document=doc, text=text,
                             prefix_title="Central Banks",
                             profile_version="test", section_key="body",
                             short_document_has_no_prefix=False)
        assert len(chunks) >= 1

        covered = set()
        for c in chunks:
            for t in range(c.body_token_start, c.body_token_end):
                covered.add(t)

        expected = set(range(token_count))
        missing = expected - covered
        extra = covered - expected
        assert not missing, (
            f"Missing tokens: {sorted(missing)[:20]}..."
            if len(missing) > 20
            else f"Missing token indices: {sorted(missing)}"
        )
        assert not extra, f"Extra token indices: {sorted(extra)}"

    def test_no_gaps_between_ranges(self):
        """Adjacent chunk ranges have no gaps."""
        text = "sentence. " * 300
        doc = _wrap_doc(text)
        chunks = _chunk_text(document=doc, text=text,
                             prefix_title="Test", profile_version="test",
                             section_key="body",
                             short_document_has_no_prefix=False)
        assert len(chunks) >= 2
        for i in range(1, len(chunks)):
            assert chunks[i].body_token_start <= chunks[i-1].body_token_end, (
                f"Gap between chunk {i-1} (end={chunks[i-1].body_token_end}) "
                f"and chunk {i} (start={chunks[i].body_token_start})"
            )


# ---------------------------------------------------------------------------
# Test 4: body_overlap_tokens == actual intersection and <= MAX_OVERLAP
# ---------------------------------------------------------------------------

class TestOverlapMetadata:
    """body_overlap_tokens must reflect actual token intersection."""

    def test_overlap_matches_actual(self):
        """Each chunk's overlap equals the reported value."""
        text = ("Paragraph one with market data. " * 100 +
                "\n\n" +
                "Paragraph two with bond data. " * 100)
        doc = _wrap_doc(text)
        chunks = _chunk_text(document=doc, text=text,
                             prefix_title="Data", profile_version="test",
                             section_key="body",
                             short_document_has_no_prefix=False)
        for i in range(1, len(chunks)):
            prev_end = chunks[i-1].body_token_end
            curr_start = chunks[i].body_token_start
            actual_overlap = max(0, prev_end - curr_start)
            assert chunks[i].body_overlap_tokens == actual_overlap, (
                f"chunk {i}: overlap mismatch: "
                f"reported={chunks[i].body_overlap_tokens} "
                f"actual={actual_overlap}"
            )
            assert chunks[i].body_overlap_tokens <= MAX_OVERLAP, (
                f"chunk {i}: overlap {chunks[i].body_overlap_tokens} > {MAX_OVERLAP}"
            )


# ---------------------------------------------------------------------------
# Test 5: prefix always present; overflow cannot drop it
# ---------------------------------------------------------------------------

class TestPrefixPreservation:
    """Prefix must be in every chunk's content_text, exactly as computed."""

    def test_prefix_always_present(self):
        """Every chunk includes the computed prefix."""
        text = "body content. " * 200
        doc = _wrap_doc(text)
        prefix, _, _ = _prefix("My Title")
        assert prefix, "prefix must be non-empty"
        chunks = _chunk_text(document=doc, text=text,
                             prefix_title="My Title",
                             profile_version="test", section_key="body",
                             short_document_has_no_prefix=False)
        for c in chunks:
            assert c.content_text.startswith(prefix), (
                f"chunk {c.ordinal}: prefix missing"
            )

    def test_long_title_prefix_truncated_correctly(self):
        """Very long title truncates at MAX_PREFIX_TOKENS, still present."""
        title = " ".join(["supercalifragilisticexpialidocious"] * 20)
        body = "body. " * 100
        doc = _wrap_doc(body)
        prefix, prefix_count, truncated = _prefix(title)
        assert truncated, "prefix should be truncated for this long title"
        assert prefix_count <= MAX_PREFIX_TOKENS
        assert len(prefix) > 0
        chunks = _chunk_text(document=doc, text=body, prefix_title=title,
                             profile_version="test", section_key="body",
                             short_document_has_no_prefix=False)
        for c in chunks:
            assert c.content_text.startswith(prefix), (
                f"chunk {c.ordinal}: long prefix lost"
            )


# ---------------------------------------------------------------------------
# Test 6: no proportional/char-ratio fallback; no silent drops
# ---------------------------------------------------------------------------

class TestNoCharRatioFallback:
    """Source code must not contain character-ratio or fixed-char fallbacks."""

    def test_no_proportional_fallback(self):
        """0.95 ratio must not appear in news_v2 source."""
        content = open("packages/data-core/catalyst_data/corpus/news_v2.py").read()
        assert "0.95" not in content, "0.95 ratio fallback found"
        assert "int(len(body)" not in content, "char-based truncation found"

    def test_no_fixed_char_limits(self):
        """No 1000/500-char escape hatches in source."""
        content = open("packages/data-core/catalyst_data/corpus/news_v2.py").read()
        assert "[:1000]" not in content, "1000-char fallback found"
        assert "[:500]" not in content, "500-char fallback found"

    def test_no_while_loop_trimming(self):
        """No while loop that trims content after window emission."""
        content = open("packages/data-core/catalyst_data/corpus/news_v2.py").read()
        assert "while count_tokens(content_text) > MAX_TOKENS" not in content, (
            "while-loop overflow trimming found"
        )


# ---------------------------------------------------------------------------
# Test 7: edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Unicode, long title, long paragraph, sentence fallback, short tail."""

    def test_cjk_long_paragraph_no_overflow(self):
        """CJK long paragraph chunks correctly."""
        text = "日中韓の市場動向について。株価が上昇した。" * 60
        doc = _wrap_doc(text)
        chunks = _chunk_text(document=doc, text=text,
                             prefix_title="アジア市場", profile_version="test",
                             section_key="body",
                             short_document_has_no_prefix=False)
        for c in chunks:
            assert count_tokens(c.content_text) <= MAX_TOKENS

    def test_long_paragraph_no_boundaries(self):
        """Long paragraph with no sentence breaks: token_fallback boundary."""
        text = "market data analysis report " * 200
        doc = _wrap_doc(text)
        chunks = _chunk_text(document=doc, text=text,
                             prefix_title="Report", profile_version="test",
                             section_key="body",
                             short_document_has_no_prefix=False)
        for c in chunks:
            assert c.boundary_kind in ("token_fallback", "document_end")


    def test_empty_text(self):
        """Empty text produces zero chunks."""
        doc = _wrap_doc("")
        chunks = _chunk_text(document=doc, text="",
                             prefix_title="Test", profile_version="test",
                             section_key="body",
                             short_document_has_no_prefix=True)
        assert len(chunks) == 0


# ---------------------------------------------------------------------------
# Test 8: deterministic chunks/IDs/hashes/ordinals/ranges
# ---------------------------------------------------------------------------

class TestDeterminism:
    """Same input produces identical output every time."""

    def test_idempotent_chunks(self):
        """Two calls with same input produce identical results."""
        text = "data. " * 150
        doc = _wrap_doc(text)
        chunks1 = _chunk_text(document=doc, text=text,
                              prefix_title="Test", profile_version="test",
                              section_key="body",
                              short_document_has_no_prefix=False)
        chunks2 = _chunk_text(document=doc, text=text,
                              prefix_title="Test", profile_version="test",
                              section_key="body",
                              short_document_has_no_prefix=False)
        assert len(chunks1) == len(chunks2)
        for i, (c1, c2) in enumerate(zip(chunks1, chunks2)):
            assert c1.chunk_id == c2.chunk_id, f"chunk {i}: ID differs"
            assert c1.content_hash == c2.content_hash, f"chunk {i}: hash differs"
            assert c1.ordinal == c2.ordinal, f"chunk {i}: ordinal differs"
            assert c1.body_token_start == c2.body_token_start
            assert c1.body_token_end == c2.body_token_end
            assert c1.body_overlap_tokens == c2.body_overlap_tokens

    def test_chunk_ids_contain_profile_version(self):
        """Chunk IDs include the profile version string."""
        text = "test. " * 50
        doc = _wrap_doc(text)
        chunks = _chunk_text(document=doc, text=text,
                             prefix_title="Test", profile_version="filing_v3",
                             section_key="item7",
                             short_document_has_no_prefix=False)
        for c in chunks:
            assert "filing_v3" in c.chunk_id

    def test_ordinals_are_zero_padded(self):
        """Ordinals are 4-digit zero-padded."""
        text = "sentence. " * 300
        doc = _wrap_doc(text)
        chunks = _chunk_text(document=doc, text=text,
                             prefix_title="Test", profile_version="test",
                             section_key="body",
                             short_document_has_no_prefix=False)
        assert len(chunks) > 1
        for c in chunks:
            assert len(c.ordinal) == 4
            assert c.ordinal.isdigit()

# Fix: the short tail test must produce >=2 chunks.  "sentence one. " * 100
# is ~200 tokens with prefix — bump to *300 for reliable multi-chunk.

    def test_real_tokenizer_overflow_reduces_without_losing_body_tokens(self):
        """Prefix/body retokenization must shrink the real semantic window."""
        title = "A"
        text = ("🚀 " * 192).strip() + "\n\n" + ("后续 " * 500).strip()
        normalized = _normalize_text(text)
        token_ids, offsets = _encoding(normalized)
        paragraphs, _ = _find_boundaries(normalized, offsets)
        prefix, prefix_count, _ = _prefix(title)

        body_max = MAX_TOKENS - prefix_count
        body_target = TARGET_TOKENS - prefix_count
        hard = min(len(token_ids), body_max)
        target = min(len(token_ids), body_target)
        lower = max(1, target - MAX_OVERLAP)
        candidates = [value for value in paragraphs if lower <= value <= hard]
        candidate_end = min(
            candidates,
            key=lambda value: (abs(value - target), value),
        )
        candidate_body = normalized[offsets[0][0]:offsets[candidate_end - 1][1]]
        assert count_tokens(prefix + candidate_body) > MAX_TOKENS

        chunks = _chunk_text(
            document=_wrap_doc(text),
            text=text,
            prefix_title=title,
            profile_version="test",
            section_key="body",
            short_document_has_no_prefix=False,
        )

        assert chunks[0].body_token_end < candidate_end
        covered: set[int] = set()
        for index, chunk in enumerate(chunks):
            assert chunk.body_token_end > chunk.body_token_start
            reconstructed = normalized[
                offsets[chunk.body_token_start][0]:offsets[chunk.body_token_end - 1][1]
            ]
            assert chunk.content_text == prefix + reconstructed
            assert chunk.content_text.startswith(prefix)
            assert count_tokens(chunk.content_text) <= MAX_TOKENS
            covered.update(range(chunk.body_token_start, chunk.body_token_end))
            if index:
                intersection = max(
                    0, chunks[index - 1].body_token_end - chunk.body_token_start
                )
                assert chunk.body_overlap_tokens == intersection
                assert intersection <= MAX_OVERLAP

        assert covered == set(range(len(token_ids)))



    def test_short_final_tail_multi_chunk(self):
        """Final short tail must produce >=2 chunks with doc_end on last."""
        text = "sentence one. " * 300 + "short tail."
        doc = _wrap_doc(text)
        chunks = _chunk_text(document=doc, text=text,
                             prefix_title="Test", profile_version="test",
                             section_key="body",
                             short_document_has_no_prefix=False)
        assert len(chunks) >= 2, f"Need >=2 chunks, got {len(chunks)}"
        assert chunks[-1].boundary_kind == "document_end"
        assert count_tokens(chunks[-1].content_text) <= MAX_TOKENS


def test_sentence_rich_boundary_lookup_scales_structurally_linearly(monkeypatch):
    from catalyst_data.corpus import news_v2

    original_left = news_v2.bisect_left
    original_right = news_v2.bisect_right
    calls = {"count": 0}

    def counted_left(*args, **kwargs):
        calls["count"] += 1
        return original_left(*args, **kwargs)

    def counted_right(*args, **kwargs):
        calls["count"] += 1
        return original_right(*args, **kwargs)

    monkeypatch.setattr(news_v2, "bisect_left", counted_left)
    monkeypatch.setattr(news_v2, "bisect_right", counted_right)

    def operations(sentence_count: int) -> int:
        calls["count"] = 0
        text = "Alpha rose. Beta fell? " * sentence_count
        _chunk_text(
            document=_wrap_doc(text),
            text=text,
            prefix_title="Market",
            profile_version="test",
            section_key="body",
            short_document_has_no_prefix=False,
        )
        return calls["count"]

    n_ops = operations(1200)
    two_n_ops = operations(2400)
    assert n_ops > 0
    assert n_ops < two_n_ops <= 2 * n_ops + 16

    select_source = inspect.getsource(news_v2._select_boundary)
    map_source = inspect.getsource(news_v2._token_end_for_char)
    chunk_source = inspect.getsource(news_v2._iter_chunk_text)
    assert "for boundary in boundaries" not in select_source
    assert "for index" not in map_source
    assert chunk_source.count("set(paragraphs)") == 1
    assert chunk_source.count("set(sentences)") == 1
