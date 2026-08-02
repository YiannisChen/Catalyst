from __future__ import annotations

import hashlib

import pytest

from catalyst_data.corpus.filing_v3 import FilingV3Profile
from catalyst_data.corpus.news_v2 import NewsV2Profile


@pytest.mark.parametrize(
    ("profile", "document"),
    [
        (
            NewsV2Profile(),
            {
                "document_id": "poly:iterator",
                "title": "Iterator",
                "description": " ".join(["news"] * 900),
                "available_at": "2026-08-01T00:00:00Z",
            },
        ),
        (
            FilingV3Profile(),
            {
                "document_id": hashlib.sha256(b"iterator-filing").hexdigest(),
                "filing_type": "8-K",
                "raw_text": "Item 1.01 Agreement\n" + " ".join(["filing"] * 900),
                "available_at": "2026-08-01T00:00:00Z",
            },
        ),
    ],
)
def test_profiles_offer_lazy_iter_chunks_with_legacy_equivalent_output(profile, document):
    lazy = profile.iter_chunks(document)
    assert iter(lazy) is lazy
    assert list(lazy) == profile.chunk(document)
