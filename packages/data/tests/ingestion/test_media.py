from collections import Counter

from catalyst_data.ingestion import media
from catalyst_data.ingestion.collectors import fetch
from catalyst_data.ingestion.storage import store


def _meta(tag: str) -> str:
    return f"<html><head>{tag}</head><body></body></html>"


def test_page_image_extracts_supported_tags_and_resolves_relative_urls():
    page = "https://publisher.example/news/story"
    assert fetch.page_image(_meta('<meta property="og:image" content="https://cdn.example/a.jpg">'), page) == "https://cdn.example/a.jpg"
    assert fetch.page_image(_meta('<meta name="twitter:image" content="https://cdn.example/t.jpg">'), page) == "https://cdn.example/t.jpg"
    assert fetch.page_image(_meta('<meta property="og:image" content="../assets/a.jpg">'), page) == "https://publisher.example/assets/a.jpg"
    assert fetch.page_image(_meta('<link rel="image_src" href="/img/a.jpg">'), page) == "https://publisher.example/img/a.jpg"
    assert fetch.page_image_with_source(_meta('<meta name="twitter:image" content="/img/a.jpg">'), page) == (
        "https://publisher.example/img/a.jpg", "twitter")


def test_page_image_rejects_data_placeholders_logos_and_missing():
    page = "https://finance.yahoo.com/news/story"
    for value in (
        "data:image/png;base64,abc",
        "https://s.yimg.com/rz/stage/p/yahoo_finance_123.png",
        "https://cdn.example/assets/company-logo.png",
        "https://cdn.example/favicon.ico",
        "https://cdn.example/sprite.png",
        "https://cdn.example/1x1/pixel.png",
    ):
        assert fetch.page_image(_meta(f'<meta property="og:image" content="{value}">'), page) is None
    assert fetch.page_image("<html><head></head><body>no image</body></html>", page) is None
    assert fetch.page_image(_meta('<meta property="og:image" content="http://cdn.example/a.jpg">'), page) is None


def test_image_shared_by_more_than_twenty_articles_is_generic():
    assert media.is_placeholder("https://cdn.example/shared.jpg", Counter({"https://cdn.example/shared.jpg": 21}))
    assert not media.is_placeholder("https://cdn.example/shared.jpg", Counter({"https://cdn.example/shared.jpg": 20}))
    assert media.headline_image({"provider": "polygon", "image_url": "https://cdn.example/a.jpg"}) == (
        "https://cdn.example/a.jpg", "polygon")
    assert media.headline_image({"provider": "finnhub", "image": "https://s.yimg.com/rz/stage/p/yahoo_finance"}) is None


def test_yahoo_mysterio_urls_share_the_inner_image_identity():
    wrapper = (
        "https://s.yimg.com/lo/mysterio/api/resizefill_w1200/"
        "https%3A%2F%2Fmedia.zenfs.com%2Fen%2Fpublisher%2Fstory-123.jpg"
    )
    assert media.image_identity(wrapper) == "https://media.zenfs.com/en/publisher/story-123.jpg"


def test_headline_media_cache_retries_transient_failure_and_stops_after_three(tmp_path):
    from datetime import datetime, timezone

    from catalyst_data.ingestion import media
    from catalyst_data.ingestion.collectors import fetch

    db = tmp_path / "media.sqlite3"

    class Flaky:
        calls = 0

        def fetch_page_detailed(self, url):
            self.calls += 1
            raise fetch.HostUnavailable("finance.yahoo.com", "timeout")

    fake = Flaky()
    article = {"url": "https://finnhub.io/api/news?id=42", "has_body": False}
    now = datetime(2026, 10, 9, tzinfo=timezone.utc)
    for _ in range(3):
        media.fill_headlines([article], db, fetcher=fake, now=now, force=True)
    assert fake.calls == 3
    conn = media.open_cache(db)
    row = conn.execute("SELECT status, reason, attempts FROM headline_media").fetchone()
    assert row[0] == "unfetched"
    assert row[1] == "HostUnavailable"
    assert row[2] == 3
    conn.close()


def test_headline_media_cache_keeps_permanent_404_as_final_miss(tmp_path):
    from catalyst_data.ingestion import media
    from catalyst_data.ingestion.collectors import fetch

    class Missing:
        calls = 0

        def fetch_page_detailed(self, _url):
            self.calls += 1
            raise fetch.PermanentPageError("http_404")

    fake = Missing()
    db = tmp_path / "media.sqlite3"
    article = {"url": "https://publisher.example/missing", "has_body": False}
    media.fill_headlines([article], db, fetcher=fake)
    media.fill_headlines([article], db, fetcher=fake, force=True)
    conn = media.open_cache(db)
    try:
        status, reason, attempts = conn.execute(
            "SELECT status,reason,attempts FROM headline_media"
        ).fetchone()
    finally:
        conn.close()
    assert (status, reason, attempts) == ("no_image", "http_404", 1)
    assert fake.calls == 1


def test_shared_image_count_includes_distinct_articles_in_headline_cache(tmp_path):
    from catalyst_data.ingestion import media

    db = tmp_path / "media.sqlite3"
    conn = media.open_cache(db)
    inner = "https://media.zenfs.com/en/publisher/common.jpg"
    wrapper = "https://s.yimg.com/lo/mysterio/api/resizefill_w1200/" + inner.replace("/", "%2F").replace(":", "%3A")
    try:
        conn.executemany(
            "INSERT INTO headline_media (article_key,image_url,image_source,status,attempts) "
            "VALUES (?,?,'og','ok',1)",
            [(f"https://story.example/{i}", wrapper) for i in range(21)],
        )
        conn.commit()
    finally:
        conn.close()
    counts = media.image_use_counts(db, tmp_path / "missing-archive.sqlite3")
    assert counts[inner] == 21
    assert media.is_placeholder(wrapper, counts)


def test_image_use_pairs_without_cache_returns_empty_set(tmp_path):
    missing_cache = tmp_path / "missing-media.sqlite3"
    missing_archive = tmp_path / "missing-archive.sqlite3"

    assert media.image_use_pairs(missing_cache, missing_archive) == set()
    assert not missing_cache.exists()


def test_yahoo_saved_article_html_extracts_real_image():
    from pathlib import Path

    html = (Path(__file__).parent.parent / "fixtures" / "yahoo_article_image_meta.html").read_text()
    result = fetch.page_image_with_source(html, "https://finance.yahoo.com/news/markets-today.html")
    assert result and result[1] == "og"
    assert media.image_identity(result[0]).startswith("https://media.zenfs.com/")


def test_benzinga_extract_body_unchanged_with_image_meta():
    body = "Acme announced a detailed business update and market outlook. "
    paragraphs = "".join(f"<p>{body}Further company details in paragraph {i}.</p>" for i in range(18))
    html = _meta('<meta property="og:image" content="https://cdn.example/acme.jpg">').replace(
        "</body>", f"<article>{paragraphs}</article></body>")
    expected = fetch.extract(f"<article>{paragraphs}</article>", "Acme")
    assert fetch.extract(html, "Acme") == expected


def _body_row(url, asset_id):
    text = "Acme reported a substantial update to its business outlook." * 15
    import hashlib
    return {"asset_id": asset_id, "text": text, "url": url, "title": "Acme update",
            "publisher": "Benzinga", "source_class": "reported_news",
            "eligible_at": "2026-10-01T00:00:00Z", "fetched_at": "2026-10-01T00:00:00Z",
            "body_sha256": hashlib.sha256(text.encode()).hexdigest(), "tickers": ["ACME"]}


def test_write_assets_stores_media(tmp_path):
    conn = store.open_writer(tmp_path / "bodies.sqlite3")
    try:
        row = _body_row("https://www.benzinga.com/news/acme", "web:media-test")
        row.update(image_url="https://cdn.example/acme.jpg", image_source="og")
        store.write_assets(conn, [row])
        assert conn.execute("SELECT canonical_url,image_url,image_source FROM web_asset_media WHERE asset_id=?",
                            (row["asset_id"],)).fetchone() == (
                                row["url"], row["image_url"], "og")
    finally:
        conn.close()


def test_backfill_resumes_and_records_misses(tmp_path):
    conn = store.open_writer(tmp_path / "bodies.sqlite3")
    try:
        store.write_assets(conn, [
            _body_row("https://www.benzinga.com/news/with-image", "web:with-image"),
            _body_row("https://www.benzinga.com/news/no-image", "web:no-image"),
        ])

        class FakeFetcher:
            def __init__(self):
                self.urls = []

            def fetch_page(self, url):
                self.urls.append(url)
                if url.endswith("with-image"):
                    return url, _meta('<meta property="og:image" content="https://cdn.example/story.jpg">')
                return url, "<html><head></head><body>no image</body></html>"

        fake = FakeFetcher()
        assert media.backfill(conn, fake) == {"checked": 2, "stored": 1, "missed": 1, "deferred": 0}
        assert conn.execute("SELECT reason FROM web_asset_media_misses WHERE asset_id='web:no-image'").fetchone() == ("no_image",)
        assert media.backfill(conn, fake) == {"checked": 0, "stored": 0, "missed": 0, "deferred": 0}
        assert len(fake.urls) == 2
    finally:
        conn.close()


def test_backfill_can_be_restricted_to_assets_from_one_job(tmp_path):
    conn = store.open_writer(tmp_path / "bodies.sqlite3")
    try:
        store.write_assets(conn, [
            _body_row("https://www.benzinga.com/news/job", "web:job"),
            _body_row("https://www.benzinga.com/news/old", "web:old"),
        ])

        class FakeFetcher:
            urls = []
            def fetch_page(self, url):
                self.urls.append(url)
                return url, "<html><head></head></html>"

        fake = FakeFetcher()
        result = media.backfill(conn, fake, asset_ids=["web:job"])
        assert result["checked"] == 1
        assert fake.urls == ["https://www.benzinga.com/news/job"]
    finally:
        conn.close()


def test_backfill_defers_unavailable_hosts_and_skips_sec(tmp_path):
    from catalyst_data.ingestion import media
    from catalyst_data.ingestion.collectors import fetch
    from catalyst_data.ingestion.storage import store

    conn = store.open_writer(tmp_path / "b.sqlite3")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(web_body_chunks)")]
    for aid, url in (("web:a", "https://www.benzinga.com/x"), ("web:s", "https://www.sec.gov/Archives/x.htm")):
        row = {c: None for c in cols}
        row.update(asset_id=aid, ordinal=1, chunk_id=aid + ":1", text="t", canonical_url=url, title="t",
                   publisher="p", source_class="reported_news", eligible_at="2026-01-01T00:00:00Z",
                   fetched_at="2026-01-01T00:00:00Z", content_hash="h", body_sha256="h", capture_mode="m")
        conn.execute(f"INSERT INTO web_body_chunks ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                     [row[c] for c in cols])
    conn.commit()

    class Down:
        calls = []

        def fetch_page(self, url):
            self.calls.append(url)
            raise fetch.HostUnavailable("www.benzinga.com", "relay unreachable")

    f = Down()
    result = media.backfill(conn, fetcher=f)
    assert result["deferred"] == 1 and result["missed"] == 0
    assert f.calls == ["https://www.benzinga.com/x"]  # SEC page never fetched
    assert conn.execute("SELECT count(*) FROM web_asset_media_misses").fetchone()[0] == 0


def test_frozen_shared_images_build_the_set_once_while_the_files_keep_changing(tmp_path, monkeypatch):
    """Daily post-processing asks for many tickers while collectors and the image fill write the same files; the
    shared-image set (a ~500k-row scan in production) must be built once for the whole batch, and again outside it."""
    from catalyst_data.ingestion import session_news

    built = []
    monkeypatch.setattr(session_news, "_shared_for_current",
                        lambda path, media_db=None: built.append((str(path), str(media_db))) or frozenset({"x"}))
    with session_news.frozen_shared_images():
        for _ in range(3):
            assert session_news._shared_for(tmp_path / "a.sqlite3", tmp_path / "m.sqlite3") == {"x"}
        session_news._shared_for(tmp_path / "other.sqlite3", tmp_path / "m.sqlite3")
    assert len(built) == 2
    session_news._shared_for(tmp_path / "a.sqlite3", tmp_path / "m.sqlite3")
    session_news._shared_for(tmp_path / "a.sqlite3", tmp_path / "m.sqlite3")
    assert len(built) == 4 and session_news._FROZEN_SHARED is None
