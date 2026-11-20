import sqlite3

from catalyst_data.ingestion.storage import page_versions as PV

LD = ('<script type="application/ld+json">{"@type":"NewsArticle","headline":"h",'
      '"datePublished":"2026-10-15T13:00:00Z","dateModified":"%s"}</script>')


def _rows(db):
    return sqlite3.connect(db).execute(
        "SELECT path, declared_published, declared_modified, modified_source, modified_after_cutoff FROM page_versions"
    ).fetchall()


def test_records_declared_times_and_whether_the_modification_is_after_the_cutoff(tmp_path, monkeypatch):
    db = tmp_path / "pv.sqlite3"
    monkeypatch.setenv("CATALYST_PAGE_VERSIONS_DB", str(db))
    PV.record("company_body", "https://a.test/1", page_html=LD % "2026-10-15T21:30:00Z", cutoff="2026-10-15T20:00:00Z")
    PV.record("company_body", "https://a.test/2", page_html=LD % "2026-10-15T14:00:00Z", cutoff="2026-10-15T20:00:00Z")
    PV.record("web_discovery", "https://b.test/3",
              page_html='<meta property="article:modified_time" content="2026-10-16T09:00:00+00:00">',
              cutoff="2026-10-15T20:00:00Z")
    PV.record("kitco", "https://k.test/4", published="2026-10-15T12:00:00Z", modified="2026-10-15T12:30:00Z",
              source="next_data", cutoff="2026-10-15T20:00:00Z")
    PV.record("benzinga", "https://z.test/5", page_html="<html>no dates</html>", cutoff="2026-10-15T20:00:00Z")
    assert _rows(db) == [
        ("company_body", "2026-10-15T13:00:00Z", "2026-10-15T21:30:00Z", "jsonld", 1),
        ("company_body", "2026-10-15T13:00:00Z", "2026-10-15T14:00:00Z", "jsonld", 0),
        ("web_discovery", None, "2026-10-16T09:00:00Z", "meta", 1),
        ("kitco", "2026-10-15T12:00:00Z", "2026-10-15T12:30:00Z", "next_data", 0),
        ("benzinga", None, None, "none", None),
    ]


def test_recording_is_off_without_a_store_and_never_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("CATALYST_PAGE_VERSIONS_DB", raising=False)
    monkeypatch.setattr(PV, "_DIR", None)
    PV.record("company_body", "https://a.test/1", page_html=LD % "x")  # no store: nothing happens
    monkeypatch.setenv("CATALYST_PAGE_VERSIONS_DB", str(tmp_path / "missing-dir" / "pv.sqlite3"))
    PV.record("company_body", "https://a.test/1", page_html=None)  # unwritable path: swallowed
