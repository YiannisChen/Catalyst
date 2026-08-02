"""Tests for canonical persisted document ID validator."""
from __future__ import annotations

import pytest
from catalyst_data.corpus.persisted_id import is_valid_persisted_document_id


class TestPersistedIdValidator:
    def test_valid_lowercase_64_hex(self):
        assert is_valid_persisted_document_id("a" * 64)

    def test_valid_mixed_hex_chars(self):
        assert is_valid_persisted_document_id("0" * 32 + "f" * 32)

    def test_valid_all_digits(self):
        assert is_valid_persisted_document_id("0123456789" * 6 + "0123")

    def test_invalid_uppercase(self):
        assert not is_valid_persisted_document_id("A" * 64)

    def test_invalid_nonhex(self):
        assert not is_valid_persisted_document_id("g" * 64)

    def test_invalid_short(self):
        assert not is_valid_persisted_document_id("a" * 63)

    def test_invalid_long(self):
        assert not is_valid_persisted_document_id("a" * 65)

    def test_invalid_none(self):
        assert not is_valid_persisted_document_id(None)

    def test_invalid_empty_string(self):
        assert not is_valid_persisted_document_id("")

    def test_invalid_int(self):
        assert not is_valid_persisted_document_id(42)

    def test_invalid_special_chars(self):
        assert not is_valid_persisted_document_id("!" * 64)

    def test_filing_v2_path_not_misclassified(self):
        """Non-persisted (legacy) IDs must not pass the validator."""
        # Legacy IDs: colon-separated, shorter than 64, uppercase, etc.
        assert not is_valid_persisted_document_id("sec:0000320193:0000320193-26-000013")
        assert not is_valid_persisted_document_id("sec:f1:primary_doc")
        assert not is_valid_persisted_document_id("poly:test1")
        assert not is_valid_persisted_document_id("ABCD" + "a" * 60)
