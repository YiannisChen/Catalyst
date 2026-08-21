"""M3-5: conservative URL normalization tests (execution-lock §F)."""
from __future__ import annotations

from catalyst_data.articles.url_normalize import NormalizedUrl, normalize_url


def test_normalize_url_lowercases_scheme_host_and_strips_fragment():
    result = normalize_url("HTTPS://WWW.Example.COM/Path#frag")
    assert result.unknown is False
    assert result.value == "https://www.example.com/Path"


def test_normalize_url_sorts_query_and_drops_tracking_prefixes():
    url = (
        "https://foo.com/path?utm_source=x&b=2&a=1&ref_src=y&mc_cid=z&plain=1"
    )
    result = normalize_url(url)
    assert result.value == "https://foo.com/path?a=1&b=2&plain=1"


def test_normalize_url_keeps_blank_query_values():
    result = normalize_url("https://foo.com/path?flag=&a=1")
    assert result.value == "https://foo.com/path?a=1&flag="


def test_normalize_url_removes_default_ports():
    a = normalize_url("https://foo.com:443/path?a=1")
    b = normalize_url("https://foo.com/path?a=1")
    assert a.value == b.value == "https://foo.com/path?a=1"
    c = normalize_url("http://foo.com:80/path")
    d = normalize_url("http://foo.com/path")
    assert c.value == d.value == "http://foo.com/path"


def test_normalize_url_keeps_nondefault_port():
    result = normalize_url("https://foo.com:8443/path?a=1")
    assert result.value == "https://foo.com:8443/path?a=1"


def test_normalize_url_preserves_path_case():
    result = normalize_url("https://foo.com/Apple/News")
    assert result.value == "https://foo.com/Apple/News"


def test_normalize_url_unknown_for_malformed_or_non_http():
    for bad in (
        None,
        "",
        "   ",
        "not a url",
        "ftp://foo.com/x",
        "file:///etc/passwd",
        "http://",
        "https://foo.com:abc/path",
    ):
        result = normalize_url(bad)
        assert result.unknown is True, bad
        assert result.value is None


def test_finnhub_vs_polygon_same_article_urls_match():
    finnhub = normalize_url(
        "https://www.cnbc.com/2026/01/05/apple-earnings.html?utm_source=finnhub&ref=1"
    )
    polygon = normalize_url(
        "https://WWW.CNBC.COM/2026/01/05/apple-earnings.html?ref=1&utm_source=polygon"
    )
    assert finnhub.unknown is False and polygon.unknown is False
    assert finnhub.value == polygon.value


def test_redirect_and_tracking_variants_match():
    plain = normalize_url("https://example.com/news/apple-ai")
    with_tracking = normalize_url(
        "https://example.com/news/apple-ai?utm_medium=organic&mc_cid=abc"
    )
    assert plain.value == "https://example.com/news/apple-ai"
    assert plain.value == with_tracking.value


def test_unrelated_urls_stay_separate():
    a = normalize_url("https://example.com/news/apple")
    b = normalize_url("https://example.com/news/microsoft")
    assert a.value != b.value
