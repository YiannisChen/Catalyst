"""B2 — redaction tests: request fingerprint, secret stripping, cursor hashing."""
from __future__ import annotations

import hashlib
import pytest


class TestRedaction:
    """Connector-side secret redaction and request fingerprinting."""

    def test_redact_request_strips_api_key_from_url(self):
        """apiKey in URL is removed from redacted params."""
        from catalyst_data.ingestion.redaction import redact_request

        req = {
            "method": "GET",
            "url": "https://api.polygon.io/v2/reference/news?ticker=AAPL&apiKey=SECRET123&limit=50",
            "headers": {},
            "params": {"ticker": "AAPL", "apiKey": "SECRET123", "limit": "50"},
            "body": None,
            "provider_profile_version": "v1",
        }
        redacted = redact_request(req)
        clean_params = redacted.get("sorted_redacted_params", {})
        assert "apikey" not in {k.lower() for k in clean_params}
        assert clean_params.get("ticker") == "AAPL"

    def test_redact_request_strips_authorization_header(self):
        """Authorization header is stripped."""
        from catalyst_data.ingestion.redaction import redact_request

        req = {
            "method": "GET",
            "url": "https://api.example.com/data",
            "headers": {"Authorization": "Bearer TOKEN123", "Accept": "application/json"},
            "params": {},
            "body": None,
            "provider_profile_version": "v1",
        }
        redacted = redact_request(req)
        headers = redacted.get("headers", {})
        assert "Authorization" not in headers
        assert headers.get("Accept") == "application/json"

    def test_compute_request_fingerprint_deterministic(self):
        """Same redacted request → same fingerprint."""
        from catalyst_data.ingestion.redaction import compute_request_fingerprint

        redacted = {
            "method": "GET",
            "normalized_host": "api.polygon.io",
            "normalized_path": "/v2/reference/news",
            "sorted_redacted_params": {"limit": "50", "ticker": "AAPL"},
            "request_body_sha256": hashlib.sha256(b"").hexdigest(),
            "provider_profile_version": "v1",
        }
        fp1 = compute_request_fingerprint(redacted)
        fp2 = compute_request_fingerprint(redacted)
        assert fp1 == fp2
        assert len(fp1) == 64

    def test_compute_request_fingerprint_different_params(self):
        """Different params → different fingerprint."""
        from catalyst_data.ingestion.redaction import compute_request_fingerprint

        r1 = {"method": "GET", "normalized_host": "h", "normalized_path": "/p",
              "sorted_redacted_params": {"ticker": "AAPL"}, "request_body_sha256": hashlib.sha256(b"").hexdigest(),
              "provider_profile_version": "v1"}
        r2 = {"method": "GET", "normalized_host": "h", "normalized_path": "/p",
              "sorted_redacted_params": {"ticker": "MSFT"}, "request_body_sha256": hashlib.sha256(b"").hexdigest(),
              "provider_profile_version": "v1"}
        assert compute_request_fingerprint(r1) != compute_request_fingerprint(r2)

    def test_compute_cursor_fingerprint_hashes_cursor(self):
        """Pagination cursor is hashed, not stored raw."""
        from catalyst_data.ingestion.redaction import compute_cursor_fingerprint

        cursor = "https://api.polygon.io/v2/reference/news?cursor=abc123def456"
        fp = compute_cursor_fingerprint(cursor)
        assert fp != cursor
        assert len(fp) == 64

    def test_full_redact_and_fingerprint_pipeline(self):
        """End-to-end: redact a real-looking request, fingerprint it."""
        from catalyst_data.ingestion.redaction import redact_request, compute_request_fingerprint

        req = {
            "method": "GET",
            "url": "https://api.polygon.io/v2/reference/news?ticker=AAPL&apiKey=sk-live-abcdef123456&limit=50",
            "headers": {"Authorization": "Bearer sk-live-abcdef123456"},
            "params": {"ticker": "AAPL", "apiKey": "sk-live-abcdef123456", "limit": "50"},
            "body": None,
            "provider_profile_version": "v1",
        }
        redacted = redact_request(req)
        fp = compute_request_fingerprint(redacted)
        assert "sk-live-abcdef123456" not in fp
        assert "apiKey" not in str(redacted.get("sorted_redacted_params", {}))
        assert "Authorization" not in str(redacted.get("headers", {}))
