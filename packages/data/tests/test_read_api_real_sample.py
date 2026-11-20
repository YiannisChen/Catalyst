"""The read API against a real slice of the production body store: four SEC EDGAR 8-K documents (public record)
observed 10-29..10-30, their temporal evidence rows, and the gap-ledger rows of those four tickers."""
import hashlib
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from catalyst_data import read_api
from catalyst_data.storage.snapshots import load_verified_stores

REAL = Path(__file__).parent / "fixtures" / "real_sec_8k_slice.sqlite3"
INVOCATION = ["read-api-real-sample"]
START, END = datetime(2026, 10, 27, tzinfo=timezone.utc), datetime(2026, 10, 31, tzinfo=timezone.utc)


@pytest.fixture
def verified(tmp_path):
    store = tmp_path / "bodies.sqlite3"
    shutil.copy(REAL, store)
    manifest = tmp_path / "inputs.json"
    manifest.write_text(json.dumps({
        "schema_version": 1, "invocation": INVOCATION, "environment": {},
        "files": [{"path": str(store), "sha256": hashlib.sha256(store.read_bytes()).hexdigest()}],
    }))
    return load_verified_stores(manifest, invocation=INVOCATION, required_stores={"bodies": store}, environ={})


def test_the_two_modes_answer_differently_on_real_filings(verified):
    observed = read_api.as_of(verified, "AVGO", START, END, END, mode="observed")
    assert [e["ticker"] for e in observed["evidence"]] == ["AVGO"] and observed["leads"] == []
    item = observed["evidence"][0]
    assert item["url"].startswith("https://www.sec.gov/Archives/edgar/data/1730168/")
    assert (item["published_at"], item["observed_at"]) == ("2026-10-27T20:23:36.000000+00:00", "2026-10-29T11:21:43.000000+00:00")
    assert item["temporal_status"] == "observed" and set(item) == set(read_api.EVIDENCE_FIELDS)
    # the same filing, the same cutoff, read as 'published': not evidence there, only a lead
    published = read_api.as_of(verified, "AVGO", START, END, END, mode="published")
    assert published["evidence"] == [] and [e["version_id"] for e in published["leads"]] == [item["version_id"]]


def test_a_cutoff_before_we_first_saw_the_filing_returns_nothing_in_observed_mode(verified):
    before = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)  # the filing existed (10-27) but we saw it on 10-29
    result = read_api.as_of(verified, "AVGO", START, END, before, mode="observed")
    assert result["evidence"] == [] and result["leads"] == []


def test_mode_is_required_and_times_need_a_timezone(verified):
    with pytest.raises(ValueError, match="no default"):
        read_api.as_of(verified, "AVGO", START, END, END, mode="latest")
    with pytest.raises(ValueError, match="timezone-aware"):
        read_api.as_of(verified, "AVGO", START, END, datetime(2026, 10, 31), mode="observed")


def test_gap_table_lists_category_rows_without_provider_ids_and_never_writes(tmp_path):
    store = tmp_path / "bodies.sqlite3"
    shutil.copy(REAL, store)
    before = store.read_bytes()
    rows = read_api.gap_table(store, ticker="AVGO")
    assert rows and {r["category"] for r in rows} == {"sec_filings"}
    assert {r["status"] for r in rows} <= {"OK", "EMPTY", "ERROR", "NOT_FETCHED"}
    assert all("source_id" not in r for r in rows)
    assert [r["session_date"] for r in rows] == sorted(r["session_date"] for r in rows)
    assert store.read_bytes() == before
    with pytest.raises(FileNotFoundError):
        read_api.gap_table(tmp_path / "missing.sqlite3")
    empty = tmp_path / "empty.sqlite3"
    sqlite3.connect(empty).close()
    with pytest.raises(LookupError):
        read_api.gap_table(empty)
