"""Benzinga archive discovery: id->time estimate, slug candidates, page window."""
from catalyst_data.ingestion.collectors import benzinga
from catalyst_data.retrieval.web_bodies import names_subject

XML = (
    "<url><loc>https://www.benzinga.com/markets/equities/26/07/100/"
    "stock-market-today-dow-falls-as-oil-climbs</loc></url>"
    "<url><loc>https://www.benzinga.com/news/26/07/150/acme-stock-surges-after-guidance</loc></url>"
    "<url><loc>https://www.benzinga.com/analyst-stock-ratings/downgrades/26/07/160/"
    "this-analyst-is-no-longer-bullish-here-are-top-5-downgrades-for-wednesday</loc></url>"
    "<url><loc>https://www.benzinga.com/news/26/07/900/acme-stock-a-month-later</loc></url>"
)
T0 = 1_783_000_000.0  # calibration epoch


def test_parse_archive_and_estimate():
    entries = benzinga.parse_archive(XML)
    assert [e[0] for e in entries] == [100, 150, 160, 900]
    assert entries[0][3].startswith("stock market today")
    calib = [(50, T0), (250, T0 + 200)]
    assert benzinga.estimate_time(calib, 150) == T0 + 100
    assert benzinga.estimate_time(calib, 900) is None  # outside calibration


def test_candidates_by_slug_and_window(monkeypatch):
    monkeypatch.setattr(benzinga, "subject_matcher", lambda s: (lambda t: "acme" in t.lower()))
    calib = [(50, T0), (250, T0 + 3600), (1000, T0 + 40 * 86400)]
    start = "2026-07-01T00:00:00Z"
    from datetime import datetime, timezone
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    picked = benzinga.candidates(benzinga.parse_archive(XML), {"ACME": "target", "SPY": "market"},
                                 iso(T0 - 3600), iso(T0 + 7200), calib)
    urls = {url.rsplit("/", 2)[-2] for url, _kind, _named in picked}
    assert urls == {"100", "150", "160"}  # 900 is weeks later
    del start


def _page(published, modified, title, body):
    article = {"@type": "NewsArticle", "headline": title, "datePublished": published}
    if modified:
        article["dateModified"] = modified
    import json
    ld = json.dumps({"@context": "https://schema.org", "@graph": [
        {"@type": "WebPage", "datePublished": "2020-01-01T00:00:00Z"}, article]})
    return (f'<html><head><script type="application/ld+json">{ld}</script></head>'
            f"<body><article>{''.join(f'<p>{body} paragraph {i} with enough words to pass.</p>' for i in range(20))}"
            "</article></body></html>")


def test_page_row_enforces_window_and_modification(monkeypatch):
    monkeypatch.setattr(benzinga, "names_subject", lambda s, t, x: s == "ACME")
    start, cutoff = "2026-07-01T13:30:00Z", "2026-07-02T13:30:00Z"
    body = "Acme shares jumped after the company raised its guidance"
    ok = benzinga.page_row("https://www.benzinga.com/x", _page("2026-07-01T16:00:00Z", "2026-07-01T17:00:00Z",
                           "Acme Raises Guidance", body), {"ACME": "target"}, start, cutoff, "now")
    assert ok and ok["tickers"] == ["ACME"] and ok["eligible_at"] == "2026-07-01T16:00:00Z"
    late_edit = _page("2026-07-01T16:00:00Z", "2026-07-03T09:00:00Z", "Acme Raises Guidance", body)
    assert benzinga.page_row("https://www.benzinga.com/x", late_edit, {"ACME": "target"}, start, cutoff, "now") is None
    before = _page("2026-06-30T16:00:00Z", "2026-06-30T16:00:00Z", "Acme Raises Guidance", body)
    assert benzinga.page_row("https://www.benzinga.com/x", before, {"ACME": "target"}, start, cutoff, "now") is None
    # No version evidence: rejected.
    unversioned = _page("2026-07-01T16:00:00Z", None, "Acme Raises Guidance", body)
    assert benzinga.page_row("https://www.benzinga.com/x", unversioned, {"ACME": "target"}, start, cutoff, "now") is None


def test_names_subject_title_or_lead():
    cu = lambda t: "copper" in t.lower()
    lead = "London's FTSE rose. Miners gained as copper prices hit a record on supply worries."
    assert names_subject("CPER", "FTSE 100 record high, miners boost", lead, cu)
    co = lambda t: "acme" in t.lower()
    # A company must be named in the title (an article about another company
    # that mentions it in the lead is not about it).
    assert not names_subject("ACME", "Rival raises guidance", "Rival competes with Acme.", co)
    assert names_subject("ACME", "Acme falls after downgrade", "", co)


def test_kitco_page_version_bound_to_url_alias():
    import json

    from catalyst_data.ingestion.collectors import kitco
    data = {"props": {"related": [{"urlAlias": "/news/other", "createdAt": "2026-09-01T00:00:00-0400",
                                   "updatedAt": "2026-09-30T00:00:00-0400"}],
                      "node": {"urlAlias": "/news/off-the-wire/2026-07-30/x",
                               "createdAt": "2026-07-30T07:51:33-0400", "updatedAt": "2026-07-30T07:53:45-0400"}}}
    page = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'
    assert kitco.page_version("https://www.kitco.com/news/off-the-wire/2026-07-30/x", page) == (
        "2026-07-30T11:51:33Z", "2026-07-30T11:53:45Z")
    assert kitco.page_version("https://www.kitco.com/news/missing", page) is None
