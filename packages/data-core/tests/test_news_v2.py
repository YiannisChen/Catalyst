"""Tests for news_v2 chunk profile — deterministic semantic-window algorithm."""
from __future__ import annotations

import hashlib
import unicodedata


def test_news_v2_short_document_one_chunk():
    """Short news (≤384 tokens) emits exactly one chunk with section_key='body'."""
    from catalyst_data.corpus.news_v2 import NewsV2Profile

    profile = NewsV2Profile()
    article = {
        "document_id": "poly:test1",
        "title": "Fed Update",
        "description": "The Federal Reserve held rates steady today.",
        "available_at": "2026-01-15T14:00:00Z",
        "ticker_associations": '["AAPL"]',
    }
    chunks = profile.chunk(article)
    assert len(chunks) == 1
    c = chunks[0]
    assert c.section_key == "body"
    assert c.ordinal == "0001"
    assert "Fed Update" in c.content_text
    assert "held rates steady" in c.content_text
    assert c.body_overlap_tokens == 0


def test_news_v2_chunk_id_format():
    """Chunk ID follows contract: document_id:profile:section_key:ordinal."""
    from catalyst_data.corpus.news_v2 import NewsV2Profile

    profile = NewsV2Profile()
    article = {
        "document_id": "poly:test2",
        "title": "Markets Rally",
        "description": "Stocks rose sharply.",
        "available_at": "2026-01-15T14:00:00Z",
        "ticker_associations": '["AAPL"]',
    }
    chunks = profile.chunk(article)
    c = chunks[0]
    assert c.chunk_id == "poly:test2:news_v2:body:0001"
    assert c.document_id == "poly:test2"
    assert c.chunk_profile_version == "news_v2"


def test_news_v2_metadata_not_in_content():
    """Content text contains only normalized title + description, never metadata."""
    from catalyst_data.corpus.news_v2 import NewsV2Profile

    profile = NewsV2Profile()
    article = {
        "document_id": "poly:test3",
        "title": "Title",
        "description": "Description.",
        "available_at": "2026-01-15T14:00:00Z",
        "ticker_associations": '["AAPL"]',
        "image_url": "https://example.com/img.jpg",
        "article_url": "https://example.com/article",
    }
    chunks = profile.chunk(article)
    c = chunks[0]
    assert "img.jpg" not in c.content_text
    assert "example.com/article" not in c.content_text
    # Just title + description
    assert c.content_text == "Title\nDescription."


def test_news_v2_normalization_applied():
    """CRLF → LF, NFC normalization, trailing whitespace stripped."""
    from catalyst_data.corpus.news_v2 import NewsV2Profile

    profile = NewsV2Profile()
    # Include CRLF line endings and trailing spaces
    article = {
        "document_id": "poly:test4",
        "title": "Title  ",
        "description": "Line1\r\nLine2  \r\n\r\nLine3",
        "available_at": "2026-01-15T14:00:00Z",
        "ticker_associations": '["AAPL"]',
    }
    chunks = profile.chunk(article)
    c = chunks[0]
    # CRLF → LF, trailing whitespace per line stripped
    assert "\r" not in c.content_text
    assert "Title" in c.content_text
    assert "Title  " not in c.content_text


def test_news_v2_empty_description():
    """Article with empty description still chunks (title only)."""
    from catalyst_data.corpus.news_v2 import NewsV2Profile

    profile = NewsV2Profile()
    article = {
        "document_id": "poly:test5",
        "title": "Title Only",
        "description": "",
        "available_at": "2026-01-15T14:00:00Z",
        "ticker_associations": '["AAPL"]',
    }
    chunks = profile.chunk(article)
    assert len(chunks) >= 1
    # content_text should be just the title, no trailing newline
    assert chunks[0].content_text == "Title Only"


def test_news_v2_content_hash_is_deterministic():
    """Same content produces same content_hash."""
    from catalyst_data.corpus.news_v2 import NewsV2Profile

    profile = NewsV2Profile()
    article = {
        "document_id": "poly:test6",
        "title": "T",
        "description": "D",
        "available_at": "2026-01-15T14:00:00Z",
        "ticker_associations": '["AAPL"]',
    }
    c1 = profile.chunk(article)
    c2 = profile.chunk(article)
    assert c1[0].content_hash == c2[0].content_hash


def test_news_v2_boundary_kinds_are_valid():
    """Every chunk has a valid boundary_kind."""
    from catalyst_data.corpus.news_v2 import NewsV2Profile

    profile = NewsV2Profile()
    VALID_KINDS = {"document_end", "paragraph", "sentence", "token_fallback"}

    article = {
        "document_id": "poly:test7",
        "title": "Long Article About Markets",
        "description": (
            "First paragraph with enough text to create multiple chunks. "
            "Second paragraph with more text continues here. "
            "Third paragraph with even more text. "
            "Fourth section discussing various market topics. "
            "Fifth paragraph about economic indicators. "
            "Sixth paragraph about corporate earnings. "
            "Seventh paragraph about global trade. "
            "Eighth paragraph about interest rates. "
            "Ninth paragraph about currency markets. "
            "Tenth paragraph wrapping up the analysis. "
            "Eleventh paragraph with additional context. "
            "Twelfth paragraph for good measure."
        ),
        "available_at": "2026-01-15T14:00:00Z",
        "ticker_associations": '["AAPL"]',
    }
    chunks = profile.chunk(article)
    for c in chunks:
        assert c.boundary_kind in VALID_KINDS, f"Invalid boundary_kind: {c.boundary_kind}"


def test_news_v2_no_chunk_exceeds_max_tokens():
    """No chunk exceeds 384 token maximum."""
    from catalyst_data.corpus.news_v2 import NewsV2Profile
    from catalyst_data.corpus.tokenizer import count_tokens

    profile = NewsV2Profile()
    article = {
        "document_id": "poly:test8",
        "title": "Markets",
        "description": (
            "Paragraph one with substantial content about market movements. "
            "Paragraph two continues with more details about trading activity. "
            "Paragraph three discusses sector performance across the board. "
            "Paragraph four covers international market developments. "
            "Paragraph five analyzes economic data releases. "
            "Paragraph six reviews corporate earnings reports. "
            "Paragraph seven examines currency fluctuations. "
            "Paragraph eight discusses commodity price movements. "
            "Paragraph nine covers bond market activity. "
            "Paragraph ten summarizes the trading day. "
            "Paragraph eleven with additional context about futures. "
            "Paragraph twelve about options market activity. "
            "Paragraph thirteen discusses ETF flows. "
            "Paragraph fourteen about mutual fund performance. "
            "Paragraph fifteen covers hedge fund activity. "
            "Paragraph sixteen about pension fund allocations."
        ),
        "available_at": "2026-01-15T14:00:00Z",
        "ticker_associations": '["AAPL"]',
    }
    chunks = profile.chunk(article)
    for c in chunks:
        tokens = count_tokens(c.content_text)
        assert tokens <= 384, (
            f"Chunk {c.ordinal} exceeds max: {tokens} tokens"
        )


def test_news_v2_uses_tokenizer_offsets_for_exact_body_slices():
    """Window text is sliced by offset mappings, never tokenizer.decode length."""
    from catalyst_data.corpus.news_v2 import NewsV2Profile, _normalize_text
    from catalyst_data.corpus.tokenizer import get_tokenizer

    title = "Market microstructure"
    description = (
        "alpha-beta/email@example_com cannot/should_not move 中文测试 " * 120
    )
    document = {
        "document_id": "poly:offsets",
        "title": title,
        "description": description,
        "available_at": "2026-01-15T14:00:00Z",
        "ticker_associations": '["AAPL"]',
    }
    normalized = _normalize_text(f"{title}\n{description}")
    offsets = get_tokenizer()(
        normalized,
        add_special_tokens=False,
        return_offsets_mapping=True,
    )["offset_mapping"]

    chunks = NewsV2Profile().chunk(document)
    assert len(chunks) > 1
    for chunk in chunks:
        expected_body = normalized[
            offsets[chunk.body_token_start][0]:
            offsets[chunk.body_token_end - 1][1]
        ]
        assert chunk.content_text == f"{title}\n{expected_body}"
        assert chunk.prefix_token_count <= 64


def test_news_v2_matches_independent_boundary_oracle():
    """Paragraph priority, starts, ends, and overlap match the frozen oracle."""
    from catalyst_data.corpus.news_v2 import NewsV2Profile
    from corpus_fixtures import (
        EXPECTED_BODY_ENDS,
        EXPECTED_BODY_OVERLAPS,
        EXPECTED_BODY_STARTS,
        EXPECTED_BOUNDARY_CHUNK_TEXTS,
        EXPECTED_BOUNDARY_KINDS,
        deterministic_boundary_article_fixture,
    )

    chunks = NewsV2Profile().chunk(deterministic_boundary_article_fixture())
    assert [chunk.content_text for chunk in chunks] == EXPECTED_BOUNDARY_CHUNK_TEXTS
    assert [chunk.boundary_kind for chunk in chunks] == EXPECTED_BOUNDARY_KINDS
    assert [chunk.body_token_start for chunk in chunks] == EXPECTED_BODY_STARTS
    assert [chunk.body_token_end for chunk in chunks] == EXPECTED_BODY_ENDS
    assert [chunk.body_overlap_tokens for chunk in chunks] == EXPECTED_BODY_OVERLAPS


def test_news_v2_long_prefix_and_token_fallback_fixtures():
    from catalyst_data.corpus.news_v2 import NewsV2Profile
    from catalyst_data.corpus.tokenizer import count_tokens
    from corpus_fixtures import long_title_article_fixture, no_boundary_article_fixture

    titled = NewsV2Profile().chunk(long_title_article_fixture())
    assert all(chunk.prefix_truncated is True for chunk in titled)
    assert all(chunk.prefix_token_count <= 64 for chunk in titled)
    assert all(count_tokens(chunk.content_text) <= 384 for chunk in titled)

    fallback = NewsV2Profile().chunk(no_boundary_article_fixture())
    assert all(
        chunk.boundary_kind in {"token_fallback", "document_end"}
        for chunk in fallback
    )
    assert all(
        right.body_token_start > left.body_token_start
        for left, right in zip(fallback, fallback[1:])
    )
