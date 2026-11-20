"""Provider transport errors map to the bounded technical retry seam."""
from catalyst_agents.nodes import evidence_analyst as ea
from catalyst_agents.runtime.provider_capability import (
    ModelTimeoutFailure,
    ModelTransportFailure,
)


class APIConnectionError(Exception):
    pass


class APITimeoutError(APIConnectionError):
    pass


def test_connection_error_becomes_retryable_transport_failure(monkeypatch):
    monkeypatch.setattr(ea, "TRANSPORT_RETRY_DELAY_S", 0)

    def llm(messages):
        raise APIConnectionError("Connection error.")

    try:
        ea._invoke_llm(llm, [{"role": "user", "content": "x"}])
    except ModelTransportFailure:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected ModelTransportFailure")


def test_timeout_maps_to_timeout_failure_and_other_errors_pass_through(monkeypatch):
    monkeypatch.setattr(ea, "TRANSPORT_RETRY_DELAY_S", 0)

    def slow(messages):
        raise APITimeoutError("timed out")

    def broken(messages):
        raise ValueError("bad")

    for fn, expected in ((slow, ModelTimeoutFailure), (broken, ValueError)):
        try:
            ea._invoke_llm(fn, [{"role": "user", "content": "x"}])
        except expected:
            continue
        raise AssertionError(f"expected {expected.__name__}")
