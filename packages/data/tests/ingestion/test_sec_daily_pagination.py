"""Offline SEC pagination regressions for daily intake.

All payloads and records are synthetic. Candidate enumeration must read every
listed history page, while the filing index page remains the daily path's
authority for Accepted time.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone

import pytest
from catalyst_data.ingestion.collectors import headlines, sec, sec_filings
from catalyst_data.ingestion.ops import daily

CIK = "0000001234"
SESSION = "2026-03-03"
START, CUTOFF = headlines.session_window(SESSION)


def _rows(*entries):
    return {
        "accessionNumber": [entry[0] for entry in entries],
        "acceptanceDateTime": [entry[1] for entry in entries],
        "form": [entry[2] for entry in entries],
        "items": [entry[3] for entry in entries],
        "primaryDocDescription": [entry[4] for entry in entries],
    }


def _filing(accession, accepted, *, form="8-K", items="2.02", description="Results"):
    return (accession, accepted, form, items, description)


class _Response:
    def __init__(self, payload=None, *, text=""):
        self._payload = payload
        self.text = text

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _candidate_loader():
    loader = getattr(sec_filings, "candidates_for_window", None)
    assert callable(loader), "daily SEC pagination candidate API is not implemented"
    return loader


def test_offline_cached_submissions_cannot_prove_current_enumeration(monkeypatch, tmp_path):
    monkeypatch.setattr(sec_filings, "_offline", lambda: True)
    monkeypatch.setattr(sec_filings, "_dir", lambda: tmp_path)
    (tmp_path / f"{CIK}.json").write_text(json.dumps(_submissions(_rows(), [])))
    with pytest.raises((RuntimeError, ValueError), match="offline|fresh|complete"):
        _candidate_loader()(CIK, START, CUTOFF, http_get=lambda *_a, **_k: None,
                            user_agent="fixture-agent")


def _submissions(recent, archive_names):
    return {"name": "Synthetic Issuer", "filings": {
        "recent": recent,
        "files": [{"name": name} for name in archive_names],
    }}


def test_window_candidates_enumerate_recent_and_every_archive_page(monkeypatch, tmp_path):
    names = [
        f"CIK{CIK}-submissions-001.json",
        f"CIK{CIK}-submissions-002.json",
    ]
    payloads = {
        f"{sec.SUBMISSIONS_BASE}/submissions/CIK{CIK}.json": _submissions(
            _rows(
                # There is no verified maximum delay for this provisional time.
                # The official index Accepted time may still place it in-window.
                _filing("recent-late", "2026-03-04T23:30:00.000Z"),
                _filing("recent-outside", "2026-03-04T02:01:00.000Z"),
            ),
            names,
        ),
        f"{sec.SUBMISSIONS_BASE}/submissions/{names[0]}": {
            "filings": _rows(_filing("archive-window", "2026-03-03T18:00:00.000Z"))
        },
        # This page has an apparently irrelevant date but is still a candidate:
        # no provider-guaranteed skew bound permits filtering before index lookup.
        f"{sec.SUBMISSIONS_BASE}/submissions/{names[1]}": {
            "filings": _rows(_filing("archive-outside", "2025-01-03T18:00:00.000Z"))
        },
    }
    requests = []

    def http_get(url, **kwargs):
        requests.append((url, kwargs))
        return _Response(payloads[url])

    monkeypatch.setattr(sec_filings, "_dir", lambda: tmp_path)

    candidates = _candidate_loader()(
        CIK, START, CUTOFF, http_get=http_get, user_agent="fixture-agent"
    )

    assert {row["accession"] for row in candidates} == {
        "recent-late", "recent-outside", "archive-window", "archive-outside"
    }
    by_accession = {row["accession"]: row for row in candidates}
    late = by_accession["recent-late"]
    assert late["accepted_at"] == late["json_accepted_at"] == "2026-03-04T23:30:00Z"
    assert late["items"] == ["2.02"]
    assert late["company"] == "Synthetic Issuer"
    assert late["description"] == "Results"
    assert len(requests) == len(payloads)
    assert {url for url, _kwargs in requests} == set(payloads)
    assert all(kwargs["headers"]["User-Agent"] == "fixture-agent" for _url, kwargs in requests)
    assert not any(url.endswith("-index.htm") for url, _kwargs in requests)


@pytest.mark.parametrize("failure", ["request", "missing_name", "malformed_arrays"])
def test_unreadable_or_malformed_archive_fails_candidate_enumeration(
    monkeypatch, tmp_path, failure
):
    archives = [
        f"CIK{CIK}-submissions-001.json",
        f"CIK{CIK}-submissions-002.json",
    ]
    files = [{"name": name} for name in archives]
    if failure == "missing_name":
        files[1] = {}
    root = {"name": "Synthetic Issuer", "filings": {
        "recent": _rows(_filing("recent", "2026-03-03T18:00:00.000Z")),
        "files": files,
    }}
    good_archive = _rows(_filing("archive-first", "2026-03-03T18:00:00.000Z"))
    bad_archive = _rows(_filing("archive-second", "2026-03-03T18:00:00.000Z"))
    if failure == "malformed_arrays":
        bad_archive["items"].pop()
    first_url, second_url = [
        f"{sec.SUBMISSIONS_BASE}/submissions/{name}" for name in archives
    ]
    payloads = {
        f"{sec.SUBMISSIONS_BASE}/submissions/CIK{CIK}.json": root,
        first_url: {"filings": good_archive},
        second_url: {"filings": bad_archive},
    }

    def http_get(url, **_kwargs):
        if failure == "request" and url == second_url:
            raise OSError("synthetic archive request failure")
        return _Response(payloads[url])

    monkeypatch.setattr(sec_filings, "_dir", lambda: tmp_path)

    with pytest.raises((ValueError, KeyError, OSError)):
        _candidate_loader()(
            CIK, START, CUTOFF, http_get=http_get, user_agent="fixture-agent"
        )


def test_failed_submissions_refresh_cannot_return_stale_cache_as_success(
    monkeypatch, tmp_path
):
    stale_cache = tmp_path / f"{CIK}.json"
    stale_cache.write_text(json.dumps(_submissions(
        _rows(_filing("stale-only", "2026-03-03T18:00:00.000Z")), []
    )))
    old_time = datetime.now(timezone.utc).timestamp() - 24 * 60 * 60
    os.utime(stale_cache, (old_time, old_time))
    calls = []

    def http_get(url, **_kwargs):
        calls.append(url)
        raise OSError("synthetic submissions refresh failure")

    monkeypatch.setattr(sec_filings, "_dir", lambda: tmp_path)

    with pytest.raises(OSError, match="refresh failure"):
        _candidate_loader()(
            CIK, START, CUTOFF, http_get=http_get, user_agent="fixture-agent"
        )
    assert calls == [f"{sec.SUBMISSIONS_BASE}/submissions/CIK{CIK}.json"]


def test_failed_daily_page_enumeration_is_retryable_and_does_not_become_empty(
    monkeypatch, tmp_path
):
    paths = daily.Paths(
        market_db=tmp_path / "market.sqlite3",
        bodies_db=tmp_path / "bodies.sqlite3",
        peer_map={},
        snapshot_db=tmp_path / "snapshot.sqlite3",
        jobs_db=tmp_path / "jobs.sqlite3",
        archive_db=tmp_path / "archive.sqlite3",
    )
    monkeypatch.setenv("SEC_USER_AGENT", "fixture-agent")
    monkeypatch.setattr(sec, "cik_for", lambda *_args, **_kwargs: CIK)
    calls = []

    def candidates(cik, start, cutoff, *, http_get, user_agent):
        calls.append((cik, start, cutoff, user_agent))
        http_get("https://synthetic.test/submissions", headers={"User-Agent": user_agent}, timeout=30)
        if len(calls) == 1:
            raise OSError("synthetic archive fetch failure")
        return []

    monkeypatch.setattr(sec_filings, "candidates_for_window", candidates, raising=False)
    legacy_calls = []
    monkeypatch.setattr(sec, "list_8k", lambda *_a, **_k: legacy_calls.append(1) or [])
    monkeypatch.setattr(
        sec,
        "rows_for_filing",
        lambda *_a, **_k: pytest.fail("no filing candidate should be processed"),
    )
    transport_calls = []
    rt = daily.Runtime(
        paths=paths,
        job_sessions=[SESSION],
        now=datetime(2026, 3, 4, 12, tzinfo=timezone.utc),
        sec_get=lambda url, **_kwargs: transport_calls.append(url) or _Response({}),
    )

    with pytest.raises(RuntimeError):
        daily.stage_sec(rt, "AAA", job_id="job-sec-pages")
    assert daily.stage_sec(rt, "AAA", job_id="job-sec-pages")[0] == "EMPTY"

    conn = sqlite3.connect(paths.bodies_db)
    attempts = conn.execute(
        "SELECT status FROM intake_log WHERE source='sec' ORDER BY attempt_id"
    ).fetchall()
    gap = conn.execute(
        "SELECT status,progress_state,coverage_from,coverage_until,pagination_complete "
        "FROM category_gaps WHERE source_id='daily:sec' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert attempts == [("ERROR",), ("EMPTY",)]
    assert gap == ("EMPTY", "completed", START, CUTOFF, 1)
    assert len(calls) == 2 and not legacy_calls
    assert transport_calls == ["https://synthetic.test/submissions"] * 2


def test_daily_candidates_still_get_official_accepted_time_from_rows_for_filing(monkeypatch, tmp_path):
    paths = daily.Paths(
        market_db=tmp_path / "market.sqlite3",
        bodies_db=tmp_path / "bodies.sqlite3",
        peer_map={},
        snapshot_db=tmp_path / "snapshot.sqlite3",
        jobs_db=tmp_path / "jobs.sqlite3",
        archive_db=tmp_path / "archive.sqlite3",
    )
    monkeypatch.setenv("SEC_USER_AGENT", "fixture-agent")
    monkeypatch.setattr(sec, "cik_for", lambda *_args, **_kwargs: CIK)
    provisional = {
        "accession": "0000001234-26-000001",
        # No verified upper bound exists for JSON/index timing skew. Keep this
        # candidate even though its provisional time is far outside the window.
        "accepted_at": "2026-03-04T23:30:00Z",
        "json_accepted_at": "2026-03-04T23:30:00Z",
        "items": ["2.02"],
        "company": "Synthetic Issuer",
        "description": "Results",
    }
    candidate_calls = []

    def candidates(cik, start, cutoff, *, http_get, user_agent):
        candidate_calls.append((cik, start, cutoff, user_agent))
        return [dict(provisional)]

    monkeypatch.setattr(sec_filings, "candidates_for_window", candidates, raising=False)
    monkeypatch.setattr(sec, "list_8k", lambda *_a, **_k: pytest.fail("recent-only path used"))
    seen = []
    from catalyst_data.ingestion.storage import page_versions
    monkeypatch.setattr(page_versions, "_path", lambda: tmp_path / "page-versions.sqlite3")
    original_rows_for_filing = sec.rows_for_filing

    def observe_rows_for_filing(cik, ticker, filing, *, window=None, **kwargs):
        seen.append((filing["accepted_at"], filing["json_accepted_at"], window))
        return original_rows_for_filing(
            cik, ticker, filing, window=window, **kwargs
        )

    monkeypatch.setattr(sec, "rows_for_filing", observe_rows_for_filing)
    index_html = '''<div class="formGrouping"><div class="infoHead">Accepted</div>
<div class="info">2026-03-03 15:30:00</div></div><html><table>
<tr><td>1</td><td>Exhibit</td><td><a href="/Archives/edgar/data/1234/000000123426000001/release.htm">release.htm</a></td><td>EX-99.1</td></tr>
</table></html>'''
    prose = "Synthetic company results describe quarterly revenue and business operations. " * 8
    transport_calls = []

    def sec_get(url, **_kwargs):
        transport_calls.append(url)
        if url.endswith("-index.htm"):
            return _Response(text=index_html)
        return _Response(text=f"<html><body><p>{prose}</p></body></html>")

    rt = daily.Runtime(
        paths=paths,
        job_sessions=[SESSION],
        now=datetime(2026, 3, 4, 12, tzinfo=timezone.utc),
        sec_get=sec_get,
    )

    result, _detail = daily.stage_sec(rt, "AAA", job_id="job-sec-accepted")

    assert result == "OK"
    assert candidate_calls == [(CIK, START, CUTOFF, "fixture-agent")]
    assert seen == [("2026-03-04T23:30:00Z", "2026-03-04T23:30:00Z", (START, CUTOFF))]
    assert any(url.endswith("-index.htm") for url in transport_calls)
    conn = sqlite3.connect(paths.bodies_db)
    url = "https://www.sec.gov/Archives/edgar/data/1234/000000123426000001/release.htm"
    asset_id = "web:" + hashlib.sha256(url.encode()).hexdigest()[:32]
    stored = conn.execute(
        "SELECT asset_id, eligible_at, text FROM web_body_chunks WHERE asset_id=? ORDER BY ordinal",
        (asset_id,),
    ).fetchall()
    conn.close()
    assert stored
    assert {row[0] for row in stored} == {"web:" + hashlib.sha256(url.encode()).hexdigest()[:32]}
    assert {row[1] for row in stored} == {"2026-03-03T20:30:00Z"}
    assert "Synthetic company results" in " ".join(row[2] for row in stored)


@pytest.mark.parametrize("old_status", ["OK", "EMPTY"])
def test_old_sec_terminal_policy_reopens_once_under_paged_policy(
    monkeypatch, tmp_path, old_status
):
    paths = daily.Paths(
        market_db=tmp_path / "market.sqlite3",
        bodies_db=tmp_path / "bodies.sqlite3",
        peer_map={},
        snapshot_db=tmp_path / "snapshot.sqlite3",
        jobs_db=tmp_path / "jobs.sqlite3",
        archive_db=tmp_path / "archive.sqlite3",
    )
    monkeypatch.setenv("SEC_USER_AGENT", "fixture-agent")
    monkeypatch.setattr(sec, "cik_for", lambda *_args, **_kwargs: CIK)

    # A terminal result under the old shared intake policy must not suppress the
    # first attempt using the new SEC-only pagination policy.
    conn = sqlite3.connect(paths.bodies_db)
    daily.ledger.ensure_ledger(conn)
    conn.execute(
        """INSERT INTO intake_log
           (subject, session_date, source, policy_version, attempted_at,
            headlines, bodies, status)
           VALUES (?, ?, 'sec', 'm9_intake_v1', '2026-03-04T12:00:00Z', 0, 0, ?)""",
        ("AAA", SESSION, old_status),
    )
    conn.commit()
    conn.close()

    calls = []

    def candidates(cik, start, cutoff, *, http_get, user_agent):
        calls.append((cik, start, cutoff, user_agent))
        # Successful empty output means every relevant page was enumerated and
        # no filing candidate requires index/attachment work.
        return []

    monkeypatch.setattr(sec_filings, "candidates_for_window", candidates, raising=False)
    monkeypatch.setattr(sec, "list_8k", lambda *_a, **_k: pytest.fail("recent-only path used"))
    rt = daily.Runtime(
        paths=paths,
        job_sessions=[SESSION],
        now=datetime(2026, 3, 4, 12, tzinfo=timezone.utc),
        sec_get=lambda _url, **_kwargs: pytest.fail("empty enumeration should not fetch filings"),
    )

    assert daily.stage_sec(rt, "AAA", job_id="job-sec-policy-upgrade")[0] == "EMPTY"
    assert len(calls) == 1

    conn = sqlite3.connect(paths.bodies_db)
    rows = conn.execute(
        "SELECT policy_version, status FROM intake_log WHERE subject='AAA' "
        "AND session_date=? AND source='sec' ORDER BY attempt_id", (SESSION,)
    ).fetchall()
    conn.close()
    assert rows == [("m9_intake_v1", old_status), ("sec_paged_v1", "EMPTY")]

    assert daily.stage_sec(rt, "AAA", job_id="job-sec-policy-upgrade")[0] == "SKIPPED"
    assert len(calls) == 1
