"""M5-7: bounded streaming coalescer.

Final TSD §19; M5 plan M5-7. The Coalescer buffers provider deltas and flushes
on 50 ms or 2,048 UTF-8 characters (whichever first) and at provider
completion. It never writes per-token rows and never slices a completed answer
to simulate streaming. Each event records stream ID, delta ordinal, exact
accepted text, and cumulative accepted char/byte count. The provisional Answer
must equal the exact ordered concatenation of accepted persisted deltas.
"""
from __future__ import annotations

import time

from catalyst_agents.runtime.coalescer import Coalescer
from catalyst_agents.runtime.delta_sink import InMemoryDeltaSink


def _coalescer(sink: InMemoryDeltaSink, *, interval_ms: int = 50, chars: int = 2048, monotonic_fn=None):
    return Coalescer(
        flush_interval_ms=interval_ms,
        flush_chars=chars,
        sink=sink,
        stream_id="stream:1",
        monotonic_fn=monotonic_fn or time.monotonic,
    )


def test_coalescer_flushes_on_char_threshold_not_per_token() -> None:
    sink = InMemoryDeltaSink()
    coalescer = _coalescer(sink, chars=2048)
    for _ in range(100):
        coalescer.accept("a" * 20)  # 2000 chars, below threshold
    assert len(sink.deltas()) == 0
    coalescer.accept("b" * 48)  # crosses 2048
    assert len(sink.deltas()) == 1
    delta = sink.deltas()[0]
    assert delta.stream_id == "stream:1"
    assert delta.ordinal == 0
    assert delta.text == "a" * 2000 + "b" * 48
    assert delta.cumulative_chars == 2048
    assert delta.cumulative_bytes == 2048


def test_coalescer_flushes_on_interval() -> None:
    sink = InMemoryDeltaSink()
    clock = {"now": 1000.0}
    coalescer = _coalescer(
        sink, interval_ms=50, chars=2048, monotonic_fn=lambda: clock["now"]
    )
    coalescer.accept("first")
    assert len(sink.deltas()) == 0
    clock["now"] += 0.06  # 60 ms elapsed
    coalescer.accept("second")
    assert len(sink.deltas()) == 1
    assert sink.deltas()[0].text == "firstsecond"
    assert sink.deltas()[0].cumulative_chars == 11


def test_coalescer_flushes_remaining_at_completion() -> None:
    sink = InMemoryDeltaSink()
    coalescer = _coalescer(sink, interval_ms=50, chars=2048)
    coalescer.accept("hello ")
    coalescer.accept("world")
    assert len(sink.deltas()) == 0
    coalescer.complete()
    assert len(sink.deltas()) == 1
    assert sink.deltas()[0].text == "hello world"


def test_answer_equals_exact_ordered_concatenation_of_deltas() -> None:
    sink = InMemoryDeltaSink()
    coalescer = _coalescer(sink, chars=10)
    coalescer.accept("0123456789")  # flush at 10 chars
    coalescer.accept("abcdef")
    coalescer.complete()
    assert [delta.text for delta in sink.deltas()] == ["0123456789", "abcdef"]
    answer = sink.answer()
    assert answer is not None
    assert answer.text == "0123456789abcdef"
    assert answer.text == "".join(delta.text for delta in sink.deltas())
    assert answer.text_sha256


def test_cumulative_byte_counts_are_exact_utf8() -> None:
    sink = InMemoryDeltaSink()
    coalescer = _coalescer(sink, chars=20)
    chinese = "中文"
    coalescer.accept(chinese)  # 2 chars, 6 bytes
    assert len(sink.deltas()) == 0
    coalescer.accept("x" * 18)
    assert len(sink.deltas()) == 1
    delta = sink.deltas()[0]
    assert delta.cumulative_chars == 20
    assert delta.cumulative_bytes == len((chinese + "x" * 18).encode("utf-8"))
