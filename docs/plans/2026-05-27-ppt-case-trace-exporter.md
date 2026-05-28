# PPT Case Trace Exporter Implementation Plan

**Goal:** Build a cloud-runnable exporter that runs selected `SUFFICIENT` Catalyst cases and saves PPT-ready full-chain evidence, node outputs, prompts, and raw LLM responses.

**Architecture:** Add a narrow PPT-only experiment script under `scripts/` and a small support module under `packages/agents/catalyst_agents/trace/` for recording LLM calls. Do not change the production graph behavior. The exporter will reuse `build_attribution_graph`, `RetrievalMetadata`, existing frozen DB/LanceDB paths, and the final `AttributionState`, then write structured JSON/CSV/Markdown artifacts per case.

**Tech Stack:** Python 3.11+, LangGraph Catalyst agents, SQLite frozen DB, LanceDB retrieval index, pytest, stdlib `csv/json/pathlib/sqlite3`, optional live LLM provider already used by Catalyst.

---

## Context And Constraints

- Existing trace persistence only stores node events and structured `decision`; it does not store full prompt or raw response.
- `packages/agents/catalyst_agents/nodes/critic.py` and `judge.py` receive `response.content` from `invoke_with_retries`, parse it, then discard the raw response.
- `packages/agents/catalyst_agents/graph.py` returns the final state with useful fields: `retrieved_chunks`, `reranked_chunks`, `all_graded_chunks`, `graded_evidence`, `critic_reasoning`, `critic_decision`, `causes`, `summary_md`, `grounding_rate`, `output_status`, `cost_breakdown`, `run_id`, `trace_id`.
- The exporter is for defense/PPT assets only. It must not alter online serving behavior.
- Initial experiment set:
  - Main case: `g013` / `NVDA` / `2025-10-28`
  - Extra cases: `g019` / `AMD` / `2025-10-06`, `g029` / `GOOGL` / `2025-09-03`, `g042` / `AAPL` / `2025-04-03`, `g025` / `UNH` / `2025-07-29`
- Default output root:
  - `outputs/ppt_case_trace/`

---

## Desired Output Layout

For each case:

```text
outputs/ppt_case_trace/<case_id>_<ticker>_<trade_date>/
  00_case/
    case_meta.json
    case_summary.md
  01_data_core/
    source_counts.json
    evidence_pool_sample.csv
    evidence_pool_readable.md
    price_window.csv
  02_miner/
    miner_input.json
    retrieved_chunks_top20.csv
    reranked_chunks_top8.csv
    miner_decision.json
    miner_trace.md
  03_critic/
    critic_prompt.md
    critic_raw_response.json
    critic_parsed.json
    all_graded_chunks.csv
    filtered_graded_evidence.csv
    critic_decision.json
    critic_slide_extract.md
  04_judge/
    judge_prompt.md
    judge_raw_response.json
    judge_parsed.json
    causes.csv
    citation_chain.csv
    judge_slide_extract.md
  05_validator_finalizer/
    validator_prompt.md
    validator_raw_response.json
    validator_result.json
    final_output.md
    final_status.json
  06_ppt_extracts/
    full_pipeline_summary.json
    full_pipeline_table.md
    node_timeline.csv
    ppt_ready_quotes.md
    ppt_case_report.md
```

If Validator has no LLM call in the current code path, `validator_prompt.md` and `validator_raw_response.json` should still be created with an explicit empty/absent marker:

```json
{
  "available": false,
  "reason": "validator did not invoke the recording LLM in this run"
}
```

---

## Task 1: Add Recording LLM Utility

**Files:**
- Create: `packages/agents/catalyst_agents/trace/recording.py`
- Test: `packages/agents/tests/test_recording_llm.py`

**Step 1: Write the failing test**

Create `packages/agents/tests/test_recording_llm.py`:

```python
from __future__ import annotations

from catalyst_agents.trace.recording import RecordingLLM


class Response:
    content = '{"ok": true}'

    class usage:
        input_tokens = 10
        output_tokens = 3
        total_tokens = 13


class InnerLLM:
    def __init__(self) -> None:
        self.prompts = []

    def invoke(self, prompt: str):
        self.prompts.append(prompt)
        return Response()


def test_recording_llm_captures_prompt_raw_response_and_usage():
    calls = []
    llm = RecordingLLM(InnerLLM(), calls, default_node="critic")

    response = llm.invoke("hello")

    assert response.content == '{"ok": true}'
    assert calls == [
        {
            "node": "critic",
            "attempt": 1,
            "prompt": "hello",
            "raw_response": '{"ok": true}',
            "usage": {
                "input_tokens": 10,
                "output_tokens": 3,
                "total_tokens": 13,
            },
        }
    ]


def test_recording_llm_supports_current_node_switching():
    calls = []
    llm = RecordingLLM(InnerLLM(), calls, default_node="unknown")

    with llm.node("judge"):
        llm.invoke("judge prompt")

    assert calls[0]["node"] == "judge"
```

**Step 2: Run test to verify it fails**

Run:

```bash
PYTHONPATH=packages/agents:packages/data-core pytest packages/agents/tests/test_recording_llm.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'catalyst_agents.trace.recording'`.

**Step 3: Write minimal implementation**

Create `packages/agents/catalyst_agents/trace/recording.py`:

```python
"""Utilities for recording LLM prompts and raw responses for PPT case traces."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator


def _usage_to_dict(usage: Any) -> dict[str, int | None] | None:
    if usage is None:
        return None
    return {
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
    }


class RecordingLLM:
    """Wrap an LLM and append prompt/raw-response records for each invoke call."""

    def __init__(self, inner: Any, calls: list[dict[str, Any]], *, default_node: str = "unknown") -> None:
        self.inner = inner
        self.calls = calls
        self._current_node = default_node
        self._attempt_by_node: dict[str, int] = {}

    @contextmanager
    def node(self, node_name: str) -> Iterator[None]:
        previous = self._current_node
        self._current_node = node_name
        try:
            yield
        finally:
            self._current_node = previous

    def invoke(self, prompt: str) -> Any:
        node_name = self._current_node
        self._attempt_by_node[node_name] = self._attempt_by_node.get(node_name, 0) + 1
        response = self.inner.invoke(prompt)
        self.calls.append(
            {
                "node": node_name,
                "attempt": self._attempt_by_node[node_name],
                "prompt": prompt,
                "raw_response": getattr(response, "content", ""),
                "usage": _usage_to_dict(getattr(response, "usage", None)),
            }
        )
        return response
```

**Step 4: Run test to verify it passes**

Run:

```bash
PYTHONPATH=packages/agents:packages/data-core pytest packages/agents/tests/test_recording_llm.py -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add packages/agents/catalyst_agents/trace/recording.py packages/agents/tests/test_recording_llm.py
git commit -m "test: add recording llm utility"
```

---

## Task 2: Add Node-Aware Raw Response Capture In Exporter

**Files:**
- Create: `scripts/export_ppt_case_trace.py`
- Test: `packages/agents/tests/test_ppt_case_exporter_recording.py`

**Step 1: Write the failing test**

Create `packages/agents/tests/test_ppt_case_exporter_recording.py`:

```python
from __future__ import annotations

import json

from scripts.export_ppt_case_trace import NodeAwareLLM


class Response:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = None


class Inner:
    def __init__(self) -> None:
        self.responses = [Response('{"critic": true}'), Response('{"judge": true}')]
        self.calls = 0

    def invoke(self, prompt: str):
        response = self.responses[self.calls]
        self.calls += 1
        return response


def test_node_aware_llm_records_critic_and_judge_separately():
    calls = []
    llm = NodeAwareLLM(Inner(), calls)

    with llm.node("critic"):
        llm.invoke("critic prompt")
    with llm.node("judge"):
        llm.invoke("judge prompt")

    assert [c["node"] for c in calls] == ["critic", "judge"]
    assert json.loads(calls[0]["raw_response"]) == {"critic": True}
    assert json.loads(calls[1]["raw_response"]) == {"judge": True}
```

**Step 2: Run test to verify it fails**

Run:

```bash
PYTHONPATH=.:packages/agents:packages/data-core pytest packages/agents/tests/test_ppt_case_exporter_recording.py -v
```

Expected: FAIL because `scripts/export_ppt_case_trace.py` does not exist.

**Step 3: Write minimal implementation**

Create `scripts/export_ppt_case_trace.py` with this initial scaffold:

```python
#!/usr/bin/env python3
"""Export defense-PPT-ready full-chain traces for selected Catalyst cases."""
from __future__ import annotations

from catalyst_agents.trace.recording import RecordingLLM


class NodeAwareLLM(RecordingLLM):
    """Script-local alias for clarity in tests and exporter code."""
```

**Step 4: Run test to verify it passes**

Run:

```bash
PYTHONPATH=.:packages/agents:packages/data-core pytest packages/agents/tests/test_ppt_case_exporter_recording.py -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add scripts/export_ppt_case_trace.py packages/agents/tests/test_ppt_case_exporter_recording.py
git commit -m "test: scaffold ppt case trace exporter"
```

---

## Task 3: Add Case Selection And CLI Parsing

**Files:**
- Modify: `scripts/export_ppt_case_trace.py`
- Test: `packages/agents/tests/test_ppt_case_exporter_cli.py`

**Step 1: Write the failing test**

Create `packages/agents/tests/test_ppt_case_exporter_cli.py`:

```python
from __future__ import annotations

from pathlib import Path

from scripts.export_ppt_case_trace import DEFAULT_CASES, parse_args, selected_cases


def test_default_cases_are_ppt_sufficient_set():
    ids = [case["id"] for case in DEFAULT_CASES]
    assert ids == ["g013", "g019", "g029", "g042", "g025"]


def test_selected_cases_filters_case_ids():
    cases = selected_cases(["g029", "g025"])
    assert [case["id"] for case in cases] == ["g029", "g025"]


def test_parse_args_defaults():
    args = parse_args([])
    assert args.out_dir == Path("outputs/ppt_case_trace")
    assert args.case_id == []
    assert args.trace_db == Path("outputs/ppt_case_trace/trace.db")
```

**Step 2: Run test to verify it fails**

Run:

```bash
PYTHONPATH=.:packages/agents:packages/data-core pytest packages/agents/tests/test_ppt_case_exporter_cli.py -v
```

Expected: FAIL because `DEFAULT_CASES`, `parse_args`, and `selected_cases` are missing.

**Step 3: Implement CLI parsing**

Add to `scripts/export_ppt_case_trace.py`:

```python
from argparse import ArgumentParser, Namespace
from pathlib import Path
from typing import Sequence


DEFAULT_CASES = [
    {
        "id": "g013",
        "ticker": "NVDA",
        "trade_date": "2025-10-28",
        "price_move_pct": 4.98,
        "expected_status": "SUFFICIENT",
        "label": "AI infrastructure / strategic partnerships",
    },
    {
        "id": "g019",
        "ticker": "AMD",
        "trade_date": "2025-10-06",
        "price_move_pct": 23.71,
        "expected_status": "SUFFICIENT",
        "label": "OpenAI partnership / AI supply chain",
    },
    {
        "id": "g029",
        "ticker": "GOOGL",
        "trade_date": "2025-09-03",
        "price_move_pct": 9.14,
        "expected_status": "SUFFICIENT",
        "label": "antitrust ruling / regulatory relief",
    },
    {
        "id": "g042",
        "ticker": "AAPL",
        "trade_date": "2025-04-03",
        "price_move_pct": -9.25,
        "expected_status": "SUFFICIENT",
        "label": "tariffs / supply-chain risk",
    },
    {
        "id": "g025",
        "ticker": "UNH",
        "trade_date": "2025-07-29",
        "price_move_pct": -7.46,
        "expected_status": "SUFFICIENT",
        "label": "earnings miss / guidance reset",
    },
]


def parse_args(argv: Sequence[str] | None = None) -> Namespace:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--case-id", action="append", default=[], help="Run only this case id; repeatable.")
    parser.add_argument("--out-dir", type=Path, default=Path("outputs/ppt_case_trace"))
    parser.add_argument("--frozen-db", type=Path, default=Path("data/catalyst_eval_frozen_v2.db"))
    parser.add_argument("--lancedb-dir", type=Path, default=Path("data/lancedb_gold/eval_frozen"))
    parser.add_argument("--trace-db", type=Path, default=Path("outputs/ppt_case_trace/trace.db"))
    parser.add_argument("--model-id", default="claude-sonnet-4-20250514")
    parser.add_argument("--dry-run", action="store_true", help="Validate config and print selected cases without LLM calls.")
    return parser.parse_args(argv)


def selected_cases(case_ids: Sequence[str]) -> list[dict]:
    if not case_ids:
        return list(DEFAULT_CASES)
    wanted = set(case_ids)
    cases = [case for case in DEFAULT_CASES if case["id"] in wanted]
    missing = wanted - {case["id"] for case in cases}
    if missing:
        raise SystemExit(f"Unknown case id(s): {', '.join(sorted(missing))}")
    return cases
```

**Step 4: Run test to verify it passes**

Run:

```bash
PYTHONPATH=.:packages/agents:packages/data-core pytest packages/agents/tests/test_ppt_case_exporter_cli.py -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add scripts/export_ppt_case_trace.py packages/agents/tests/test_ppt_case_exporter_cli.py
git commit -m "feat: add ppt case exporter cli"
```

---

## Task 4: Add Artifact Writing Helpers

**Files:**
- Modify: `scripts/export_ppt_case_trace.py`
- Test: `packages/agents/tests/test_ppt_case_exporter_artifacts.py`

**Step 1: Write the failing test**

Create `packages/agents/tests/test_ppt_case_exporter_artifacts.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

from scripts.export_ppt_case_trace import case_dir_name, write_json, write_markdown, write_rows_csv


def test_case_dir_name_is_stable():
    assert case_dir_name({"id": "g013", "ticker": "NVDA", "trade_date": "2025-10-28"}) == "g013_NVDA_2025-10-28"


def test_write_json_creates_parent_and_pretty_json(tmp_path: Path):
    path = tmp_path / "a" / "b.json"
    write_json(path, {"x": 1})
    assert json.loads(path.read_text()) == {"x": 1}
    assert "\n  " in path.read_text()


def test_write_rows_csv_handles_missing_columns(tmp_path: Path):
    path = tmp_path / "rows.csv"
    write_rows_csv(path, [{"a": 1, "b": 2}, {"a": 3}], columns=["a", "b"])
    assert path.read_text().splitlines() == ["a,b", "1,2", "3,"]


def test_write_markdown_creates_parent(tmp_path: Path):
    path = tmp_path / "report" / "x.md"
    write_markdown(path, "# hello")
    assert path.read_text() == "# hello\n"
```

**Step 2: Run test to verify it fails**

Run:

```bash
PYTHONPATH=.:packages/agents:packages/data-core pytest packages/agents/tests/test_ppt_case_exporter_artifacts.py -v
```

Expected: FAIL because helper functions are missing.

**Step 3: Implement artifact helpers**

Add:

```python
import csv
import json
from typing import Any


def case_dir_name(case: dict[str, Any]) -> str:
    return f"{case['id']}_{case['ticker']}_{case['trade_date']}"


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_markdown(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def write_rows_csv(path: Path, rows: list[dict[str, Any]], *, columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
```

**Step 4: Run test to verify it passes**

Run:

```bash
PYTHONPATH=.:packages/agents:packages/data-core pytest packages/agents/tests/test_ppt_case_exporter_artifacts.py -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add scripts/export_ppt_case_trace.py packages/agents/tests/test_ppt_case_exporter_artifacts.py
git commit -m "feat: add ppt artifact writers"
```

---

## Task 5: Add Data-Core Export Helpers

**Files:**
- Modify: `scripts/export_ppt_case_trace.py`
- Test: `packages/agents/tests/test_ppt_case_exporter_data_core.py`

**Step 1: Write the failing test**

Create `packages/agents/tests/test_ppt_case_exporter_data_core.py`:

```python
from __future__ import annotations

import sqlite3
from pathlib import Path

from catalyst_data.storage.sqlite import compute_asset_id, init_db, upsert_clean_asset
from scripts.export_ppt_case_trace import collect_evidence_pool, compute_date_range


def _insert(conn, ticker: str, source: str, date: str, content: str) -> None:
    asset_id = compute_asset_id(ticker, date, source)
    upsert_clean_asset(
        conn,
        asset_id=asset_id,
        ticker=ticker,
        source_type=source,
        reference_date=date,
        content_md=content,
    )


def test_compute_date_range_uses_three_day_window():
    assert compute_date_range("2025-10-28") == ("2025-10-25", "2025-10-31")


def test_collect_evidence_pool_filters_ticker_and_window(tmp_path: Path):
    db_path = tmp_path / "frozen.db"
    conn = sqlite3.connect(db_path)
    init_db(conn)
    _insert(conn, "NVDA", "polygon_news", "2025-10-28", "direct evidence")
    _insert(conn, "NVDA", "fmp_fundamentals", "2025-10-29", "fundamental evidence")
    _insert(conn, "AMD", "polygon_news", "2025-10-28", "wrong ticker")
    _insert(conn, "NVDA", "polygon_news", "2025-11-10", "out of window")
    conn.close()

    rows = collect_evidence_pool(db_path, {"ticker": "NVDA", "trade_date": "2025-10-28"})

    assert [row["source_type"] for row in rows] == ["polygon_news", "fmp_fundamentals"]
    assert all(row["ticker"] == "NVDA" for row in rows)
```

**Step 2: Run test to verify it fails**

Run:

```bash
PYTHONPATH=.:packages/agents:packages/data-core pytest packages/agents/tests/test_ppt_case_exporter_data_core.py -v
```

Expected: FAIL because functions are missing.

**Step 3: Implement data-core helpers**

Add:

```python
from datetime import datetime, timedelta
import sqlite3


DIRECT_PPT_SOURCES = {
    "polygon_news",
    "fmp_news",
    "finnhub_company_news",
    "fmp_fundamentals",
    "sec_filing",
    "polygon_ohlcv",
    "fred_macro",
    "fred_rates",
    "macro_news",
    "market_news",
    "geopolitical_news",
    "policy_news",
}


def compute_date_range(trade_date: str, window_days: int = 3) -> tuple[str, str]:
    dt = datetime.strptime(trade_date, "%Y-%m-%d")
    return (
        (dt - timedelta(days=window_days)).strftime("%Y-%m-%d"),
        (dt + timedelta(days=window_days)).strftime("%Y-%m-%d"),
    )


def collect_evidence_pool(db_path: Path, case: dict[str, Any]) -> list[dict[str, Any]]:
    start, end = compute_date_range(case["trade_date"])
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT asset_id, ticker, source_type, reference_date, content_md
            FROM clean_assets
            WHERE ticker = ?
              AND reference_date >= ?
              AND reference_date <= ?
              AND COALESCE(is_duplicate, 0) = 0
            ORDER BY reference_date ASC, source_type ASC, asset_id ASC
            """,
            (case["ticker"], start, end),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "asset_id": row["asset_id"],
            "ticker": row["ticker"],
            "source_type": row["source_type"],
            "reference_date": row["reference_date"],
            "content_md": row["content_md"],
            "excerpt": preview(row["content_md"], 220),
        }
        for row in rows
        if row["source_type"] in DIRECT_PPT_SOURCES
    ]


def preview(text: str | None, max_chars: int = 220) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[:max_chars].rsplit(" ", 1)[0] + " ..."
```

**Step 4: Run test to verify it passes**

Run:

```bash
PYTHONPATH=.:packages/agents:packages/data-core pytest packages/agents/tests/test_ppt_case_exporter_data_core.py -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add scripts/export_ppt_case_trace.py packages/agents/tests/test_ppt_case_exporter_data_core.py
git commit -m "feat: collect ppt evidence pool"
```

---

## Task 6: Add Prompt/Raw Capture By Monkeypatching Node Retry Functions

**Files:**
- Modify: `scripts/export_ppt_case_trace.py`
- Test: `packages/agents/tests/test_ppt_case_exporter_capture.py`

**Step 1: Write the failing test**

Create `packages/agents/tests/test_ppt_case_exporter_capture.py`:

```python
from __future__ import annotations

import json

import catalyst_agents.nodes.critic as critic_mod
import catalyst_agents.nodes.judge as judge_mod
from scripts.export_ppt_case_trace import RawCallRecorder, capture_node_calls


class Response:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = None


class LLM:
    def invoke(self, prompt: str):
        return Response('{"ok": true}')


def test_capture_node_calls_records_node_name(monkeypatch):
    recorder = RawCallRecorder()

    with capture_node_calls(recorder):
        response, parsed = critic_mod.invoke_with_retries(
            LLM(),
            "critic prompt",
            parse_fn=json.loads,
            node_name="Critic",
        )
        judge_mod.invoke_with_retries(
            LLM(),
            "judge prompt",
            parse_fn=json.loads,
            node_name="Judge",
        )

    assert parsed == {"ok": True}
    assert [call["node"] for call in recorder.calls] == ["critic", "judge"]
    assert recorder.calls[0]["prompt"] == "critic prompt"
    assert recorder.calls[1]["raw_response"] == '{"ok": true}'
```

**Step 2: Run test to verify it fails**

Run:

```bash
PYTHONPATH=.:packages/agents:packages/data-core pytest packages/agents/tests/test_ppt_case_exporter_capture.py -v
```

Expected: FAIL because capture helpers are missing.

**Step 3: Implement capture helpers**

Add:

```python
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import wraps
from typing import Callable, Iterator

import catalyst_agents.nodes.critic as critic_mod
import catalyst_agents.nodes.judge as judge_mod
import catalyst_agents.nodes.validator as validator_mod


@dataclass
class RawCallRecorder:
    calls: list[dict[str, Any]] = field(default_factory=list)

    def record(self, *, node: str, prompt: str, response: Any, parsed: dict[str, Any]) -> None:
        self.calls.append(
            {
                "node": node,
                "prompt": prompt,
                "raw_response": getattr(response, "content", ""),
                "parsed": parsed,
                "usage": None,
            }
        )


def _node_from_name(node_name: str) -> str:
    lowered = node_name.lower()
    if "critic" in lowered:
        return "critic"
    if "judge" in lowered:
        return "judge"
    if "validator" in lowered:
        return "validator"
    return lowered.replace(" ", "_")


@contextmanager
def capture_node_calls(recorder: RawCallRecorder) -> Iterator[None]:
    originals: list[tuple[Any, Callable]] = []

    def patch_module(module: Any) -> None:
        original = module.invoke_with_retries
        originals.append((module, original))

        @wraps(original)
        def wrapped(llm: Any, prompt: str, *, parse_fn: Callable[[str], dict], node_name: str = "node", retry_prompt_fn: Callable[[str, int], str] | None = None):
            response, parsed = original(
                llm,
                prompt,
                parse_fn=parse_fn,
                node_name=node_name,
                retry_prompt_fn=retry_prompt_fn,
            )
            recorder.record(node=_node_from_name(node_name), prompt=prompt, response=response, parsed=parsed)
            return response, parsed

        module.invoke_with_retries = wrapped

    for module in (critic_mod, judge_mod):
        patch_module(module)
    if hasattr(validator_mod, "invoke_with_retries"):
        patch_module(validator_mod)

    try:
        yield
    finally:
        for module, original in originals:
            module.invoke_with_retries = original
```

**Step 4: Run test to verify it passes**

Run:

```bash
PYTHONPATH=.:packages/agents:packages/data-core pytest packages/agents/tests/test_ppt_case_exporter_capture.py -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add scripts/export_ppt_case_trace.py packages/agents/tests/test_ppt_case_exporter_capture.py
git commit -m "feat: capture node raw responses for ppt export"
```

---

## Task 7: Add Single-Case Runner

**Files:**
- Modify: `scripts/export_ppt_case_trace.py`
- Test: `packages/agents/tests/test_ppt_case_exporter_run_case.py`

**Step 1: Write the failing test**

Create `packages/agents/tests/test_ppt_case_exporter_run_case.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

from scripts.export_ppt_case_trace import export_case_artifacts


def test_export_case_artifacts_writes_core_files(tmp_path: Path):
    case = {
        "id": "g013",
        "ticker": "NVDA",
        "trade_date": "2025-10-28",
        "price_move_pct": 4.98,
        "expected_status": "SUFFICIENT",
        "label": "AI infrastructure",
    }
    state = {
        "retrieved_chunks": [{"asset_id": "a1", "source_type": "polygon_news", "reference_date": "2025-10-28", "content_md": "news", "rrf_score": 1.0}],
        "reranked_chunks": [{"asset_id": "a1", "source_type": "polygon_news", "reference_date": "2025-10-28", "content_md": "news", "rerank_score": 0.9}],
        "all_graded_chunks": [{"chunk_id": "a1", "relevance": 0.9, "category": "sector", "temporal_match": True, "reasoning": "direct"}],
        "graded_evidence": [{"chunk_id": "a1", "relevance": 0.9, "category": "sector", "temporal_match": True, "reasoning": "direct"}],
        "critic_decision": None,
        "critic_reasoning": "enough",
        "causes": [{"text": "AI demand", "category": "sector", "confidence": 0.9, "evidence_ids": ["a1"], "direction": "positive"}],
        "summary_md": "NVDA moved because [a1].",
        "grounding_rate": 1.0,
        "output_status": "SUFFICIENT",
        "validation_error": None,
        "cost_breakdown": [],
        "total_cost_usd": 0.0,
        "total_tokens": 0,
    }
    raw_calls = [
        {"node": "critic", "prompt": "critic prompt", "raw_response": '{"critic": true}', "parsed": {"critic": True}},
        {"node": "judge", "prompt": "judge prompt", "raw_response": '{"judge": true}', "parsed": {"judge": True}},
    ]
    evidence_pool = [{"asset_id": "a1", "ticker": "NVDA", "source_type": "polygon_news", "reference_date": "2025-10-28", "excerpt": "news"}]

    export_case_artifacts(tmp_path, case, state, raw_calls, evidence_pool)

    case_dir = tmp_path / "g013_NVDA_2025-10-28"
    assert json.loads((case_dir / "00_case" / "case_meta.json").read_text())["ticker"] == "NVDA"
    assert (case_dir / "03_critic" / "critic_prompt.md").read_text() == "critic prompt\n"
    assert json.loads((case_dir / "04_judge" / "judge_raw_response.json").read_text())["raw_response"] == '{"judge": true}'
    assert (case_dir / "06_ppt_extracts" / "ppt_case_report.md").exists()
```

**Step 2: Run test to verify it fails**

Run:

```bash
PYTHONPATH=.:packages/agents:packages/data-core pytest packages/agents/tests/test_ppt_case_exporter_run_case.py -v
```

Expected: FAIL because `export_case_artifacts` is missing.

**Step 3: Implement artifact export**

Add:

```python
def _status_value(value: Any) -> str | None:
    if value is None:
        return None
    return getattr(value, "name", None) or str(value)


def _call_for(raw_calls: list[dict[str, Any]], node: str) -> dict[str, Any] | None:
    return next((call for call in raw_calls if call.get("node") == node), None)


def _chunk_row(chunk: dict[str, Any], rank: int) -> dict[str, Any]:
    return {
        "rank": rank,
        "asset_id": chunk.get("asset_id", ""),
        "source_type": chunk.get("source_type", ""),
        "reference_date": chunk.get("reference_date", ""),
        "score": chunk.get("rerank_score", chunk.get("rrf_score", "")),
        "excerpt": preview(chunk.get("content_md", ""), 220),
    }


def export_case_artifacts(
    out_root: Path,
    case: dict[str, Any],
    state: dict[str, Any],
    raw_calls: list[dict[str, Any]],
    evidence_pool: list[dict[str, Any]],
) -> Path:
    case_root = out_root / case_dir_name(case)
    critic_call = _call_for(raw_calls, "critic")
    judge_call = _call_for(raw_calls, "judge")
    validator_call = _call_for(raw_calls, "validator")

    write_json(case_root / "00_case" / "case_meta.json", case)
    write_markdown(
        case_root / "00_case" / "case_summary.md",
        f"# {case['ticker']} / {case['trade_date']}\n\n"
        f"- Move: {case['price_move_pct']}%\n"
        f"- Expected status: {case['expected_status']}\n"
        f"- Label: {case.get('label', '')}\n",
    )

    counts: dict[str, int] = {}
    for row in evidence_pool:
        counts[row["source_type"]] = counts.get(row["source_type"], 0) + 1
    write_json(case_root / "01_data_core" / "source_counts.json", counts)
    write_rows_csv(
        case_root / "01_data_core" / "evidence_pool_sample.csv",
        evidence_pool,
        columns=["asset_id", "ticker", "source_type", "reference_date", "excerpt"],
    )
    write_markdown(
        case_root / "01_data_core" / "evidence_pool_readable.md",
        "\n".join(f"- `{r['source_type']}` {r['reference_date']}: {r.get('excerpt', '')}" for r in evidence_pool[:12]),
    )

    retrieved = state.get("retrieved_chunks", [])
    reranked = state.get("reranked_chunks", [])
    write_json(
        case_root / "02_miner" / "miner_input.json",
        {
            "ticker": case["ticker"],
            "trade_date": case["trade_date"],
            "price_move_pct": case["price_move_pct"],
            "query": f"Why did {case['ticker']} move on {case['trade_date']}?",
        },
    )
    write_rows_csv(case_root / "02_miner" / "retrieved_chunks_top20.csv", [_chunk_row(c, i) for i, c in enumerate(retrieved[:20], 1)], columns=["rank", "asset_id", "source_type", "reference_date", "score", "excerpt"])
    write_rows_csv(case_root / "02_miner" / "reranked_chunks_top8.csv", [_chunk_row(c, i) for i, c in enumerate(reranked[:8], 1)], columns=["rank", "asset_id", "source_type", "reference_date", "score", "excerpt"])
    write_json(case_root / "02_miner" / "miner_decision.json", {"retrieved": len(retrieved), "reranked": len(reranked)})
    write_markdown(case_root / "02_miner" / "miner_trace.md", f"Retrieved {len(retrieved)} chunks; reranked {len(reranked)} chunks.")

    write_markdown(case_root / "03_critic" / "critic_prompt.md", (critic_call or {}).get("prompt", ""))
    write_json(case_root / "03_critic" / "critic_raw_response.json", {"available": critic_call is not None, "raw_response": (critic_call or {}).get("raw_response")})
    write_json(case_root / "03_critic" / "critic_parsed.json", (critic_call or {}).get("parsed", {}))
    write_rows_csv(case_root / "03_critic" / "all_graded_chunks.csv", state.get("all_graded_chunks", []), columns=["chunk_id", "relevance", "category", "temporal_match", "reasoning"])
    write_rows_csv(case_root / "03_critic" / "filtered_graded_evidence.csv", state.get("graded_evidence", []), columns=["chunk_id", "relevance", "category", "temporal_match", "reasoning"])
    decision = state.get("critic_decision")
    write_json(
        case_root / "03_critic" / "critic_decision.json",
        {
            "sufficiency": getattr(decision, "sufficiency", None),
            "next_action": getattr(decision, "next_action", None),
            "magnitude_coverage": getattr(decision, "magnitude_coverage", None),
            "reasoning": state.get("critic_reasoning", ""),
        },
    )
    write_markdown(case_root / "03_critic" / "critic_slide_extract.md", state.get("critic_reasoning", ""))

    write_markdown(case_root / "04_judge" / "judge_prompt.md", (judge_call or {}).get("prompt", ""))
    write_json(case_root / "04_judge" / "judge_raw_response.json", {"available": judge_call is not None, "raw_response": (judge_call or {}).get("raw_response")})
    write_json(case_root / "04_judge" / "judge_parsed.json", (judge_call or {}).get("parsed", {}))
    write_rows_csv(case_root / "04_judge" / "causes.csv", state.get("causes", []), columns=["text", "category", "confidence", "direction", "evidence_ids"])
    citation_rows = []
    for idx, cause in enumerate(state.get("causes", []), 1):
        for evidence_id in cause.get("evidence_ids", []):
            citation_rows.append({"cause_id": idx, "cause_text": cause.get("text", ""), "evidence_id": evidence_id})
    write_rows_csv(case_root / "04_judge" / "citation_chain.csv", citation_rows, columns=["cause_id", "cause_text", "evidence_id"])
    write_markdown(case_root / "04_judge" / "judge_slide_extract.md", state.get("summary_md", ""))

    if validator_call:
        write_markdown(case_root / "05_validator_finalizer" / "validator_prompt.md", validator_call.get("prompt", ""))
        write_json(case_root / "05_validator_finalizer" / "validator_raw_response.json", {"available": True, "raw_response": validator_call.get("raw_response")})
    else:
        write_markdown(case_root / "05_validator_finalizer" / "validator_prompt.md", "")
        write_json(case_root / "05_validator_finalizer" / "validator_raw_response.json", {"available": False, "reason": "validator did not invoke the recording LLM in this run"})
    write_json(case_root / "05_validator_finalizer" / "validator_result.json", {"validation_error": state.get("validation_error"), "grounding_rate": state.get("grounding_rate")})
    write_markdown(case_root / "05_validator_finalizer" / "final_output.md", state.get("summary_md", ""))
    write_json(case_root / "05_validator_finalizer" / "final_status.json", {"output_status": _status_value(state.get("output_status")), "total_cost_usd": state.get("total_cost_usd"), "total_tokens": state.get("total_tokens")})

    summary = {
        "case": f"{case['ticker']} / {case['trade_date']}",
        "data_core": {"evidence_pool_total": len(evidence_pool), "source_counts": counts},
        "miner": {"retrieved": len(retrieved), "reranked": len(reranked)},
        "critic": {"graded": len(state.get("all_graded_chunks", [])), "kept": len(state.get("graded_evidence", []))},
        "judge": {"causes": len(state.get("causes", [])), "grounding_rate": state.get("grounding_rate")},
        "validator_finalizer": {"status": _status_value(state.get("output_status")), "validation_error": state.get("validation_error")},
    }
    write_json(case_root / "06_ppt_extracts" / "full_pipeline_summary.json", summary)
    write_markdown(case_root / "06_ppt_extracts" / "full_pipeline_table.md", _pipeline_table(summary))
    write_rows_csv(case_root / "06_ppt_extracts" / "node_timeline.csv", state.get("cost_breakdown", []), columns=["node", "model_id", "input_tokens", "output_tokens", "cost_usd"])
    write_markdown(case_root / "06_ppt_extracts" / "ppt_ready_quotes.md", _ppt_quotes(case, state))
    write_markdown(case_root / "06_ppt_extracts" / "ppt_case_report.md", _ppt_case_report(case, summary, state))
    return case_root
```

Also add simple Markdown formatters:

```python
def _pipeline_table(summary: dict[str, Any]) -> str:
    return (
        "| Node | Key output |\n"
        "|---|---|\n"
        f"| Data-core | {summary['data_core']['evidence_pool_total']} evidence items |\n"
        f"| Miner | {summary['miner']['retrieved']} retrieved → {summary['miner']['reranked']} reranked |\n"
        f"| Critic | {summary['critic']['graded']} graded → {summary['critic']['kept']} kept |\n"
        f"| Judge | {summary['judge']['causes']} cause(s), grounding={summary['judge']['grounding_rate']} |\n"
        f"| Finalizer | {summary['validator_finalizer']['status']} |\n"
    )


def _ppt_quotes(case: dict[str, Any], state: dict[str, Any]) -> str:
    return (
        f"# PPT-ready quotes: {case['ticker']} / {case['trade_date']}\n\n"
        f"## Critic\n\n{state.get('critic_reasoning', '')}\n\n"
        f"## Judge summary\n\n{state.get('summary_md', '')}\n"
    )


def _ppt_case_report(case: dict[str, Any], summary: dict[str, Any], state: dict[str, Any]) -> str:
    return (
        f"# PPT Case Report: {case['ticker']} / {case['trade_date']}\n\n"
        f"## Pipeline\n\n{_pipeline_table(summary)}\n\n"
        f"## Final Summary\n\n{state.get('summary_md', '')}\n"
    )
```

**Step 4: Run test to verify it passes**

Run:

```bash
PYTHONPATH=.:packages/agents:packages/data-core pytest packages/agents/tests/test_ppt_case_exporter_run_case.py -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add scripts/export_ppt_case_trace.py packages/agents/tests/test_ppt_case_exporter_run_case.py
git commit -m "feat: export ppt case artifacts"
```

---

## Task 8: Add Graph Runner Integration

**Files:**
- Modify: `scripts/export_ppt_case_trace.py`
- Test: `packages/agents/tests/test_ppt_case_exporter_integration.py`

**Step 1: Write the failing test**

Create `packages/agents/tests/test_ppt_case_exporter_integration.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

from scripts.export_ppt_case_trace import run_case


class Usage:
    input_tokens = 10
    output_tokens = 5
    total_tokens = 15


class Response:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = Usage()


class LLM:
    def __init__(self) -> None:
        self.calls = 0
        self.responses = [
            json.dumps({
                "graded_chunks": [{"chunk_id": "c1", "relevance": 0.9, "category": "sector", "temporal_match": True, "reasoning": "direct"}],
                "reasoning": "sufficient direct evidence"
            }),
            json.dumps({
                "causes": [{"text": "AI partnership", "category": "sector", "confidence": 0.9, "evidence_ids": ["c1"], "direction": "positive"}],
                "summary_md": "NVDA rose due to [c1].",
                "self_grounding_check": {"total_claims": 1, "grounded_claims": 1, "ungrounded_claims": 0}
            }),
        ]

    def invoke(self, prompt: str):
        response = Response(self.responses[min(self.calls, len(self.responses) - 1)])
        self.calls += 1
        return response


def test_run_case_with_mocked_retrieval_writes_case_dir(tmp_path: Path, monkeypatch):
    chunks = [{
        "asset_id": "c1",
        "ticker": "NVDA",
        "source_type": "polygon_news",
        "reference_date": "2025-10-28",
        "content_md": "NVDA rose after AI partnership news.",
        "rrf_score": 1.0,
    }]

    def fake_retrieve(query, layer, metadata, *, rerank=None):
        return chunks

    monkeypatch.setattr("catalyst_agents.nodes.miner.retrieve", fake_retrieve)
    monkeypatch.setattr("scripts.export_ppt_case_trace.collect_evidence_pool", lambda db_path, case: chunks)

    case = {"id": "g013", "ticker": "NVDA", "trade_date": "2025-10-28", "price_move_pct": 4.98, "expected_status": "SUFFICIENT", "label": "AI"}
    case_root = run_case(case, out_root=tmp_path, frozen_db=tmp_path / "x.db", lancedb_dir=tmp_path / "lancedb", trace_db=tmp_path / "trace.db", llm=LLM())

    assert case_root.name == "g013_NVDA_2025-10-28"
    assert (case_root / "03_critic" / "critic_raw_response.json").exists()
    assert (case_root / "04_judge" / "judge_raw_response.json").exists()
```

**Step 2: Run test to verify it fails**

Run:

```bash
PYTHONPATH=.:packages/agents:packages/data-core pytest packages/agents/tests/test_ppt_case_exporter_integration.py -v
```

Expected: FAIL because `run_case` is missing.

**Step 3: Implement runner**

Add:

```python
from contextlib import contextmanager
import os

from catalyst_agents.graph import build_attribution_graph
from catalyst_agents.retrieval.policy import Layer, RetrievalMetadata
from catalyst_agents.trace.exporter import export_run


@contextmanager
def trace_db_env(trace_db: Path):
    previous = os.environ.get("CATALYST_DB_PATH")
    os.environ["CATALYST_DB_PATH"] = str(trace_db)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("CATALYST_DB_PATH", None)
        else:
            os.environ["CATALYST_DB_PATH"] = previous


def initial_state_for_case(case: dict[str, Any], frozen_db: Path, lancedb_dir: Path) -> dict[str, Any]:
    return {
        "ticker": case["ticker"],
        "trade_date": case["trade_date"],
        "query": None,
        "price_move_pct": case["price_move_pct"],
        "retrieved_chunks": [],
        "reranked_chunks": [],
        "graded_evidence": [],
        "critic_reasoning": "",
        "critic_decision": None,
        "error_type": None,
        "causes": [],
        "summary_md": "",
        "grounding_rate": None,
        "output_status": None,
        "validation_error": None,
        "validator_attempts": 0,
        "phase": None,
        "router_edge": None,
        "router_reason": None,
        "expansions_used": 0,
        "max_expansions": 2,
        "current_layer": Layer.DIRECT,
        "retrieval_metadata": RetrievalMetadata(
            ticker=case["ticker"],
            trade_date=case["trade_date"],
            date_range=compute_date_range(case["trade_date"]),
            db_path=frozen_db,
            lancedb_dir=lancedb_dir,
        ),
        "cost_breakdown": [],
        "total_cost_usd": 0.0,
        "total_tokens": 0,
        "model_id": "claude-sonnet-4-20250514",
    }


def run_case(
    case: dict[str, Any],
    *,
    out_root: Path,
    frozen_db: Path,
    lancedb_dir: Path,
    trace_db: Path,
    llm: Any,
    table: Any = None,
    embedding_fn: Any = None,
    reranker: Any = None,
) -> Path:
    recorder = RawCallRecorder()
    graph = build_attribution_graph(
        use_critic=True,
        table=table,
        embedding_fn=embedding_fn,
        reranker=reranker,
        llm=llm,
    )
    state = initial_state_for_case(case, frozen_db, lancedb_dir)
    with trace_db_env(trace_db), capture_node_calls(recorder):
        result = graph.invoke(state)
    if result.get("run_id"):
        trace_dir = out_root / case_dir_name(case) / "06_ppt_extracts"
        trace_dir.mkdir(parents=True, exist_ok=True)
        export_run(result["run_id"], out_path=trace_dir / "trace_events.json", db_path=trace_db)
    evidence_pool = collect_evidence_pool(frozen_db, case)
    return export_case_artifacts(out_root, case, result, recorder.calls, evidence_pool)
```

**Step 4: Run test to verify it passes**

Run:

```bash
PYTHONPATH=.:packages/agents:packages/data-core pytest packages/agents/tests/test_ppt_case_exporter_integration.py -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add scripts/export_ppt_case_trace.py packages/agents/tests/test_ppt_case_exporter_integration.py
git commit -m "feat: run ppt case trace export"
```

---

## Task 9: Add Live LLM Factory Hook

**Files:**
- Modify: `scripts/export_ppt_case_trace.py`
- Test: `packages/agents/tests/test_ppt_case_exporter_llm_factory.py`

**Step 1: Write the failing test**

Create `packages/agents/tests/test_ppt_case_exporter_llm_factory.py`:

```python
from __future__ import annotations

import pytest

from scripts.export_ppt_case_trace import build_llm


def test_build_llm_rejects_missing_provider(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(SystemExit):
        build_llm("claude-sonnet-4-20250514")
```

**Step 2: Run test to verify it fails**

Run:

```bash
PYTHONPATH=.:packages/agents:packages/data-core pytest packages/agents/tests/test_ppt_case_exporter_llm_factory.py -v
```

Expected: FAIL because `build_llm` is missing.

**Step 3: Implement minimal factory**

Add:

```python
def build_llm(model_id: str) -> Any:
    if model_id.startswith("claude"):
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise SystemExit("ANTHROPIC_API_KEY is required for Claude models.")
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=model_id, temperature=0)
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is required for non-Claude models.")
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(model=model_id, temperature=0)
```

**Step 4: Run test to verify it passes**

Run:

```bash
PYTHONPATH=.:packages/agents:packages/data-core pytest packages/agents/tests/test_ppt_case_exporter_llm_factory.py -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add scripts/export_ppt_case_trace.py packages/agents/tests/test_ppt_case_exporter_llm_factory.py
git commit -m "feat: add llm factory for ppt exporter"
```

---

## Task 10: Add Main Entry Point And Dry Run

**Files:**
- Modify: `scripts/export_ppt_case_trace.py`
- Test: `packages/agents/tests/test_ppt_case_exporter_main.py`

**Step 1: Write the failing test**

Create `packages/agents/tests/test_ppt_case_exporter_main.py`:

```python
from __future__ import annotations

from scripts.export_ppt_case_trace import main


def test_main_dry_run_prints_selected_cases(capsys):
    exit_code = main(["--dry-run", "--case-id", "g013"])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "g013 NVDA 2025-10-28" in out
```

**Step 2: Run test to verify it fails**

Run:

```bash
PYTHONPATH=.:packages/agents:packages/data-core pytest packages/agents/tests/test_ppt_case_exporter_main.py -v
```

Expected: FAIL because `main` is missing.

**Step 3: Implement main**

Add:

```python
def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    cases = selected_cases(args.case_id)
    if args.dry_run:
        print("Selected PPT trace cases:")
        for case in cases:
            print(f"- {case['id']} {case['ticker']} {case['trade_date']} {case['price_move_pct']}% {case.get('label', '')}")
        return 0

    llm = build_llm(args.model_id)
    for case in cases:
        print(f"Running {case['id']} {case['ticker']} {case['trade_date']}...")
        case_root = run_case(
            case,
            out_root=args.out_dir,
            frozen_db=args.frozen_db,
            lancedb_dir=args.lancedb_dir,
            trace_db=args.trace_db,
            llm=llm,
        )
        print(f"  wrote {case_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

**Step 4: Run test to verify it passes**

Run:

```bash
PYTHONPATH=.:packages/agents:packages/data-core pytest packages/agents/tests/test_ppt_case_exporter_main.py -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add scripts/export_ppt_case_trace.py packages/agents/tests/test_ppt_case_exporter_main.py
git commit -m "feat: add ppt trace exporter entrypoint"
```

---

## Task 11: Run Focused Test Suite

**Files:**
- No code changes unless failures reveal issues.

**Step 1: Run new exporter tests**

Run:

```bash
PYTHONPATH=.:packages/agents:packages/data-core pytest \
  packages/agents/tests/test_recording_llm.py \
  packages/agents/tests/test_ppt_case_exporter_*.py \
  -v
```

Expected: PASS.

**Step 2: Run existing graph/trace tests**

Run:

```bash
PYTHONPATH=.:packages/agents:packages/data-core pytest \
  packages/agents/tests/test_graph.py \
  packages/agents/tests/test_trace.py \
  -v
```

Expected: PASS.

**Step 3: Fix only exporter-related regressions**

If existing graph/trace tests fail because of changed global monkeypatch state, fix `capture_node_calls` cleanup. Do not change production graph logic for PPT-only requirements.

**Step 4: Commit if fixes were needed**

```bash
git add scripts/export_ppt_case_trace.py packages/agents/tests
git commit -m "fix: stabilize ppt exporter tests"
```

---

## Task 12: Cloud Smoke Test Without LLM Calls

**Files:**
- No code changes unless dry run fails.

**Step 1: Run dry run on cloud**

Run:

```bash
PYTHONPATH=.:packages/agents:packages/data-core:packages/eval \
python scripts/export_ppt_case_trace.py --dry-run
```

Expected output includes:

```text
Selected PPT trace cases:
- g013 NVDA 2025-10-28 4.98% AI infrastructure / strategic partnerships
- g019 AMD 2025-10-06 23.71% OpenAI partnership / AI supply chain
- g029 GOOGL 2025-09-03 9.14% antitrust ruling / regulatory relief
- g042 AAPL 2025-04-03 -9.25% tariffs / supply-chain risk
- g025 UNH 2025-07-29 -7.46% earnings miss / guidance reset
```

**Step 2: Verify data files exist**

Run:

```bash
test -f data/catalyst_eval_frozen_v2.db
test -d data/lancedb_gold/eval_frozen
```

Expected: exit code 0.

**Step 3: Commit no changes**

No commit needed.

---

## Task 13: Cloud Full Run For Main Case

**Files:**
- Output only under `outputs/ppt_case_trace/`.

**Step 1: Run main NVDA case**

Run:

```bash
export ANTHROPIC_API_KEY="..."
PYTHONPATH=.:packages/agents:packages/data-core:packages/eval \
python scripts/export_ppt_case_trace.py \
  --case-id g013 \
  --out-dir outputs/ppt_case_trace \
  --trace-db outputs/ppt_case_trace/trace.db \
  --model-id claude-sonnet-4-20250514
```

Expected:

```text
Running g013 NVDA 2025-10-28...
  wrote outputs/ppt_case_trace/g013_NVDA_2025-10-28
```

**Step 2: Verify critical artifacts**

Run:

```bash
test -s outputs/ppt_case_trace/g013_NVDA_2025-10-28/03_critic/critic_raw_response.json
test -s outputs/ppt_case_trace/g013_NVDA_2025-10-28/04_judge/judge_raw_response.json
test -s outputs/ppt_case_trace/g013_NVDA_2025-10-28/06_ppt_extracts/full_pipeline_summary.json
python -m json.tool outputs/ppt_case_trace/g013_NVDA_2025-10-28/06_ppt_extracts/full_pipeline_summary.json
```

Expected: all commands succeed and JSON shows `validator_finalizer.status` as `SUFFICIENT`.

**Step 3: Inspect PPT report**

Run:

```bash
sed -n '1,220p' outputs/ppt_case_trace/g013_NVDA_2025-10-28/06_ppt_extracts/ppt_case_report.md
```

Expected: readable pipeline table and final summary.

---

## Task 14: Cloud Full Run For All Five Cases

**Files:**
- Output only under `outputs/ppt_case_trace/`.

**Step 1: Run all cases**

Run:

```bash
export ANTHROPIC_API_KEY="..."
PYTHONPATH=.:packages/agents:packages/data-core:packages/eval \
python scripts/export_ppt_case_trace.py \
  --out-dir outputs/ppt_case_trace \
  --trace-db outputs/ppt_case_trace/trace.db \
  --model-id claude-sonnet-4-20250514
```

Expected:

```text
Running g013 NVDA 2025-10-28...
Running g019 AMD 2025-10-06...
Running g029 GOOGL 2025-09-03...
Running g042 AAPL 2025-04-03...
Running g025 UNH 2025-07-29...
```

**Step 2: Verify every case produced the PPT core files**

Run:

```bash
for d in outputs/ppt_case_trace/g*_*/; do
  test -s "$d/00_case/case_meta.json" || exit 1
  test -s "$d/02_miner/reranked_chunks_top8.csv" || exit 1
  test -s "$d/03_critic/critic_raw_response.json" || exit 1
  test -s "$d/04_judge/judge_raw_response.json" || exit 1
  test -s "$d/06_ppt_extracts/full_pipeline_summary.json" || exit 1
  echo "ok $d"
done
```

Expected: one `ok` line per case.

**Step 3: Create archive for local PPT work**

Run:

```bash
tar -czf outputs/ppt_case_trace_$(date +%Y%m%d_%H%M%S).tar.gz outputs/ppt_case_trace
```

Expected: tarball created.

---

## Task 15: Decide PPT Material Scope

**Files:**
- No code changes.

**Step 1: Review main case materials**

Open:

```text
outputs/ppt_case_trace/g013_NVDA_2025-10-28/06_ppt_extracts/ppt_case_report.md
outputs/ppt_case_trace/g013_NVDA_2025-10-28/02_miner/reranked_chunks_top8.csv
outputs/ppt_case_trace/g013_NVDA_2025-10-28/03_critic/critic_slide_extract.md
outputs/ppt_case_trace/g013_NVDA_2025-10-28/04_judge/citation_chain.csv
```

**Step 2: Select PPT assets**

Use only these in slides unless a raw response is exceptionally clear:

- Case card from `00_case/case_meta.json`
- Data-core source counts from `01_data_core/source_counts.json`
- Miner Top-8 table from `02_miner/reranked_chunks_top8.csv`
- Miner funnel from `06_ppt_extracts/full_pipeline_summary.json`
- Critic decision from `03_critic/critic_decision.json`
- Judge citation chain from `04_judge/citation_chain.csv`
- Final status from `05_validator_finalizer/final_status.json`

**Step 3: Keep raw responses as backup**

Use:

```text
03_critic/critic_raw_response.json
04_judge/judge_raw_response.json
```

Only for appendix or cropped evidence panels. Do not put full raw responses into main slides.

---

## Notes For Cloud Setup

Recommended Python setup on the GPU instance:

```bash
conda create -n catalyst-ppt python=3.11 -y
conda activate catalyst-ppt
pip install -e packages/data-core -e packages/agents -e packages/eval
pip install pytest langchain-anthropic langchain-openai
```

Dry-run first:

```bash
PYTHONPATH=.:packages/agents:packages/data-core:packages/eval python scripts/export_ppt_case_trace.py --dry-run
```

Then run one case before all cases:

```bash
PYTHONPATH=.:packages/agents:packages/data-core:packages/eval python scripts/export_ppt_case_trace.py --case-id g013
```

---

## Risk Checklist

- If raw response files are empty, inspect whether `critic.py` or `judge.py` imports `invoke_with_retries` directly. The plan patches module-level imported symbols, which should work with the current code.
- If Validator has no raw response, this is acceptable; finalizer/status JSON is enough for PPT.
- If LanceDB is unavailable, retrieval falls back to SQLite. That is acceptable for script tests, but the cloud full run should use the frozen LanceDB index for the final PPT case.
- If the case output is `PARTIAL` or `INSUFFICIENT`, keep the artifacts but do not use that case in the main PPT. Re-run with the next candidate case.
- If raw prompts are too long, keep them in files but only use `critic_slide_extract.md`, `judge_slide_extract.md`, and `full_pipeline_summary.json` for slide production.

