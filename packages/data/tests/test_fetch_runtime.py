"""Real SQLite accounting around retry and ingestion boundaries; no live providers."""
from datetime import timedelta

import pytest
import requests
from catalyst_data.ingestion import resilient as R


class Response:
    status_code = 200
    headers = {}


@pytest.fixture(autouse=True)
def local_state(tmp_path, monkeypatch):
    monkeypatch.setenv("CATALYST_RESILIENCE_DB", str(tmp_path / "state.sqlite3"))
    monkeypatch.setattr(R, "sleep", lambda _: None)


def source_report(source):
    return R.report()["sources"][source]


def test_two_http_requests_one_task_and_a_retry_have_distinct_denominators():
    answers = iter([requests.Timeout(), Response()])
    def http(*args, **kwargs):
        value = next(answers)
        if isinstance(value, Exception):
            raise value
        return value
    with R.task("test:source", request_coverage="complete") as work:
        R.get("unit", "unused", http=http)
        R.get("unit", "unused2", http=lambda *_: Response())
        work.finish("OK")
    source = source_report("test:source")
    assert source["tasks"]["registered"] == source["tasks"]["succeeded"] == 1
    assert source["requests"]["attempts"] == 3
    assert source["requests"]["successes"] == 2
    assert source["requests"]["rate"] == pytest.approx(2 / 3)
    assert source["targets"]["rate"] is None


def test_caught_ingestion_error_does_not_become_task_success():
    with R.task("test:source", request_coverage="complete") as work:
        R.get("unit", "unused", http=lambda *_: Response())
        work.finish("ERROR")
    source = source_report("test:source")
    assert source["requests"]["rate"] == 1
    assert source["tasks"]["succeeded"] == 0
    assert source["tasks"]["failed"] == 1


def test_queue_retry_restores_original_task_id_and_legacy_queue_stays_unlinked():
    @R.handler("accounting-test")
    def redo(payload):
        with R.task("test:source", request_coverage="complete") as work:
            R.get("unit", "unused", http=lambda *_: Response())
            work.finish("OK")
    with R.task("test:source", request_coverage="complete") as work:
        R.enqueue("accounting-test", "x", "unit", {})
        work.finish("ERROR")
    assert source_report("test:source")["tasks"]["retrying"] == 1
    R.run_due(R.timefields.now_utc() + timedelta(minutes=16))
    source = source_report("test:source")
    assert source["tasks"]["registered"] == source["tasks"]["succeeded"] == 1
    assert source["tasks"]["retrying"] == 0


def test_partial_transport_is_marked_unavailable_not_inferred_complete():
    with R.task("test:rss", request_coverage="unmeasured") as work:
        work.finish("EMPTY")
    assert source_report("test:rss")["requests"]["rate"] is None
    assert source_report("test:rss")["requests"]["coverage"] == "unmeasured"


def test_same_required_body_version_is_deduped_across_poll_tasks():
    for _ in range(2):
        with R.task("test:body", request_coverage="unmeasured") as work:
            work.expect_target("article:42:version1")
            work.expect_target("article:42:version1")
            work.acquire_target("article:42:version1")
            work.finish("OK")
    source = source_report("test:body")
    assert source["tasks"]["registered"] == 2
    assert source["targets"]["registered"] == source["targets"]["acquired"] == 1


def test_physical_403_is_not_a_success_even_if_business_treats_it_as_empty():
    class Forbidden(Response):
        status_code = 403
    with R.task("test:source", request_coverage="complete") as work:
        R.get("unit", "unused", http=lambda *_: Forbidden(), ok_status=(403,))
        work.finish("EMPTY")
    source = source_report("test:source")
    assert source["tasks"]["succeeded"] == 1
    assert source["requests"]["successes"] == 0


def test_legacy_report_does_not_mix_queue_completions_into_request_rate():
    R.get("unit", "unused", http=lambda *_: Response())
    report = R.report()
    assert report["schema_version"] == 2
    assert "eventual_ok" not in report["legacy"]["unit"]
    assert report["legacy_comparable_to_v2"] is False


def test_unobtained_body_is_unavailable_then_can_be_recovered_by_later_poll():
    with R.task("test:body") as work:
        work.expect_target("article:missing:v1")
        work.finish("OK")
    assert source_report("test:body")["targets"]["unavailable"] == 1
    with R.task("test:body") as work:
        work.expect_target("article:missing:v1")
        work.acquire_target("article:missing:v1")
        work.finish("OK")
    assert source_report("test:body")["targets"]["acquired"] == 1


def test_resumed_body_task_keeps_original_association_and_retry_state():
    @R.handler("body-resume")
    def redo(payload):
        with R.task("test:body") as work:
            work.expect_target("article:42:v1")
            work.acquire_target("article:42:v1")
            work.finish("OK")
    with R.task("test:body") as work:
        work.expect_target("article:42:v1")
        R.enqueue("body-resume", "one", "unit", {})
        work.finish("ERROR")
    assert source_report("test:body")["targets"]["retrying"] == 1
    assert R.run_due(R.timefields.now_utc() + timedelta(minutes=16))["DONE"] == 1
    source = source_report("test:body")
    assert source["tasks"]["registered"] == source["tasks"]["succeeded"] == 1
    assert source["targets"]["acquired"] == 1


def test_multiple_queue_items_do_not_finish_parent_task_early():
    @R.handler("multi-queue")
    def redo(payload):
        if payload["fail"]:
            raise R.TransientError("unit", "5xx")
    with R.task("test:multi", request_coverage="partial") as work:
        R.enqueue("multi-queue", "one", "unit", {"fail": False})
        R.enqueue("multi-queue", "two", "unit", {"fail": True})
        work.finish("ERROR")
    result = R.run_due(R.timefields.now_utc() + timedelta(minutes=16))
    assert result["DONE"] == result["PENDING"] == 1
    assert source_report("test:multi")["tasks"]["retrying"] == 1
    assert source_report("test:multi")["requests"]["coverage"] == "partial"


def test_queue_and_task_resolution_roll_back_together(monkeypatch):
    from catalyst_data.ingestion import fetch_runtime as FR
    @R.handler("atomic-queue")
    def redo(payload):
        pass
    with R.task("test:atomic") as work:
        R.enqueue("atomic-queue", "one", "unit", {})
        work.finish("ERROR")
    real = FR.Work.persist_final_state
    calls = []
    def fail_once(work, conn):
        if not calls:
            calls.append(True)
            real(work, conn)
            raise RuntimeError("injected write failure before commit")
        return real(work, conn)
    monkeypatch.setattr(FR.Work, "persist_final_state", fail_once)
    with pytest.raises(RuntimeError, match="injected"):
        R.run_due(R.timefields.now_utc() + timedelta(minutes=16))
    conn = R._conn()
    assert conn.execute("SELECT status FROM queue WHERE kind='atomic-queue'").fetchone()[0] == "PENDING"
    conn.close()
    assert source_report("test:atomic")["tasks"]["retrying"] == 1


def test_legacy_queued_work_never_acquires_an_invented_task_identity():
    @R.handler("legacy-queue")
    def redo(payload):
        with R.task("test:legacy") as work:
            work.finish("OK")
    R.enqueue("legacy-queue", "old", "unit", {})
    assert R.run_due(R.timefields.now_utc() + timedelta(minutes=16))["DONE"] == 1
    assert "test:legacy" not in R.report()["sources"]


def test_new_poll_exposes_body_recovery_as_pending_while_it_is_running():
    with R.task("test:body") as work:
        work.expect_target("article:42:v1")
        work.finish("OK")
    with R.task("test:body") as work:
        work.expect_target("article:42:v1")
        assert source_report("test:body")["targets"]["pending"] == 1
        work.finish("OK")
