from catalyst_data.ingestion.collectors.kitco import candidates, parse_sitemap


def test_parse_and_filter_sitemap_entries():
    xml = """<urlset>
      <url><loc>https://kitco.example/gold</loc><news:news><news:publication_date>2026-01-10T12:00:00+00:00</news:publication_date><news:title><![CDATA[Gold rises on demand]]></news:title></news:news></url>
      <url><loc>https://kitco.example/late</loc><news:news><news:publication_date>2026-01-11T12:00:00Z</news:publication_date><news:title><![CDATA[Gold jumps late]]></news:title></news:news></url>
      <url><loc>https://kitco.example/oil</loc><news:news><news:publication_date>2026-01-10T13:00:00Z</news:publication_date><news:title><![CDATA[Oil prices fall]]></news:title></news:news></url>
    </urlset>"""
    entries = parse_sitemap(xml)
    assert len(entries) == 3
    assert entries[0][1] == "2026-01-10T12:00:00Z"
    assert candidates(entries, "GLD", "2026-01-09T00:00:00Z", "2026-01-10T23:59:59Z") == [entries[0]]


def test_existing_body_retag_records_actual_observation_not_publication(monkeypatch):
    from datetime import datetime, timezone

    from catalyst_data.ingestion.collectors import kitco

    class Clock:
        @staticmethod
        def now(zone):
            assert zone == timezone.utc
            return datetime(2026, 10, 26, 18, tzinfo=zone)

    known = dict(asset_id='asset', url='https://www.kitco.com/story', title='Gold rises',
                 text='Gold rose after new data.', eligible_at='2026-10-20T15:00:00Z')
    monkeypatch.setattr(kitco, 'datetime', Clock)
    monkeypatch.setattr(kitco.store, 'stored_body', lambda *args: known)
    monkeypatch.setattr(kitco, 'names_subject', lambda *args: True)
    rows = kitco.fetch_bodies(None, [(known['url'], known['eligible_at'], known['title'])],
                             'GLD', conn=object())
    assert rows[0]['fetched_at'] == '2026-10-26T18:00:00Z'
    assert rows[0]['eligible_at'] == known['eligible_at']
    assert rows[0]['_retag_only'] is True
