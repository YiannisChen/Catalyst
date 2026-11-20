"""resilient: one retry policy for every provider request; queue, breaker and attempt records (fault injection)."""
import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests
from catalyst_data.ingestion import resilient as R

M9 = Path(R.__file__).parent


class Resp:
    def __init__(self, status=200, headers=None):
        self.status_code, self.headers = status, headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("CATALYST_RESILIENCE_DB", str(tmp_path / "resilience.sqlite3"))
    slept = []
    monkeypatch.setattr(R, "sleep", slept.append)
    monkeypatch.setattr(R, "rand", lambda: 1.0)
    return slept


def seq(*answers):
    answers = list(answers)
    calls = []

    def http(url, **kw):
        calls.append(url)
        a = answers.pop(0)
        if isinstance(a, BaseException):
            raise a
        return a
    http.calls = calls
    return http


def test_transient_errors_are_retried_with_backoff_and_retry_after(state):
    http = seq(requests.Timeout(), Resp(503), Resp(429, {"Retry-After": "7"}), Resp(200))
    R.POLICIES["t"] = R.Policy(attempts=4, base_s=1.0, cap_s=60, budget_s=100)
    assert R.get("t", "u", http=http).status_code == 200
    assert state == [1.0, 2.0, 7.0]  # 1 * 2^0, 1 * 2^1, then the server's Retry-After
    rep = R.report()["legacy"]["t"]
    assert rep["requests"] == 1 and rep["first_try_ok"] == 0 and rep["ok_after_retries"] == 1


def test_permanent_errors_are_not_retried():
    for answer, cls in ((Resp(404), "404"), (Resp(403), "403"), (ValueError("bad json"), "parse")):
        http = seq(answer, Resp(200))
        with pytest.raises(R.PermanentError) as e:
            R.get("p", "u", http=http)
        assert e.value.error_class == cls and len(http.calls) == 1


def test_a_status_meaning_no_data_is_returned():
    assert R.get("finra", "u", http=seq(Resp(404)), ok_status=(404,)).status_code == 404


def test_the_retry_budget_caps_the_waiting(state):
    R.POLICIES["b"] = R.Policy(attempts=10, base_s=10.0, cap_s=60, budget_s=25)
    http = seq(*[Resp(500)] * 10)
    with pytest.raises(R.TransientError):
        R.get("b", "u", http=http)
    assert sum(state) <= 25 and len(http.calls) == 2  # 10 s, then 20 s would pass the budget


def test_the_breaker_opens_after_k_failures_and_a_probe_closes_it(monkeypatch):
    R.POLICIES["c"] = R.Policy(attempts=1, breaker_k=2, breaker_open_s=600)
    for _ in range(2):
        with pytest.raises(R.TransientError):
            R.get("c", "u", http=seq(requests.ConnectionError()))
    http = seq(Resp(200))
    with pytest.raises(R.BreakerOpen):
        R.get("c", "u", http=http)
    assert http.calls == []  # no request while open
    later = R.timefields.now_utc() + timedelta(seconds=601)
    monkeypatch.setattr(R, "_now", lambda: later)
    assert R.get("c", "u", http=http).status_code == 200  # half-open probe succeeds and closes it
    assert not R.breaker_open("c")


def test_queued_work_is_retried_at_15_min_1_h_6_h_then_final():
    tries = []

    @R.handler("unit")
    def redo(payload):
        tries.append(payload["x"])
        raise R.TransientError("p", "5xx")

    t0 = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
    R._now = lambda: t0  # noqa: SLF001  (restored by the fixture's fresh module state per test file run)
    try:
        R.enqueue("unit", "k1", "p", {"x": 1}, error_class="5xx")
        assert R.run_due(t0 + timedelta(minutes=10))["PENDING"] == 0  # not due yet
        assert R.run_due(t0 + timedelta(minutes=16))["PENDING"] == 1
        assert R.run_due(t0 + timedelta(minutes=16) + timedelta(hours=1, minutes=1))["PENDING"] == 1
        assert R.run_due(t0 + timedelta(hours=8))["FAILED"] == 1
        assert tries == [1, 1, 1]
        assert R.report(now=t0 + timedelta(hours=8))["legacy"]["p"]["queue"] == {"FAILED": 1}
    finally:
        R._now = lambda: R.timefields.now_utc()  # noqa: SLF001


def test_legacy_queue_success_does_not_fabricate_a_completion_ratio():
    @R.handler("unit2")
    def redo(payload):
        R.get("q", "u", http=seq(Resp(200)))

    with pytest.raises(R.TransientError):
        R.get("q", "u", http=seq(Resp(500), Resp(500), Resp(500)))
    R.enqueue("unit2", "k", "q", {})
    assert R.run_due(R.timefields.now_utc() + timedelta(minutes=16))["DONE"] == 1
    rep = R.report()["legacy"]["q"]
    assert rep["requests"] == 1 and rep["ok_after_retries"] == 0 and "eventual_ok" not in rep
    assert rep["queue"] == {"DONE": 1}


def test_no_module_under_m9_retries_by_itself():
    """Retry loops and back-off waits live only in resilient.py (and the token bucket's own refill wait)."""
    allowed = {"resilient.py", "ratelimit.py"}
    found = []
    for root in (M9, M9 / "collectors"):
        for path in sorted(root.glob("*.py")):
            if path.name in allowed or path.name not in OWNED:
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.For) and isinstance(node.target, ast.Name) and "attempt" in node.target.id:
                    found.append(f"{path.name}:{node.lineno} for {node.target.id} in ...")
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "backoff":
                    found.append(f"{path.name}:{node.lineno} .backoff()")
    assert found == []


# Modules this branch owns (the shared intake modules move to resilient on data/time-fix, behind a shadow run).
OWNED = {"alpaca.py"}
