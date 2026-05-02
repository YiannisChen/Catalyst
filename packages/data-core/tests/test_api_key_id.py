import hashlib

from catalyst_data.config import api_key_id


def test_api_key_id_returns_12_char_hash_prefix(monkeypatch):
    key = "secret-polygon-key"
    monkeypatch.setenv("POLYGON_API_KEY", key)

    expected = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    assert api_key_id("polygon") == expected


def test_api_key_id_returns_none_when_missing(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)

    assert api_key_id("fred") is None


def test_api_key_id_never_returns_raw_key(monkeypatch):
    key = "fmp-super-secret"
    monkeypatch.setenv("FMP_API_KEY", key)

    result = api_key_id("fmp")
    assert result is not None
    assert key not in result
    assert len(result) == 12
