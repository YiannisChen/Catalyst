from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from catalyst_data.ingestion.input_manifest import PreflightError
from catalyst_data.storage.read_adapters import read_store_facts
from catalyst_data.storage.snapshots import load_verified_stores
from catalyst_data.storage.time_corrections import load_corrections

START = "2026-10-20T00:00:00Z"
END = "2026-10-21T00:00:00Z"
AT = "2026-10-20T12:00:00Z"
INVOCATION = ["python", "-m", "catalyst_data.query_service"]


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _registry_path(tmp_path):
    path = tmp_path / "sources_v1.json"
    if not path.exists():
        path.write_text(json.dumps({"schema_version": 1, "sources": [
            {"id": "news_archive:finnhub_company"},
            {"id": "press_rss:ir"},
            {"id": "press_rss:prnewswire"},
        ]}))
    return path


def _verified(tmp_path, path, *, extra_files=(), registry_path=None, pin_registry=True,
              store_id="archive"):
    manifest = tmp_path / "inputs.json"
    registry_path = registry_path or _registry_path(tmp_path)
    files = (path, *extra_files, *((registry_path,) if pin_registry else ()))
    manifest.write_text(json.dumps({
        "schema_version": 1,
        "invocation": INVOCATION,
        "files": [{"path": str(item), "sha256": _sha(item)} for item in files],
        "environment": {},
    }))
    return load_verified_stores(
        manifest, invocation=INVOCATION, required_stores={store_id: path}, environ={},
    )


def _create_headline_archive(path):
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE finnhub_news (
                symbol TEXT NOT NULL, news_id INTEGER NOT NULL, published_utc TEXT NOT NULL,
                headline TEXT NOT NULL, url TEXT NOT NULL, source TEXT, category TEXT,
                related TEXT, summary TEXT, raw_json TEXT NOT NULL, first_seen_at TEXT NOT NULL,
                PRIMARY KEY (symbol, news_id)
            );
            CREATE TABLE archive_windows (
                symbol TEXT NOT NULL, window_start TEXT NOT NULL, window_end TEXT NOT NULL,
                status TEXT NOT NULL, items INTEGER NOT NULL, attempts INTEGER NOT NULL,
                detail TEXT, fetched_at TEXT NOT NULL,
                PRIMARY KEY (symbol, window_start, window_end)
            );
        """)
        conn.executemany("INSERT INTO finnhub_news VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
            ("ABC", 1, "2026-10-20T09:00:00Z", "Known headline", "https://a.test/1",
             "Wire", "company", "ABC", "summary only", "{}", "2026-10-20T10:00:00Z"),
            ("ABC", 2, "2026-10-20T09:30:00Z", "Not yet observed", "https://a.test/2",
             "Wire", "company", "ABC", "summary only", "{}", "2026-10-20T13:00:00Z"),
            ("ABC", 3, "2026-10-20T13:00:00Z", "Future publication", "https://a.test/3",
             "Wire", "company", "ABC", None, "{}", "2026-10-20T11:00:00Z"),
        ])


def _create_news_archive(path):
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE news_items (
                provider TEXT NOT NULL, item_key TEXT NOT NULL, url TEXT NOT NULL,
                title TEXT NOT NULL, summary TEXT, publisher TEXT, published_at TEXT,
                tickers_json TEXT NOT NULL, extra_json TEXT, first_seen_at TEXT NOT NULL,
                PRIMARY KEY (provider, item_key)
            );
            CREATE TABLE news_item_tickers (
                provider TEXT NOT NULL, item_key TEXT NOT NULL, ticker TEXT NOT NULL,
                PRIMARY KEY (provider, item_key, ticker)
            );
        """)
        conn.execute(
            "INSERT INTO news_items VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("finnhub_company", "item-1", "https://news.test/1", "ABC result", "summary",
             "Wire", "2026-10-20T09:00:00Z", '["ABC"]', None, "2026-10-20T10:00:00Z"),
        )
        # The current relation table is not the first-seen ticker-membership proof.
        conn.execute("INSERT INTO news_item_tickers VALUES (?,?,?)", ("finnhub_company", "item-1", "XYZ"))
        conn.execute(
            "INSERT INTO news_items VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("finnhub_company", "item-future", "https://news.test/future", "Future publication", None,
             "Wire", "2026-10-20T13:00:00Z", '["ABC"]', None, "2026-10-20T11:00:00Z"),
        )


def _create_press_rss(path):
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE press_rss (
                provider TEXT NOT NULL, guid TEXT NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL,
                summary TEXT, published_at TEXT, tickers_json TEXT NOT NULL, feed TEXT NOT NULL,
                first_seen_at TEXT NOT NULL, PRIMARY KEY (provider, guid)
            );
            CREATE TABLE press_rss_tickers (
                provider TEXT NOT NULL, guid TEXT NOT NULL, ticker TEXT NOT NULL,
                PRIMARY KEY (provider, guid, ticker)
            );
            CREATE TABLE press_rss_observations (
                provider TEXT NOT NULL, guid TEXT NOT NULL, content_sha256 TEXT NOT NULL,
                observed_at TEXT NOT NULL, PRIMARY KEY (provider, guid, content_sha256, observed_at)
            );
        """)
        conn.execute(
            "INSERT INTO press_rss VALUES (?,?,?,?,?,?,?,?,?)",
            ("ir", "guid-1", "https://ir.test/release", "ABC release", "summary",
             "2026-10-20T09:00:00Z", '["ABC"]', "https://ir.test/feed.xml",
             "2026-10-20T10:00:00Z"),
        )
        conn.execute("INSERT INTO press_rss_tickers VALUES (?,?,?)", ("ir", "guid-1", "XYZ"))
        conn.execute("INSERT INTO press_rss_observations VALUES (?,?,?,?)", (
            "ir", "guid-1", _press_content_hash(
                "https://ir.test/release", "ABC release", "summary", "2026-10-20T09:00:00Z", ["ABC"]
            ), "2026-10-20T10:00:00.123456+00:00",
        ))
        conn.execute(
            "INSERT INTO press_rss VALUES (?,?,?,?,?,?,?,?,?)",
            ("ir", "guid-2", "https://ir.test/no-pub", "Undated release", None,
             None, '["ABC"]', "https://ir.test/feed.xml", "2026-10-20T10:00:00Z"),
        )
        conn.execute(
            "INSERT INTO press_rss VALUES (?,?,?,?,?,?,?,?,?)",
            ("ir", "guid-3", "https://ir.test/future", "Future publication", None,
             "2026-10-20T13:00:00Z", '["ABC"]', "https://ir.test/feed.xml",
             "2026-10-20T11:00:00Z"),
        )


def _read(tmp_path, path, adapter, *, mode="observed", at=AT, **options):
    registry_path = _registry_path(tmp_path)
    return read_store_facts(
        _verified(tmp_path, path, registry_path=registry_path),
        "archive", adapter, "ABC", START, END, at,
        mode=mode, registry_path=registry_path, **options,
    )


def _press_content_hash(url, title, summary, published_at, tickers):
    content = {"url": url, "title": title, "summary": summary,
               "published_at": published_at, "tickers": tickers}
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def test_headline_archive_observed_requires_first_seen_and_returns_metadata_as_leads(tmp_path):
    path = tmp_path / "headline.sqlite3"
    _create_headline_archive(path)

    result = _read(tmp_path, path, "headline_archive")

    assert len(result["metadata_leads"]) == 1
    lead = result["metadata_leads"][0]
    assert lead["product"] == "headlines"
    assert lead["subject_kind"] == "ticker" and lead["subject_id"] == "ABC"
    assert lead["store_id"] == "archive"
    assert lead["snapshot_id"] == _sha(path)
    assert lead["title"] == "Known headline"
    assert result["market_facts"] == []
    assert result["audit"]["data_snapshot_ids"] == {"archive": _sha(path)}
    assert result["audit"]["mode"] == "observed"
    assert result["audit"]["excluded_missing_published_count"] == 0


def test_headline_archive_subject_uses_canonical_query_ticker_for_lowercase_stored_symbol(tmp_path):
    path = tmp_path / "headline-lowercase.sqlite3"
    _create_headline_archive(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE finnhub_news SET symbol='abc' WHERE news_id=1")

    result = _read(tmp_path, path, "headline_archive")

    assert len(result["metadata_leads"]) == 1
    assert result["metadata_leads"][0]["subject_id"] == "ABC"
    assert result["metadata_leads"][0]["item_id"] == "abc:1"


def test_headline_archive_rejects_malformed_first_seen_timestamp(tmp_path):
    path = tmp_path / "headline.sqlite3"
    _create_headline_archive(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE finnhub_news SET first_seen_at='2026-10-20T10:00:00' WHERE news_id=1")

    with pytest.raises(ValueError, match="timestamp"):
        _read(tmp_path, path, "headline_archive", at="2026-10-21T00:00:00Z")


@pytest.mark.parametrize("adapter", ["headline_archive", "news_archive"])
def test_observed_adapters_reject_submicrosecond_first_seen_precision(tmp_path, adapter):
    path = tmp_path / f"{adapter}-submicrosecond.sqlite3"
    if adapter == "headline_archive":
        _create_headline_archive(path)
        with sqlite3.connect(path) as conn:
            conn.execute(
                "UPDATE finnhub_news SET first_seen_at=? WHERE news_id=1",
                ("2026-10-20T10:00:00.1234567Z",),
            )
    else:
        _create_news_archive(path)
        with sqlite3.connect(path) as conn:
            conn.execute(
                "UPDATE news_items SET first_seen_at=? WHERE item_key='item-1'",
                ("2026-10-20T10:00:00.1234567Z",),
            )

    with pytest.raises(ValueError, match="precision|timestamp"):
        _read(tmp_path, path, adapter, at="2026-10-20T10:00:00.123456Z")


def test_source_registry_must_be_an_explicit_manifest_pinned_input(tmp_path):
    path = tmp_path / "news.sqlite3"
    _create_news_archive(path)
    registry_path = _registry_path(tmp_path)
    verified_without_registry = _verified(
        tmp_path, path, registry_path=registry_path, pin_registry=False,
    )

    with pytest.raises((ValueError, PreflightError), match="registry|pinned|manifest"):
        read_store_facts(
            verified_without_registry, "archive", "news_archive", "ABC", START, END, AT,
            mode="observed", registry_path=registry_path,
        )


def test_source_registry_manifest_mismatch_is_rejected(tmp_path):
    path = tmp_path / "news.sqlite3"
    _create_news_archive(path)
    registry_path = _registry_path(tmp_path)
    verified = _verified(tmp_path, path, registry_path=registry_path)
    registry_path.write_text(json.dumps({"schema_version": 1, "sources": []}))

    with pytest.raises((ValueError, PreflightError), match="registry|pinned|manifest|changed|checksum"):
        read_store_facts(
            verified, "archive", "news_archive", "ABC", START, END, AT,
            mode="observed", registry_path=registry_path,
        )


def test_pinned_source_registry_sha_is_recorded_in_audit(tmp_path):
    path = tmp_path / "news.sqlite3"
    _create_news_archive(path)
    registry_path = _registry_path(tmp_path)
    verified = _verified(tmp_path, path, registry_path=registry_path)

    result = read_store_facts(
        verified, "archive", "news_archive", "ABC", START, END, AT,
        mode="observed", registry_path=registry_path,
    )

    assert result["audit"]["source_registry_sha256"] == _sha(registry_path)


def test_current_registry_rejects_retired_news_archive_source(tmp_path):
    path = tmp_path / "news.sqlite3"
    _create_news_archive(path)
    source_registry = Path(__file__).resolve().parents[3] / "data/manifests/sources_v1.json"
    registry_path = tmp_path / "sources_v1.json"
    registry_path.write_bytes(source_registry.read_bytes())
    verified = _verified(tmp_path, path, registry_path=registry_path)

    with pytest.raises(ValueError, match="unknown registry source"):
        read_store_facts(
            verified, "archive", "news_archive", "ABC", START, END, AT,
            mode="observed", registry_path=registry_path,
        )


def test_headline_archive_keeps_registered_upstream_source_and_actual_snapshot_store(tmp_path):
    path = tmp_path / "headline_archive.sqlite3"
    _create_headline_archive(path)
    source_registry = Path(__file__).resolve().parents[3] / "data/manifests/sources_v1.json"
    registry_path = tmp_path / "sources_v1.json"
    registry_path.write_bytes(source_registry.read_bytes())
    verified = _verified(
        tmp_path, path, registry_path=registry_path, store_id="headline_archive",
    )

    result = read_store_facts(
        verified, "headline_archive", "headline_archive", "ABC", START, END, AT,
        mode="observed", registry_path=registry_path,
    )

    lead = result["metadata_leads"][0]
    assert lead["source_id"] == "daily:finnhub"
    assert lead["store_id"] == "headline_archive"
    assert result["audit"]["data_snapshot_ids"] == {"headline_archive": _sha(path)}


def test_news_archive_uses_first_seen_ticker_json_not_current_ticker_map(tmp_path):
    path = tmp_path / "news.sqlite3"
    _create_news_archive(path)

    result = _read(tmp_path, path, "news_archive")

    assert [item["title"] for item in result["metadata_leads"]] == ["ABC result"]
    assert result["metadata_leads"][0]["product"] == "headlines"
    assert result["market_facts"] == []
    assert result["audit"]["excluded_missing_published_count"] == 0


def test_news_archive_malformed_ticker_json_fails_closed(tmp_path):
    path = tmp_path / "news.sqlite3"
    _create_news_archive(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE news_items SET tickers_json='{bad' WHERE item_key='item-1'")

    with pytest.raises(ValueError, match="ticker|JSON|membership"):
        _read(tmp_path, path, "news_archive")


def test_press_rss_uses_first_seen_ticker_json_and_excludes_missing_publication_time(tmp_path):
    path = tmp_path / "press.sqlite3"
    _create_press_rss(path)

    result = _read(tmp_path, path, "press_rss")

    assert [item["title"] for item in result["metadata_leads"]] == ["ABC release"]
    assert result["metadata_leads"][0]["feed"] == "https://ir.test/feed.xml"
    assert result["market_facts"] == []
    assert result["audit"]["excluded_missing_published_count"] == 1


def test_press_rss_observed_requires_matching_content_observation_and_cutoff(tmp_path):
    path = tmp_path / "press.sqlite3"
    _create_press_rss(path)
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM press_rss_observations")
        conn.executemany("INSERT INTO press_rss_observations VALUES (?,?,?,?)", [
            ("ir", "guid-1", "0" * 64, "2026-10-20T09:59:59.999999+00:00"),
            ("ir", "guid-1", _press_content_hash(
                "https://ir.test/release", "ABC release", "summary", "2026-10-20T09:00:00Z", ["ABC"]
            ), "2026-10-20T10:00:00.123456+00:00"),
        ])

    before_observation = _read(tmp_path, path, "press_rss", at="2026-10-20T10:00:00.123455Z")
    after_observation = _read(tmp_path, path, "press_rss", at="2026-10-20T10:00:00.123456Z")

    assert before_observation["metadata_leads"] == []
    assert [item["title"] for item in after_observation["metadata_leads"]] == ["ABC release"]
    assert after_observation["metadata_leads"][0]["observation_available_at"] == "2026-10-20T10:00:00.123456+00:00"


def test_legacy_press_rss_without_observation_table_only_remains_published_lead(tmp_path):
    path = tmp_path / "legacy-press.sqlite3"
    _create_press_rss(path)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE press_rss_observations")

    observed = _read(tmp_path, path, "press_rss")
    published = _read(tmp_path, path, "press_rss", mode="published")

    assert observed["metadata_leads"] == []
    assert [item["title"] for item in published["metadata_leads"]] == ["ABC release"]
    assert published["metadata_leads"][0]["temporal_status"] == "unverified_published"


@pytest.mark.parametrize("adapter", ["headline_archive", "news_archive"])
def test_second_precision_observation_requires_next_second_cutoff(tmp_path, adapter):
    path = tmp_path / f"{adapter}-second.sqlite3"
    {"headline_archive": _create_headline_archive,
     "news_archive": _create_news_archive}[adapter](path)

    at_recorded_second = _read(tmp_path, path, adapter, at="2026-10-20T10:00:00Z")
    at_conservative_boundary = _read(tmp_path, path, adapter, at="2026-10-20T10:00:01Z")

    assert at_recorded_second["metadata_leads"] == []
    assert at_conservative_boundary["metadata_leads"][0]["first_seen_at"] == "2026-10-20T10:00:00Z"
    assert at_conservative_boundary["metadata_leads"][0]["observation_available_at"] == "2026-10-20T10:00:01.000000+00:00"


@pytest.mark.parametrize("adapter", ["headline_archive", "news_archive"])
def test_microsecond_observation_uses_exact_cutoff_and_preserves_raw_time(tmp_path, adapter):
    path = tmp_path / f"{adapter}-microsecond.sqlite3"
    {"headline_archive": _create_headline_archive,
     "news_archive": _create_news_archive}[adapter](path)
    with sqlite3.connect(path) as conn:
        table = "finnhub_news" if adapter == "headline_archive" else "news_items"
        column = "first_seen_at"
        key = "news_id=1" if adapter == "headline_archive" else "item_key='item-1'"
        conn.execute(f"UPDATE {table} SET {column}='2026-10-20T10:00:00.123456Z' WHERE {key}")

    exact_before = _read(tmp_path, path, adapter, at="2026-10-20T10:00:00.123455Z")
    exact_at = _read(tmp_path, path, adapter, at="2026-10-20T10:00:00.123456Z")

    assert exact_before["metadata_leads"] == []
    assert exact_at["metadata_leads"][0]["first_seen_at"] == "2026-10-20T10:00:00.123456Z"
    assert exact_at["metadata_leads"][0]["observation_available_at"] == "2026-10-20T10:00:00.123456+00:00"


@pytest.mark.parametrize("adapter", ["headline_archive", "news_archive", "press_rss"])
def test_published_mode_excludes_in_window_metadata_after_cutoff(tmp_path, adapter):
    path = tmp_path / f"{adapter}.sqlite3"
    {"headline_archive": _create_headline_archive,
     "news_archive": _create_news_archive,
     "press_rss": _create_press_rss}[adapter](path)

    result = _read(tmp_path, path, adapter, mode="published")

    assert all(item["title"] != "Future publication" for item in result["metadata_leads"])
    assert all(item["temporal_status"] == "unverified_published"
               for item in result["metadata_leads"])


def _finnhub_correction(tmp_path, *, stored, corrected):
    csv_path = tmp_path / "finnhub-corrections.csv"
    csv_path.write_text(
        "url,publisher,stored_eligible_at,corrected_at,stores\n"
        f"https://news.test/1,Wire,{stored},{corrected},archive-correction\n"
    )
    table = load_corrections(
        csv_path.read_bytes(), kind="finnhub", filename=csv_path.name, expected_sha256=_sha(csv_path),
    )
    return csv_path, table


def test_news_archive_applies_registered_finnhub_correction_before_window_filter(tmp_path):
    path = tmp_path / "news-corrected-window.sqlite3"
    _create_news_archive(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE news_items SET published_at='2026-10-20T09:00:00Z' WHERE item_key='item-1'")
    original = "2026-10-20T09:00:00Z"
    corrected = "2026-10-20T23:00:00Z"
    csv_path, table = _finnhub_correction(tmp_path, stored=original, corrected=corrected)
    registry_path = _registry_path(tmp_path)
    verified = _verified(tmp_path, path, extra_files=(csv_path,), registry_path=registry_path)

    result = read_store_facts(
        verified, "archive", "news_archive", "ABC", "2026-10-20T22:00:00Z",
        "2026-10-21T00:00:00Z", "2026-10-20T23:30:00Z", mode="published",
        correction_table_id=table.table_id, correction_tables=(table,),
        correction_store_ids={"archive": "archive-correction"},
        correction_artifacts={table.table_id: csv_path}, registry_path=registry_path,
    )

    assert [lead["title"] for lead in result["metadata_leads"]] == ["ABC result"]
    assert result["metadata_leads"][0]["published_at"] == "2026-10-20T23:00:00.000000+00:00"


def test_news_archive_applies_registered_finnhub_correction_before_cutoff_filter(tmp_path):
    path = tmp_path / "news-corrected-cutoff.sqlite3"
    _create_news_archive(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE news_items SET published_at='2026-10-20T23:00:00Z' WHERE item_key='item-1'")
    csv_path, table = _finnhub_correction(
        tmp_path, stored="2026-10-20T23:00:00Z", corrected="2026-10-21T01:00:00Z",
    )
    registry_path = _registry_path(tmp_path)
    verified = _verified(tmp_path, path, extra_files=(csv_path,), registry_path=registry_path)

    result = read_store_facts(
        verified, "archive", "news_archive", "ABC", "2026-10-20T00:00:00Z",
        "2026-10-21T00:00:00Z", "2026-10-21T00:00:00Z", mode="published",
        correction_table_id=table.table_id, correction_tables=(table,),
        correction_store_ids={"archive": "archive-correction"},
        correction_artifacts={table.table_id: csv_path}, registry_path=registry_path,
    )

    assert all(lead["item_id"] != "item-1" for lead in result["metadata_leads"])


def test_unknown_or_malformed_adapter_schema_fails_closed(tmp_path):
    path = tmp_path / "bad.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE news_items (provider TEXT)")
    registry_path = _registry_path(tmp_path)
    verified = _verified(tmp_path, path, registry_path=registry_path)

    with pytest.raises(ValueError, match="schema|adapter"):
        read_store_facts(verified, "archive", "news_archive", "ABC", START, END, AT,
                         mode="observed", registry_path=registry_path)


def test_invalid_mode_is_rejected(tmp_path):
    path = tmp_path / "news.sqlite3"
    _create_news_archive(path)
    registry_path = _registry_path(tmp_path)
    verified = _verified(tmp_path, path, registry_path=registry_path)

    with pytest.raises(ValueError, match="mode"):
        read_store_facts(verified, "archive", "news_archive", "ABC", START, END, AT,
                         mode="current", registry_path=registry_path)
