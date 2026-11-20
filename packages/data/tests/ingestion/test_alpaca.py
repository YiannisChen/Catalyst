import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from catalyst_data.ingestion import coverage
from catalyst_data.ingestion.collectors import alpaca
from catalyst_data.ingestion.coverage_readiness import (
    CoveragePlan,
    CoverageRequirement,
    PlannedPartition,
    SourceCapability,
    evaluate_coverage,
    expected_partition_id,
)
from catalyst_data.ingestion.storage import store
from catalyst_data.ingestion.time import timefields
from catalyst_data.storage import body_versions, temporal_evidence


def _item(i, updated, content="<p>Body</p>", symbols=("AAPL",)):
    return {"id": i, "created_at": "2026-10-20T13:00:00Z", "updated_at": updated, "headline": "h", "summary": "s",
            "content": content, "url": f"https://www.benzinga.com/news/26/10/{i}/x", "author": "a",
            "source": "benzinga", "symbols": list(symbols)}


def test_store_keeps_first_seen_and_every_version(tmp_path):
    conn = alpaca.connect(tmp_path / "n.sqlite3")
    assert alpaca.store_news(conn, [_item(1, "2026-10-20T13:00:01Z")], "2026-10-20T13:05:00Z") == 1
    assert alpaca.store_news(conn, [_item(1, "2026-10-20T15:00:00Z", "<p>Body, corrected</p>")],
                             "2026-10-20T16:00:00Z") == 0
    row = conn.execute("SELECT first_seen_at, updated_at, content FROM news WHERE id = 1").fetchone()
    assert row == ("2026-10-20T13:05:00Z", "2026-10-20T15:00:00Z", "<p>Body, corrected</p>")
    assert [r[0] for r in conn.execute("SELECT updated_at FROM news_versions ORDER BY 1")] == [
        "2026-10-20T13:00:01Z", "2026-10-20T15:00:00Z"]
    versions = body_versions.list_versions(conn, source_id="alpaca_news", document_id="1")
    assert [(v["body"], v["observed_at"], v["provider_updated_at"]) for v in versions] == [
        ("<p>Body</p>", "2026-10-20T13:05:00.000000+00:00", "2026-10-20T13:00:01.000000+00:00"),
        ("<p>Body, corrected</p>", "2026-10-20T16:00:00.000000+00:00", "2026-10-20T15:00:00.000000+00:00"),
    ]
    assert alpaca.text("<p>A&amp;B</p><script>x</script>") == "A&B"


def test_store_news_records_complete_versions_and_does_not_invent_provider_update(tmp_path):
    conn = alpaca.connect(tmp_path / "n.sqlite3")
    alpaca.store_news(conn, [_item(2, None)], "2026-10-20T13:05:00Z")
    versions = body_versions.list_versions(conn, source_id="alpaca_news", document_id="2")
    assert len(versions) == 1
    assert versions[0]["body"] == "<p>Body</p>"
    assert versions[0]["provider_updated_at"] is None
    assert versions[0]["published_at"] == "2026-10-20T13:00:00.000000+00:00"


def test_append_body_version_error_includes_document_identity(tmp_path):
    conn = alpaca.connect(tmp_path / "n.sqlite3")

    with pytest.raises(alpaca.body_versions.BodyVersionError) as exc_info:
        alpaca._append_body_version(
            conn,
            document_id="alpaca-doc:bad",
            title="Empty body article",
            body="",
            observed_at="2026-10-20T13:05:00Z",
            first_seen_at="2026-10-20T13:05:00Z",
            published_at="2026-10-20T13:00:00Z",
            provider_updated_at=None,
            published_raw="2026-10-20T13:00:00Z",
            url="https://example.com/article",
            dedup_url="https://example.com/article",
            publisher="Example",
            symbols=[],
            is_backfill=False,
        )

    assert type(exc_info.value) is alpaca.body_versions.BodyVersionError
    assert "body must contain complete nonempty text" in str(exc_info.value)
    assert "source_id=" + alpaca.SOURCE in str(exc_info.value)
    assert "document_id=alpaca-doc:bad" in str(exc_info.value)


def test_first_versioned_update_preserves_the_preexisting_current_body(tmp_path):
    conn = alpaca.connect(tmp_path / "n.sqlite3")
    conn.execute(
        "INSERT INTO news VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (3, "2026-10-20T13:00:00Z", "2026-10-20T13:10:00Z", "old title", None,
         "<p>Previously stored body</p>", "https://example.com/3", None, "benzinga", '["AAPL"]',
         "2026-10-20T13:05:00Z"),
    )
    conn.commit()

    alpaca.store_news(
        conn, [_item(3, "2026-10-20T15:00:00Z", "<p>Corrected body</p>")],
        "2026-10-20T16:00:00Z",
    )

    versions = body_versions.list_versions(conn, source_id="alpaca_news", document_id="3")
    assert [version["body"] for version in versions] == [
        "<p>Previously stored body</p>", "<p>Corrected body</p>",
    ]
    assert all(version["observed_at"] == "2026-10-20T16:00:00.000000+00:00" for version in versions)
    result = temporal_evidence.as_of(
        conn, "AAPL", datetime(2026, 10, 20, tzinfo=timezone.utc),
        datetime(2026, 10, 21, tzinfo=timezone.utc),
        datetime(2026, 10, 20, 17, tzinfo=timezone.utc),
        mode="observed", data_snapshot_id="snapshot", correction_table_id="corrections",
    )
    assert [row["text"] for row in result["evidence"]] == ["<p>Corrected body</p>"]


def test_store_news_registers_temporal_identity_and_projects_full_text(tmp_path):
    conn = alpaca.connect(tmp_path / "n.sqlite3")

    alpaca.store_news(conn, [_item(4, "2026-10-20T15:00:00Z", symbols=("AAPL", "BRK.B"))],
                      "2026-10-20T16:00:00Z", is_backfill=True)

    row = conn.execute(
        """SELECT m.ticker,m.kind,m.eligible_at,m.is_backfill,m.origin,m.url,m.canonical_url,
                  x.published_at_source,x.published_at_precision,x.version
           FROM temporal_evidence_map m JOIN temporal_evidence_versions x USING(version_id)
           ORDER BY m.ticker"""
    ).fetchall()
    assert [(item[0], item[1], item[3], item[4], item[7], item[8], item[9]) for item in row] == [
        ("AAPL", "body", 1, "publisher:benzinga.com", "api", "second", 1),
        ("BRK-B", "body", 1, "publisher:benzinga.com", "api", "second", 1),
    ]
    assert all(item[5] == _item(4, "2026-10-20T15:00:00Z")["url"] for item in row)
    assert all(item[6] == item[5] for item in row)
    assert len({item[2] for item in row}) == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM evidence_text_projection WHERE evidence_class='body_evidence'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT evidence_id FROM evidence_text_fts WHERE evidence_text_fts MATCH ?",
        ('"Body"',),
    ).fetchone()[0].startswith("web:")


def test_backfill_skips_done_days(tmp_path, monkeypatch):
    conn = alpaca.connect(tmp_path / "n.sqlite3")
    asked = []
    monkeypatch.setattr(alpaca, "news_pages", lambda start, end, symbols=None, **_kwargs: (asked.append(start), iter([[_item(2, None)]]))[1])
    assert alpaca.backfill(conn, ["2026-10-19"], log=lambda *_: None)["OK"] == 1
    assert asked == ["2026-10-19T04:00:00Z"]  # midnight New York (EDT)
    assert alpaca.backfill(conn, ["2026-10-19"])["skipped"] == 1


def test_backfill_does_not_skip_an_intake_row_without_coverage(tmp_path, monkeypatch):
    conn = alpaca.connect(tmp_path / "n.sqlite3")
    alpaca.ledger.record_attempt(
        conn, subject="news:backfill", session_date="2026-10-19", source=alpaca.SOURCE,
        status="OK", headlines=1, bodies=0, attempted_at="2026-10-19T20:00:00Z",
    )
    asked = []
    monkeypatch.setattr(
        alpaca, "news_pages",
        lambda start, end, symbols=None, **_kwargs: (asked.append(start), iter([[]]))[1],
    )
    result = alpaca.backfill(conn, ["2026-10-19"], log=lambda *_: None)
    assert result["skipped"] == 0
    assert result["EMPTY"] == 1
    assert asked == ["2026-10-19T04:00:00Z"]


def test_backfill_does_not_skip_a_day_whose_body_targets_are_incomplete(tmp_path, monkeypatch):
    conn = alpaca.connect(tmp_path / "n.sqlite3")
    asked = []

    def pages(start, end, symbols=None, **_kwargs):
        asked.append(start)
        return iter([[_item(9, "2026-10-19T15:00:00Z", content="", symbols=("AAPL",))]])

    monkeypatch.setattr(alpaca, "news_pages", pages)
    first = alpaca.backfill(conn, ["2026-10-19"], ["AAPL"], log=lambda *_: None)
    second = alpaca.backfill(conn, ["2026-10-19"], ["AAPL"], log=lambda *_: None)
    assert first["skipped"] == 0 and first["OK"] == 1
    assert second["skipped"] == 0 and second["OK"] == 1
    assert asked == ["2026-10-19T04:00:00Z", "2026-10-19T04:00:00Z"]


class _CoverageWork:
    def __init__(self):
        self.expected = set()
        self.acquired = set()
        self.finished = None
        self.complete = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def expect_target(self, target):
        self.expected.add(target)

    def acquire_target(self, target):
        self.acquired.add(target)

    def discovery_complete(self, complete):
        self.complete = complete

    def finish(self, status):
        self.finished = status


def _coverage_rows(conn):
    return coverage.list_attempts(conn), coverage.list_facts(conn)


def test_poll_records_feed_coverage_and_second_precision_closed_window(tmp_path, monkeypatch):
    conn = alpaca.connect(tmp_path / "n.sqlite3")
    now = datetime(2026, 10, 20, 16, 30, 0, 987654, tzinfo=timezone.utc)
    monkeypatch.setattr(alpaca, "_now", lambda: now)
    work = _CoverageWork()
    monkeypatch.setattr(alpaca.resilient, "task", lambda *_a, **_k: work)
    requested = []

    def pages(start, end, symbols=None, *, content=True, on_cursor=None):
        requested.append((start, end, symbols, content))
        yield [_item(11, "2026-10-20T16:29:30Z")]
        yield [_item(11, "2026-10-20T16:29:31Z")]

    monkeypatch.setattr(alpaca, "news_pages", pages)

    result = alpaca.poll(conn, minutes=90, log=lambda *_: None)

    attempts, facts = _coverage_rows(conn)
    assert result["status"] == "OK"
    assert requested == [("2026-10-20T15:00:00Z", "2026-10-20T16:30:00Z", None, True)]
    assert [(a.source_id, a.product, a.subject_kind, a.subject_id) for a in attempts] == [
        ("alpaca_news:news:poll", "headlines", "feed", "alpaca_news"),
        ("alpaca_news:news:poll", "full_text", "feed", "alpaca_news"),
    ]
    assert all(a.query_spec_json == "{}" for a in attempts)
    by_product = {fact.product: fact for fact in facts}
    assert set(by_product) == {"headlines", "full_text"}
    assert all((fact.status, fact.enumeration_complete, fact.coverage_from, fact.coverage_until) == (
        "OK", True, "2026-10-20T15:00:00.000000+00:00", "2026-10-20T16:29:59.000000+00:00"
    ) for fact in by_product.values())
    assert all(a.subject_kind != "ticker" for a in attempts)
    assert by_product["headlines"].expected_target_count is None
    assert by_product["full_text"].expected_target_count == by_product["full_text"].acquired_target_count == 1
    assert {t.identity for t in coverage.list_targets(conn, coverage_id=by_product["full_text"].coverage_id)} == {
        "provider:alpaca_news:news:poll:11"
    }


def test_poll_missing_body_is_headline_only_and_receipts_share_archive_transaction(tmp_path, monkeypatch):
    conn = alpaca.connect(tmp_path / "n.sqlite3")
    monkeypatch.setattr(alpaca, "_now", lambda: datetime(2026, 10, 20, 16, 30, tzinfo=timezone.utc))
    monkeypatch.setattr(alpaca.resilient, "task", lambda *_a, **_k: _CoverageWork())
    monkeypatch.setattr(alpaca, "news_pages", lambda *_a, **_k: iter([[_item(12, None, content="")]]))

    alpaca.poll(conn, minutes=90, log=lambda *_: None)

    attempts, facts = _coverage_rows(conn)
    assert len(attempts) == len(facts) == 2
    by_product = {fact.product: fact for fact in facts}
    assert by_product["headlines"].status == "OK"
    assert by_product["full_text"].status == "OK"
    assert by_product["full_text"].expected_target_count == 1
    assert by_product["full_text"].acquired_target_count == 0
    assert coverage.list_targets(conn, coverage_id=by_product["full_text"].coverage_id)[0].acquired is False
    assert conn.execute("SELECT COUNT(*) FROM news WHERE id=12").fetchone()[0] == 1


def test_empty_exhausted_poll_records_zero_target_empty_full_text_fact(tmp_path, monkeypatch):
    conn = alpaca.connect(tmp_path / "n.sqlite3")
    monkeypatch.setattr(alpaca, "_now", lambda: datetime(2026, 10, 20, 16, 30, tzinfo=timezone.utc))
    monkeypatch.setattr(alpaca.resilient, "task", lambda *_a, **_k: _CoverageWork())
    monkeypatch.setattr(alpaca, "news_pages", lambda *_a, **_k: iter([[]]))

    result = alpaca.poll(conn, minutes=90, log=lambda *_: None)

    assert result["status"] == "EMPTY"
    facts = {fact.product: fact for fact in coverage.list_facts(conn)}
    assert set(facts) == {"headlines", "full_text"}
    assert all(fact.status == "EMPTY" and fact.enumeration_complete is True for fact in facts.values())
    assert facts["full_text"].expected_target_count == facts["full_text"].acquired_target_count == 0
    assert not coverage.list_targets(conn, coverage_id=facts["full_text"].coverage_id)


def test_poll_partial_page_failure_rolls_back_articles_and_records_incomplete_coverage(tmp_path, monkeypatch):
    conn = alpaca.connect(tmp_path / "n.sqlite3")
    monkeypatch.setattr(alpaca, "_now", lambda: datetime(2026, 10, 20, 16, 30, tzinfo=timezone.utc))
    monkeypatch.setattr(alpaca.resilient, "task", lambda *_a, **_k: _CoverageWork())

    def broken_pages(*_a, **kwargs):
        yield [_item(13, "2026-10-20T16:29:30Z")]
        kwargs["on_cursor"]("page-cursor-sha256")
        raise RuntimeError("provider URL contained APCA_API_SECRET_KEY=private-value")

    monkeypatch.setattr(alpaca, "news_pages", broken_pages)

    logs = []
    result = alpaca.poll(conn, minutes=90, log=logs.append)

    attempts, facts = _coverage_rows(conn)
    assert result["status"] == "ERROR"
    assert conn.execute("SELECT COUNT(*) FROM news WHERE id=13").fetchone()[0] == 0
    assert len(attempts) == len(facts) == 2
    assert all(f.status == "ERROR" and f.enumeration_complete is False for f in facts)
    full_text = next(f for f in facts if f.product == "full_text")
    assert full_text.expected_target_count == 1 and full_text.acquired_target_count == 0
    assert full_text.cursor == "page-cursor-sha256"
    assert logs == ["alpaca news poll: RuntimeError"]
    assert coverage.list_targets(conn, coverage_id=full_text.coverage_id)[0].acquired is False
    assert conn.execute("SELECT COUNT(*) FROM body_versions WHERE document_id='13'").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM intake_log WHERE subject='news:poll'").fetchone()[0] == "ERROR"


def test_backfill_records_requested_tickers_as_separate_coverage_partitions(tmp_path, monkeypatch):
    conn = alpaca.connect(tmp_path / "n.sqlite3")
    monkeypatch.setattr(alpaca.resilient, "task", lambda *_a, **_k: _CoverageWork())
    requested = []

    def pages(start, end, symbols=None, *, content=True, on_cursor=None):
        requested.append((start, end, symbols, content))
        yield [_item(14, "2026-10-20T14:00:00Z", symbols=("AAPL",))]

    monkeypatch.setattr(alpaca, "news_pages", pages)

    result = alpaca.backfill(conn, ["2026-10-20"], ["AAPL", "MSFT"], log=lambda *_: None)

    attempts, facts = _coverage_rows(conn)
    assert result["OK"] == 1
    assert requested == [("2026-10-20T04:00:00Z", "2026-10-21T04:00:00Z", ["AAPL", "MSFT"], True)]
    assert {(a.source_id, a.product, a.subject_kind, a.subject_id) for a in attempts} == {
        ("alpaca_news:backfill", "headlines", "ticker", "AAPL"),
        ("alpaca_news:backfill", "full_text", "ticker", "AAPL"),
        ("alpaca_news:backfill", "headlines", "ticker", "MSFT"),
        ("alpaca_news:backfill", "full_text", "ticker", "MSFT"),
    }
    assert all(a.query_spec_json == "{}" for a in attempts)
    assert len(facts) == 4
    assert all(f.enumeration_complete is True for f in facts)
    assert {(f.product, f.subject_id, f.status) for f in facts} == {
        ("headlines", "AAPL", "OK"), ("full_text", "AAPL", "OK"),
        ("headlines", "MSFT", "EMPTY"), ("full_text", "MSFT", "EMPTY"),
    }
    assert all(f.coverage_until == "2026-10-21T03:59:59.000000+00:00" for f in facts)
    full_text = [f for f in facts if f.product == "full_text"]
    assert next(f for f in full_text if f.subject_id == "AAPL").expected_target_count == 1
    empty = next(f for f in full_text if f.subject_id == "MSFT")
    assert empty.expected_target_count == empty.acquired_target_count == 0


@pytest.mark.parametrize("mode", ["failure", "empty", "symbol_mismatch", "body_success"])
def test_backfill_can_write_only_selected_product_without_replacing_headline_fact(tmp_path, monkeypatch, mode):
    conn = alpaca.connect(tmp_path / "selected-product.sqlite3")
    monkeypatch.setattr(alpaca.resilient, "task", lambda *_a, **_k: _CoverageWork())
    monkeypatch.setattr(alpaca, "_now", lambda: datetime(2026, 10, 21, 12, tzinfo=timezone.utc))
    monkeypatch.setattr(alpaca, "news_pages", lambda *_a, **_k: iter([[
        _item(90, "2026-10-20T14:00:00Z", content="")
    ]]))
    alpaca.backfill(conn, ["2026-10-20"], ["AAPL"], log=lambda *_: None)
    old_headline = next(s for s in coverage.latest_coverage(conn)
                        if s.attempt.product == "headlines")

    if mode == "failure":
        def pages(*_a, **_k):
            raise RuntimeError("provider failure")
    elif mode == "empty":
        pages = lambda *_a, **_k: iter([[]])
    elif mode == "symbol_mismatch":
        pages = lambda *_a, **_k: iter([[_item(91, "2026-10-20T14:00:00Z", symbols=("MSFT",))]])
    else:
        pages = lambda *_a, **_k: iter([[_item(92, "2026-10-20T14:00:00Z", symbols=("AAPL",))]])
    monkeypatch.setattr(alpaca, "news_pages", pages)

    result = alpaca.backfill(conn, ["2026-10-20"], ["AAPL"], products={"full_text"},
                             log=lambda *_: None)

    headline_now = next(s for s in coverage.latest_coverage(conn)
                        if s.attempt.product == "headlines")
    body_now = next(s for s in coverage.latest_coverage(conn)
                    if s.attempt.product == "full_text")
    assert headline_now.attempt.attempt_seq == old_headline.attempt.attempt_seq
    assert headline_now.fact.coverage_id == old_headline.fact.coverage_id
    assert alpaca._headline_coverage_complete(headline_now)
    if mode == "failure":
        assert not alpaca._backfill_coverage_complete(conn, "2026-10-20", ["AAPL"])
        assert result["ERROR"] == 1
        assert body_now.fact.status == "ERROR"
        assert body_now.fact.enumeration_complete is False
    elif mode in {"empty", "symbol_mismatch"}:
        assert not alpaca._backfill_coverage_complete(conn, "2026-10-20", ["AAPL"])
        assert result["ERROR"] == 1
        assert body_now.fact.status == "ERROR"
        assert body_now.fact.enumeration_complete is False
        assert not alpaca._full_text_coverage_complete(body_now)
    else:
        assert result["OK"] == 1
        assert body_now.fact.status == "OK"
        assert body_now.fact.acquired_target_count == body_now.fact.expected_target_count == 1


def test_backfill_default_still_writes_both_products(tmp_path, monkeypatch):
    conn = alpaca.connect(tmp_path / "default-products.sqlite3")
    monkeypatch.setattr(alpaca.resilient, "task", lambda *_a, **_k: _CoverageWork())
    monkeypatch.setattr(alpaca, "news_pages", lambda *_a, **_k: iter([[]]))
    alpaca.backfill(conn, ["2026-10-20"], ["AAPL"], log=lambda *_: None)
    assert {fact.product for fact in coverage.list_facts(conn)} == {"headlines", "full_text"}


def test_alpaca_backfill_facts_satisfy_unfiltered_g2_ticker_requirement(tmp_path, monkeypatch):
    conn = alpaca.connect(tmp_path / "n.sqlite3")
    monkeypatch.setattr(alpaca.resilient, "task", lambda *_a, **_k: _CoverageWork())
    monkeypatch.setattr(
        alpaca, "news_pages",
        lambda *_a, **_k: iter([[_item(15, "2026-10-20T14:00:00Z", symbols=("AAPL",))]]),
    )

    alpaca.backfill(conn, ["2026-10-20"], ["AAPL"], log=lambda *_: None)

    attempt = next(a for a in coverage.list_attempts(conn) if a.product == "full_text")
    fact = next(f for f in coverage.list_facts(conn) if f.attempt_id == attempt.attempt_id)
    assert timefields.stored(fact.coverage_until) == (
        timefields.stored(attempt.requested_until) - timedelta(seconds=1)
    )
    part_values = dict(
        partition_id="pending", source_id=attempt.source_id, product=attempt.product,
        subject_kind=attempt.subject_kind, subject_id=attempt.subject_id,
        requested_from=attempt.requested_from, requested_until=attempt.requested_until,
        query_filters=json.loads(attempt.query_spec_json),
        coverage_policy_version=attempt.coverage_policy_version, session_ids=(),
    )
    part = PlannedPartition(**part_values)
    part = PlannedPartition(**(part_values | {"partition_id": expected_partition_id(part)}))
    requirement = CoverageRequirement(
        requirement_id="alpaca-full-text", session_date="2026-10-20", ticker="AAPL",
        category="target_news", scope_kind="target", scope_id="AAPL",
        subject_kind="ticker", subject_id="AAPL", product="full_text",
        requested_from=attempt.requested_from, requested_until=fact.coverage_until,
        query_filters={}, source_ids=(attempt.source_id,), source_policy="all",
    )
    plan = CoveragePlan(
        (part,),
        {attempt.source_id: SourceCapability(frozenset({"headlines", "full_text"}), frozenset({"ticker"}))},
        {},
    )

    result = evaluate_coverage([requirement], plan, coverage.latest_coverage(conn))

    assert result[0]["complete"] is True
    assert result[0]["status"] == "OK"


def test_news_poll_cli_records_disabled_scope_without_provider_call(tmp_path, monkeypatch):
    db_path = tmp_path / "disabled.sqlite3"
    now = datetime(2026, 10, 20, 16, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(alpaca, "_now", lambda: now)
    monkeypatch.setattr(alpaca, "configured", lambda: False)
    monkeypatch.setattr(alpaca, "call", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("provider called")))

    assert alpaca.main(["news-poll", "--db", str(db_path), "--minutes", "90"]) == 0

    conn = alpaca.connect(db_path)
    attempts, facts = _coverage_rows(conn)
    conn.close()
    assert [(a.source_id, a.product, a.subject_kind, a.subject_id) for a in attempts] == [
        ("alpaca_news:news:poll", "headlines", "feed", "alpaca_news"),
        ("alpaca_news:news:poll", "full_text", "feed", "alpaca_news"),
    ]
    assert all(f.status == "NOT_FETCHED" and f.reason == "DISABLED" for f in facts)
    assert all(f.enumeration_complete is None and f.coverage_from is None for f in facts)


def test_news_pages_rejects_missing_or_malformed_news_list(monkeypatch):
    monkeypatch.setattr(alpaca, "call", lambda *_a, **_k: {})
    with pytest.raises(ValueError, match="news"):
        list(alpaca.news_pages("2026-10-20T00:00:00Z", "2026-10-20T01:00:00Z"))

    monkeypatch.setattr(alpaca, "call", lambda *_a, **_k: {"news": [None]})
    with pytest.raises(ValueError, match="article"):
        list(alpaca.news_pages("2026-10-20T00:00:00Z", "2026-10-20T01:00:00Z"))

    monkeypatch.setattr(
        alpaca, "call", lambda *_a, **_k: {"news": [], "next_page_token": 7},
    )
    with pytest.raises(ValueError, match="cursor"):
        list(alpaca.news_pages("2026-10-20T00:00:00Z", "2026-10-20T01:00:00Z"))


def test_news_pages_rejects_repeated_cursor(monkeypatch):
    calls = 0

    def repeated(*_a, **_k):
        nonlocal calls
        calls += 1
        if calls > 2:
            raise RuntimeError("collector retried a repeated cursor")
        return {"news": [], "next_page_token": "repeat-me"}

    monkeypatch.setattr(alpaca, "call", repeated)
    with pytest.raises(ValueError, match="cursor"):
        list(alpaca.news_pages("2026-10-20T00:00:00Z", "2026-10-20T01:00:00Z"))


def test_backfill_large_ticker_sets_have_stable_noncolliding_dedup_keys(tmp_path, monkeypatch):
    conn = alpaca.connect(tmp_path / "n.sqlite3")
    works = []
    asked = []

    def task(*_a, **_k):
        work = _CoverageWork()
        works.append(work)
        return work

    def pages(start, end, symbols=None, **_kwargs):
        asked.append(tuple(symbols or ()))
        return iter([[]])

    monkeypatch.setattr(alpaca.resilient, "task", task)
    monkeypatch.setattr(alpaca, "news_pages", pages)
    first = ["AAPL", "MSFT", "NVDA", "AMZN"]
    other = ["AAPL", "MSFT", "NVDA", "META"]

    assert alpaca.backfill(conn, ["2026-10-19"], first, log=lambda *_: None)["EMPTY"] == 1
    assert alpaca.backfill(conn, ["2026-10-19"], other, log=lambda *_: None)["EMPTY"] == 1
    assert alpaca.backfill(conn, ["2026-10-19"], list(reversed(first)), log=lambda *_: None)["skipped"] == 1

    assert asked == [tuple(sorted(first)), tuple(sorted(other))]
    subjects = [row[0] for row in conn.execute(
        "SELECT DISTINCT subject FROM intake_log WHERE subject LIKE 'news:backfill:%' ORDER BY 1"
    )]
    assert len(subjects) == 2 and subjects[0] != subjects[1]
    assert subjects[0] != "news:backfill:symbols" and subjects[1] != "news:backfill:symbols"


def test_backfill_registers_missing_and_acquired_body_targets(tmp_path, monkeypatch):
    conn = alpaca.connect(tmp_path / "n.sqlite3")
    work = _CoverageWork()
    monkeypatch.setattr(alpaca.resilient, "task", lambda *_a, **_k: work)
    monkeypatch.setattr(
        alpaca, "news_pages",
        lambda *_a, **_k: iter([[
            _item(21, "2026-10-20T14:00:00Z", content="<p>Body</p>", symbols=("AAPL",)),
            _item(22, None, content="", symbols=("AAPL",)),
        ]]),
    )

    assert alpaca.backfill(conn, ["2026-10-20"], ["AAPL"], log=lambda *_: None)["OK"] == 1

    assert work.expected == {
        "alpaca:21:2026-10-20T14:00:00Z",
        "alpaca:22:unversioned",
    }
    assert work.acquired == {"alpaca:21:2026-10-20T14:00:00Z"}


def test_news_poll_holds_the_shared_store_lock_while_opening_and_collecting(tmp_path, monkeypatch):
    db_path = tmp_path / "alpaca.sqlite3"
    lock_checks = []

    def connect_while_locked(path):
        with pytest.raises(store.RefreshBusy):
            with store.refresh_lock(path):
                pass
        lock_checks.append("locked")
        return sqlite3.connect(":memory:")

    monkeypatch.setattr(alpaca, "connect", connect_while_locked)
    monkeypatch.setattr(alpaca, "configured", lambda: False)
    monkeypatch.setattr(alpaca, "_record_disabled", lambda *_args, **_kwargs: None)

    assert alpaca.main(["news-poll", "--db", str(db_path)]) == 0
    assert lock_checks == ["locked"]
    assert Path(f"{db_path}.refresh.lock").is_file()


def test_news_poll_waits_for_competing_backfill_lock_without_losing_the_poll(tmp_path, monkeypatch):
    db_path = tmp_path / "alpaca.sqlite3"
    entered = threading.Event()
    release = threading.Event()
    poll_calls = []
    result = []

    def hold_backfill_lock():
        with store.refresh_lock(db_path):
            entered.set()
            assert release.wait(timeout=2)

    owner = threading.Thread(target=hold_backfill_lock)
    owner.start()
    assert entered.wait(timeout=2)
    monkeypatch.setattr(alpaca, "POLL_LOCK_WAIT_SECONDS", 1)
    monkeypatch.setattr(alpaca, "configured", lambda: True)
    monkeypatch.setattr(alpaca, "poll", lambda _conn, *, minutes: poll_calls.append(minutes) or {"status": "EMPTY"})
    caller = threading.Thread(target=lambda: result.append(alpaca.main(["news-poll", "--db", str(db_path)])))
    caller.start()
    time.sleep(0.1)
    assert caller.is_alive()
    release.set()
    owner.join(timeout=2)
    caller.join(timeout=2)

    assert not owner.is_alive()
    assert not caller.is_alive()
    assert result == [0]
    assert poll_calls
    assert not alpaca._poll_retry_path(db_path).exists()


def test_news_poll_timeout_persists_window_and_next_timer_retries_it(tmp_path, monkeypatch):
    db_path = tmp_path / "alpaca.sqlite3"
    poll_calls = []
    first_run = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
    clock = iter([first_run, first_run + timedelta(hours=2)])
    monkeypatch.setattr(alpaca, "POLL_LOCK_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(alpaca, "configured", lambda: True)
    monkeypatch.setattr(alpaca, "_now", lambda: next(clock))
    monkeypatch.setattr(alpaca, "poll", lambda _conn, *, minutes: poll_calls.append(minutes) or {"status": "EMPTY"})

    with store.refresh_lock(db_path):
        assert alpaca.main(["news-poll", "--db", str(db_path), "--minutes", "90"]) == 75
    marker = alpaca._poll_retry_path(db_path)
    assert marker.exists()
    assert poll_calls == []

    assert alpaca.main(["news-poll", "--db", str(db_path), "--minutes", "90"]) == 0
    assert poll_calls == [210]
    assert not marker.exists()


def test_news_poll_retry_outside_the_window_records_the_gap_and_consumes_the_marker(tmp_path, monkeypatch, capsys):
    """A marker too old for a poll to refill used to exit 2 on every later poll and was never consumed (10-29)."""
    import sqlite3

    db_path = tmp_path / "alpaca.sqlite3"
    marker = alpaca._poll_retry_path(db_path)
    started_at = datetime(2026, 10, 1, tzinfo=timezone.utc)
    alpaca._write_poll_retry(marker, started_at)
    now = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
    poll_calls = []
    monkeypatch.setattr(alpaca, "configured", lambda: True)
    monkeypatch.setattr(alpaca, "_now", lambda: now)
    monkeypatch.setattr(alpaca, "poll", lambda _conn, *, minutes: poll_calls.append(minutes) or {"status": "EMPTY"})

    assert alpaca.main(["news-poll", "--db", str(db_path), "--minutes", "90"]) == 0
    assert poll_calls == [90]  # the poll itself is not widened
    assert not marker.exists()
    report = json.loads(capsys.readouterr().out.strip())
    assert (report["retry_pending"], report["retry_reason"], report["gap_recorded"]) == (False, "outside_auto_window", True)
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT product, status, reason, requested_from, requested_until FROM source_coverage_facts "
                            "ORDER BY product").fetchall()
    assert [r[:3] for r in rows] == [("full_text", "NOT_FETCHED", "deferred"), ("headlines", "NOT_FETCHED", "deferred")]
    assert rows[0][3].startswith("2026-10-01") and rows[0][4].startswith("2026-10-27T10:30")  # [marker, now - 90 min)

    assert alpaca.main(["news-poll", "--db", str(db_path), "--minutes", "90"]) == 0  # the next poll is plain again


def test_news_poll_marker_write_failure_is_reported_without_false_retry_claim(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "alpaca.sqlite3"
    marker = alpaca._poll_retry_path(db_path)
    poll_calls = []

    def fail_marker_write(*_args, **_kwargs):
        raise PermissionError("read-only marker directory")

    monkeypatch.setattr(alpaca, "POLL_LOCK_WAIT_SECONDS", 0.01)
    monkeypatch.setattr(alpaca, "configured", lambda: True)
    monkeypatch.setattr(alpaca, "_now", lambda: datetime(2026, 10, 27, 12, tzinfo=timezone.utc))
    monkeypatch.setattr(alpaca, "_write_poll_retry", fail_marker_write)
    monkeypatch.setattr(alpaca, "poll", lambda _conn, *, minutes: poll_calls.append(minutes) or {"status": "EMPTY"})

    with store.refresh_lock(db_path):
        assert alpaca.main(["news-poll", "--db", str(db_path), "--minutes", "90"]) == 75
    busy = json.loads(capsys.readouterr().out.strip())
    assert busy == {"cmd": "news-poll", "status": "BUSY", "retry_pending": False,
                    "retry_reason": "retry_marker_unavailable"}
    assert not marker.exists()
    assert poll_calls == []

    # Marker failure does not prevent the existing current-window poll when the lock is available.
    assert alpaca.main(["news-poll", "--db", str(db_path), "--minutes", "90"]) == 0
    assert poll_calls == [90]
    assert not marker.exists()
    success = json.loads(capsys.readouterr().out.strip())
    assert success["status"] == "EMPTY"
    assert success["retry_pending"] is False
    assert success["retry_reason"] == "retry_marker_unavailable"


def test_news_poll_retry_uses_fixed_three_day_window(tmp_path):
    marker = tmp_path / "retry.json"
    now = datetime(2026, 10, 28, 18, tzinfo=timezone.utc)
    alpaca._write_poll_retry(marker, now - timedelta(days=alpaca.POLL_RETRY_FALLBACK_DAYS))
    assert alpaca._poll_retry_minutes(marker, 90, now) == (3 * 24 * 60, None)
    marker.unlink()
    alpaca._write_poll_retry(marker, now - timedelta(days=alpaca.POLL_RETRY_FALLBACK_DAYS, seconds=1))
    assert alpaca._poll_retry_minutes(marker, 90, now) == (90, "outside_auto_window")


def test_poll_retry_marker_z_time_keeps_the_same_utc_elapsed_minutes(tmp_path, monkeypatch):
    marker = tmp_path / "retry.json"
    started_at = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
    now = started_at + timedelta(minutes=210)
    alpaca._write_poll_retry(marker, started_at)

    assert alpaca._poll_retry_minutes(marker, 90, now) == (210, None)


def test_poll_retry_widens_inside_the_fixed_bound(tmp_path):
    marker = tmp_path / "retry.json"
    now = datetime(2026, 10, 30, 16, tzinfo=timezone.utc)
    started_at = now - timedelta(hours=32)
    alpaca._write_poll_retry(marker, started_at)

    assert alpaca._poll_retry_minutes(marker, 90, now) == (32 * 60, None)
    marker.unlink()
    alpaca._write_poll_retry(marker, now - timedelta(days=alpaca.POLL_RETRY_FALLBACK_DAYS + 1))
    assert alpaca._poll_retry_minutes(marker, 90, now) == (90, "outside_auto_window")


def test_an_expired_marker_is_kept_when_the_poll_fails_or_the_gap_cannot_be_recorded(tmp_path, monkeypatch, capsys):
    import sqlite3

    db_path = tmp_path / "alpaca.sqlite3"
    marker = alpaca._poll_retry_path(db_path)
    alpaca._write_poll_retry(marker, datetime(2026, 10, 1, tzinfo=timezone.utc))
    now = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(alpaca, "configured", lambda: True)
    monkeypatch.setattr(alpaca, "_now", lambda: now)

    monkeypatch.setattr(alpaca, "poll", lambda _conn, *, minutes: {"status": "ERROR"})
    assert alpaca.main(["news-poll", "--db", str(db_path), "--minutes", "90"]) == 1
    assert marker.exists()  # a failed poll consumes nothing and records no gap
    monkeypatch.setattr(alpaca, "poll", lambda _conn, *, minutes: {"status": "EMPTY"})
    monkeypatch.setattr(alpaca, "_record_unrefilled_gap", lambda *_a: False)
    assert alpaca.main(["news-poll", "--db", str(db_path), "--minutes", "90"]) == 2
    assert marker.exists()  # no record of the hole: the marker stays the only trace
    capsys.readouterr()
    with sqlite3.connect(db_path) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        rows = conn.execute("SELECT count(*) FROM source_coverage_facts WHERE reason='deferred'").fetchone()[0] \
            if "source_coverage_facts" in tables else 0
    assert rows == 0
