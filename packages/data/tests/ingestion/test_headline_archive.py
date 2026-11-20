from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from catalyst_data.ingestion import coverage
from catalyst_data.ingestion.collectors import headline_archive as ha


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _items(day: date, n: int, base: int):
    ts = int(datetime(day.year, day.month, day.day, 15, tzinfo=timezone.utc).timestamp())
    return [{"id": base + i, "datetime": ts, "headline": f"h{i}", "url": f"https://x/{base + i}",
             "source": "Yahoo"} for i in range(n)]


def _coverage_bounds(start: date, end: date) -> tuple[str, str]:
    begin = datetime.combine(start, datetime.min.time(), timezone.utc) - timedelta(microseconds=1)
    until = datetime.combine(end + timedelta(days=1), datetime.min.time(), timezone.utc) - timedelta(microseconds=1)
    return begin.isoformat(timespec="microseconds"), until.isoformat(timespec="microseconds")


def _coverage_by_window(conn):
    return {
        (snapshot.attempt.subject_id, snapshot.attempt.requested_from, snapshot.attempt.requested_until): snapshot
        for snapshot in coverage.latest_coverage(conn)
    }


def test_capped_week_is_split_down_to_days_and_resumes(tmp_path):
    calls = []

    def http_get(url, params, timeout):
        start, end = date.fromisoformat(params["from"]), date.fromisoformat(params["to"])
        calls.append((params["symbol"], start, end))
        # 40 items a day, at most 240 returned: a 7-day window caps, 3-4 day windows do not
        items = []
        for ordinal in range(start.toordinal(), end.toordinal() + 1):
            items += _items(date.fromordinal(ordinal), 40, ordinal * 100)
        return FakeResponse(200, items[-240:])

    conn = ha.open_store(tmp_path / "a.sqlite3")
    stats = ha.archive(conn, ["AAPL"], date(2026, 3, 2), date(2026, 3, 8), token="t",
                       http_get=http_get, sleep=lambda s: None)
    statuses = dict(((r[0], r[1]), r[2]) for r in conn.execute(
        "SELECT window_start, window_end, status FROM archive_windows"))
    assert statuses[("2026-03-02", "2026-03-08")] == "SPLIT"
    assert statuses[("2026-03-02", "2026-03-04")] == "OK"
    assert statuses[("2026-03-05", "2026-03-08")] == "OK"
    assert conn.execute("SELECT count(*) FROM finnhub_news").fetchone()[0] == 7 * 40
    assert stats.errors == 0 and stats.capped == 0
    # older half is fetched before the newer half
    assert calls[1][1] == date(2026, 3, 2)

    snapshots = _coverage_by_window(conn)
    parent_bounds = _coverage_bounds(date(2026, 3, 2), date(2026, 3, 8))
    parent = snapshots[("AAPL", *parent_bounds)]
    assert parent.attempt.source_id == "daily:finnhub"
    assert parent.attempt.product == "headlines"
    assert parent.attempt.subject_kind == "ticker"
    assert parent.attempt.query_spec_json == "{}"
    assert parent.attempt.coverage_policy_version == "headline_archive_v1"
    assert parent.fact.status == "OK"
    assert parent.fact.enumeration_complete is False
    assert parent.fact.coverage_from is None and parent.fact.coverage_until is None
    for child_start, child_end in ((date(2026, 3, 2), date(2026, 3, 4)),
                                   (date(2026, 3, 5), date(2026, 3, 8))):
        child = snapshots[("AAPL", *_coverage_bounds(child_start, child_end))]
        assert child.fact.status == "OK" and child.fact.enumeration_complete is True
        assert (child.fact.coverage_from, child.fact.coverage_until) == _coverage_bounds(child_start, child_end)
    assert [(w.start, w.end) for w in ha.coverage_leaves(
        conn, ["AAPL"], date(2026, 3, 2), date(2026, 3, 8)
    )] == [(date(2026, 3, 2), date(2026, 3, 4)), (date(2026, 3, 5), date(2026, 3, 8))]
    attempts_before_replay = conn.execute("SELECT COUNT(*) FROM source_coverage_attempts").fetchone()[0]

    calls.clear()
    ha.archive(conn, ["AAPL"], date(2026, 3, 2), date(2026, 3, 8), token="t",
               http_get=http_get, sleep=lambda s: None)
    # the split parent is done, its halves are done: nothing is refetched
    assert calls == []
    assert conn.execute("SELECT COUNT(*) FROM source_coverage_attempts").fetchone()[0] == attempts_before_replay


def test_uncapped_and_empty_windows_record_exact_ticker_coverage(tmp_path):
    day = date(2026, 3, 2)

    def http_get(_url, params, timeout):
        if params["symbol"] == "EMPTY":
            return FakeResponse(200, [])
        return FakeResponse(200, _items(day, 1, 100))

    conn = ha.open_store(tmp_path / "coverage.sqlite3")
    ha.archive(conn, ["AAPL", "EMPTY"], day, day, token="never-stored", http_get=http_get,
               sleep=lambda _seconds: None)

    snapshots = _coverage_by_window(conn)
    bounds = _coverage_bounds(day, day)
    ok = snapshots[("AAPL", *bounds)]
    empty = snapshots[("EMPTY", *bounds)]
    assert ok.attempt.source_id == empty.attempt.source_id == "daily:finnhub"
    assert ok.attempt.product == empty.attempt.product == "headlines"
    assert ok.attempt.subject_kind == empty.attempt.subject_kind == "ticker"
    assert ok.attempt.query_spec_json == empty.attempt.query_spec_json == "{}"
    assert ok.attempt.coverage_policy_version == empty.attempt.coverage_policy_version == "headline_archive_v1"
    assert (ok.fact.status, ok.fact.enumeration_complete) == ("OK", True)
    assert (empty.fact.status, empty.fact.enumeration_complete) == ("EMPTY", True)
    assert (ok.fact.coverage_from, ok.fact.coverage_until) == bounds
    assert (empty.fact.coverage_from, empty.fact.coverage_until) == bounds
    conn.close()


def test_one_day_cap_is_not_complete_coverage(tmp_path):
    day = date(2026, 3, 2)
    conn = ha.open_store(tmp_path / "capped.sqlite3")
    stats = ha.archive(conn, ["AAPL"], day, day, token="t",
                       http_get=lambda *_args, **_kwargs: FakeResponse(200, _items(day, ha.ITEM_CAP, 1)),
                       sleep=lambda _seconds: None)

    assert stats.capped == 1
    snapshot = next(iter(coverage.latest_coverage(conn)))
    assert snapshot.fact.status == "OK"
    assert snapshot.fact.enumeration_complete is False
    assert snapshot.fact.coverage_from is None and snapshot.fact.coverage_until is None
    assert [(w.start, w.end) for w in ha.coverage_leaves(conn, ["AAPL"], day, day)] == [(day, day)]
    conn.close()


def test_oldest_week_first_across_symbols_and_errors_are_recorded(tmp_path):
    calls = []

    def http_get(url, params, timeout):
        calls.append((params["symbol"], params["from"]))
        if params["symbol"] == "BAD":
            return FakeResponse(500, None)
        return FakeResponse(200, [])

    conn = ha.open_store(tmp_path / "a.sqlite3")
    stats = ha.archive(conn, ["AAPL", "BAD"], date(2026, 3, 2), date(2026, 3, 15), token="t",
                       http_get=http_get, sleep=lambda s: None)
    firsts = [c for c in calls if c[0] == "AAPL"]
    assert firsts[0][1] == "2026-03-02" and firsts[1][1] == "2026-03-09"
    assert stats.errors == 2  # two weeks for BAD, each after MAX_ATTEMPTS
    assert len([c for c in calls if c[0] == "BAD"]) == 2 * ha.MAX_ATTEMPTS
    status = conn.execute("SELECT status, attempts FROM archive_windows WHERE symbol='BAD'").fetchall()
    assert status == [("ERROR", ha.MAX_ATTEMPTS)] * 2
    bad = [snap for snap in coverage.latest_coverage(conn) if snap.attempt.subject_id == "BAD"]
    assert len(bad) == 2
    assert all(snap.fact.status == "ERROR" and snap.fact.reason == "ERROR" for snap in bad)
    assert all(snap.fact.coverage_from is None and snap.fact.enumeration_complete is None for snap in bad)
    # an ERROR window is retried on the next run
    calls.clear()
    ha.archive(conn, ["BAD"], date(2026, 3, 2), date(2026, 3, 8), token="t",
               http_get=http_get, sleep=lambda s: None)
    assert len(calls) == ha.MAX_ATTEMPTS


@pytest.mark.parametrize("payload", [
    None,
    {},
    [{"id": 1, "datetime": 1772463600, "headline": "missing URL"}],
    [{"id": True, "datetime": 1772463600, "url": "https://x/1"}],
    [{"id": 1.9, "datetime": 1772463600, "url": "https://x/1"}],
    [{"id": -1, "datetime": 1772463600, "url": "https://x/1"}],
    [{"id": "01", "datetime": 1772463600, "url": "https://x/1"}],
])
def test_malformed_success_payload_is_error_not_empty_coverage(tmp_path, payload):
    conn = ha.open_store(tmp_path / "malformed.sqlite3")
    stats = ha.archive(conn, ["AAPL"], date(2026, 3, 2), date(2026, 3, 2), token="t",
                       http_get=lambda *_args, **_kwargs: FakeResponse(200, payload),
                       sleep=lambda _seconds: None)

    assert stats.errors == 1
    assert conn.execute("SELECT status FROM archive_windows").fetchone() == ("ERROR",)
    snapshot = next(iter(coverage.latest_coverage(conn)))
    assert snapshot.fact.status == "ERROR" and snapshot.fact.reason == "ERROR"
    assert snapshot.fact.enumeration_complete is None
    assert conn.execute("SELECT COUNT(*) FROM finnhub_news").fetchone()[0] == 0
    conn.close()


def test_canonical_integer_string_provider_id_is_accepted(tmp_path):
    conn = ha.open_store(tmp_path / "integer-string-id.sqlite3")
    day = date(2026, 3, 2)
    stats = ha.archive(conn, ["AAPL"], day, day, token="t",
                       http_get=lambda *_args, **_kwargs: FakeResponse(
                           200, [{"id": "123", "datetime": 1772463600, "url": "https://x/123"}]
                       ),
                       sleep=lambda _seconds: None)

    assert stats.errors == 0
    assert conn.execute("SELECT news_id FROM finnhub_news").fetchone() == (123,)
    conn.close()


def test_coverage_failure_rolls_back_items_and_window_before_error_receipt(tmp_path, monkeypatch):
    conn = ha.open_store(tmp_path / "atomic.sqlite3")
    real_record = coverage.record_coverage
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected coverage failure")
        return real_record(*args, **kwargs)

    monkeypatch.setattr(coverage, "record_coverage", fail_once)
    day = date(2026, 3, 2)
    stats = ha.archive(conn, ["AAPL"], day, day, token="t",
                       http_get=lambda *_args, **_kwargs: FakeResponse(200, _items(day, 1, 100)),
                       sleep=lambda _seconds: None)

    assert stats.errors == 1
    assert conn.execute("SELECT COUNT(*) FROM finnhub_news").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM archive_windows").fetchone() == ("ERROR",)
    snapshots = coverage.latest_coverage(conn)
    assert len(snapshots) == 1
    assert snapshots[0].fact.status == "ERROR" and snapshots[0].fact.reason == "ERROR"
    conn.close()


def test_skipped_preexisting_terminal_window_does_not_invent_coverage(tmp_path):
    conn = ha.open_store(tmp_path / "legacy.sqlite3")
    conn.execute(
        "INSERT INTO archive_windows VALUES (?,?,?,?,?,?,?,?)",
        ("AAPL", "2026-03-02", "2026-03-02", "EMPTY", 0, 1, "legacy", "2026-03-03T00:00:00Z"),
    )
    conn.commit()
    calls = []

    ha.archive(conn, ["AAPL"], date(2026, 3, 2), date(2026, 3, 2), token="t",
               http_get=lambda *_args, **_kwargs: calls.append("request"), sleep=lambda _seconds: None)

    assert calls == []
    assert conn.execute("SELECT COUNT(*) FROM source_coverage_attempts").fetchone()[0] == 0
    conn.close()


def test_resume_split_parent_queues_only_missing_leaf_window(tmp_path):
    conn = ha.open_store(tmp_path / "split-resume.sqlite3")
    conn.executemany(
        "INSERT INTO archive_windows VALUES (?,?,?,?,?,?,?,?)",
        [
            ("AAPL", "2026-03-02", "2026-03-08", "SPLIT", 230, 1, "", "2026-03-09T00:00:00Z"),
            ("AAPL", "2026-03-02", "2026-03-04", "EMPTY", 0, 1, "", "2026-03-09T00:00:00Z"),
        ],
    )
    conn.commit()
    calls = []

    def http_get(_url, params, timeout):
        calls.append((params["from"], params["to"]))
        return FakeResponse(200, [])

    ha.archive(conn, ["AAPL"], date(2026, 3, 2), date(2026, 3, 8), token="t",
               http_get=http_get, sleep=lambda _seconds: None)

    assert calls == [("2026-03-05", "2026-03-08")]
    assert conn.execute(
        "SELECT status FROM archive_windows WHERE window_start='2026-03-05' AND window_end='2026-03-08'"
    ).fetchone() == ("EMPTY",)
    assert [(window.start, window.end) for window in ha.coverage_leaves(
        conn, ["AAPL"], date(2026, 3, 2), date(2026, 3, 8)
    )] == [(date(2026, 3, 2), date(2026, 3, 4)), (date(2026, 3, 5), date(2026, 3, 8))]
    conn.close()


def test_subjects_cover_universe_peers_and_proxies():
    names = ha.subjects()
    assert {"AAPL", "TSM", "DELL", "GLD", "USO", "SPY"} <= set(names)
    assert not {"XLK", "SMH"} & set(names)
    assert len(names) >= 102
