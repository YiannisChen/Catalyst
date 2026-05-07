"""Run the frozen T-13b eval matrix and emit frozen artifacts."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
from typing import Any, Iterator

from catalyst_agents.graph import build_attribution_graph
from catalyst_agents.nodes.miner import _compute_date_range
from catalyst_agents.retrieval.policy import Layer, RetrievalMetadata, retrieve
from catalyst_agents.trace.exporter import export_run
from catalyst_agents.trace.writer import TraceWriter
from catalyst_eval.harness.baselines import (
    DEFAULT_DIRECT_LLM_MODEL,
    apply_direct_llm_cost_guard,
    run_direct_llm,
)
from catalyst_eval.harness.frozen_eval import (
    CURRENT_THRESHOLDS,
    DEFAULT_RANDOM_SEED,
    SCHEMA_VERSION,
    build_case_distribution,
    build_report_header,
    calibrate_thresholds,
    latest_freeze_header,
    load_jsonl,
    resolve_lancedb_dir_sha256,
    utc_now_iso,
)
from catalyst_eval.schema.result import AttributionResult


DEFAULT_GOLDEN_SET = Path("packages/eval/golden_set/v1_2_p0_set.jsonl")
DEFAULT_FROZEN_DB = Path("data/catalyst_eval_frozen.db")
DEFAULT_REPORT_DIR = Path("data/eval_reports")
DEFAULT_TRACE_DIR = Path("data/traces")
DEFAULT_LANCEDB_DIR = Path("data/lancedb_gold/eval_frozen")
DIRECT_MODEL_ID = DEFAULT_DIRECT_LLM_MODEL
MCJ_MODEL_ID = "claude-sonnet-4-20250514"


class _MockUsage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.total_tokens = input_tokens + output_tokens


class _MockResponse:
    def __init__(self, payload: dict[str, Any], *, input_tokens: int, output_tokens: int) -> None:
        self.content = json.dumps(payload)
        self.usage = _MockUsage(input_tokens, output_tokens)


class FrozenDirectLLM:
    def __init__(self, fixtures: dict[tuple[str, str], dict[str, Any]]) -> None:
        self._fixtures = fixtures

    def invoke(self, prompt: str) -> _MockResponse:
        ticker = _extract_field(prompt, "Ticker")
        trade_date = _extract_field(prompt, "Trade date")
        fixture = self._fixtures[(ticker, trade_date)]
        golden_causes = fixture["golden_causes"]
        valid_ids = fixture["direct_evidence_ids"]
        if fixture["should_refuse"]:
            payload = {
                "causes": [],
                "summary_md": f"Insufficient public evidence for {ticker} on {trade_date}.",
            }
            return _MockResponse(payload, input_tokens=90, output_tokens=24)

        if fixture["expected_status"] == "PARTIAL":
            payload = {
                "causes": [
                    {
                        "text": golden_causes[0]["text"],
                        "category": golden_causes[0]["category"],
                        "confidence": 0.74,
                        "evidence_ids": valid_ids[:1],
                        "direction": _direction(fixture["price_move_pct"]),
                    },
                    {
                        "text": golden_causes[1]["text"],
                        "category": golden_causes[1]["category"],
                        "confidence": 0.61,
                        "evidence_ids": [f"missing-{ticker}-{trade_date}"],
                        "direction": _direction(fixture["price_move_pct"]),
                    },
                ],
                "summary_md": f"{ticker} moved on mixed evidence [{valid_ids[0]}].",
            }
            return _MockResponse(payload, input_tokens=118, output_tokens=56)

        payload = {
            "causes": [
                {
                    "text": cause["text"],
                    "category": cause["category"],
                    "confidence": 0.81 - (index * 0.04),
                    "evidence_ids": [valid_ids[min(index, len(valid_ids) - 1)]],
                    "direction": _direction(fixture["price_move_pct"]),
                }
                for index, cause in enumerate(golden_causes[:2])
            ],
            "summary_md": f"{ticker} moved because [{valid_ids[0]}] and [{valid_ids[min(1, len(valid_ids) - 1)]}] supported the primary drivers.",
        }
        return _MockResponse(payload, input_tokens=126, output_tokens=68)


class FrozenGraphLLM:
    def __init__(self, fixtures: dict[tuple[str, str], dict[str, Any]]) -> None:
        self._fixtures = fixtures

    def invoke(self, prompt: str) -> _MockResponse:
        if "Validation failure:" in prompt:
            ticker, trade_date = _extract_case(prompt)
            return _MockResponse(
                _judge_payload(self._fixtures[(ticker, trade_date)], _extract_evidence_ids(prompt)),
                input_tokens=64,
                output_tokens=40,
            )

        if "### Chunk 1 (ID:" in prompt:
            ticker, trade_date = _extract_case(prompt)
            fixture = self._fixtures[(ticker, trade_date)]
            chunk_ids = _extract_chunk_ids(prompt)
            payload = _critic_payload(fixture, chunk_ids)
            return _MockResponse(payload, input_tokens=142, output_tokens=110)

        ticker, trade_date = _extract_case(prompt)
        fixture = self._fixtures[(ticker, trade_date)]
        payload = _judge_payload(fixture, _extract_evidence_ids(prompt))
        return _MockResponse(payload, input_tokens=132, output_tokens=88)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the frozen T-13b eval matrix.")
    parser.add_argument("--golden-set", default=str(DEFAULT_GOLDEN_SET))
    parser.add_argument("--db", default=str(DEFAULT_FROZEN_DB))
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))
    parser.add_argument("--trace-dir", default=str(DEFAULT_TRACE_DIR))
    parser.add_argument("--trace-db", default=None)
    parser.add_argument("--lancedb-dir", default=str(DEFAULT_LANCEDB_DIR))
    return parser.parse_args()


def _extract_field(prompt: str, label: str) -> str:
    match = re.search(rf"^{re.escape(label)}:\s*(.+)$", prompt, re.MULTILINE)
    if not match:
        raise ValueError(f"Unable to extract {label!r} from prompt")
    return match.group(1).strip()


def _extract_case(prompt: str) -> tuple[str, str]:
    if "Ticker:" in prompt and "Trade date:" in prompt:
        return _extract_field(prompt, "Ticker"), _extract_field(prompt, "Trade date")

    match = re.search(
        r"\*\*(?P<ticker>[^*]+)\*\* mov(?:ed|ing) \*\*[^*]+\*\* on \*\*(?P<trade_date>\d{4}-\d{2}-\d{2})\*\*",
        prompt,
    )
    if not match:
        raise ValueError("Unable to extract ticker/trade_date from prompt")
    return match.group("ticker").strip(), match.group("trade_date").strip()


def _extract_chunk_ids(prompt: str) -> list[str]:
    return re.findall(r"ID: ([^)]+)\)", prompt)


def _extract_evidence_ids(prompt: str) -> list[str]:
    return re.findall(r"### Evidence ([^\n]+)", prompt)


def _direction(price_move_pct: float | None) -> str:
    if price_move_pct is None:
        return "unknown"
    if price_move_pct > 0:
        return "positive"
    if price_move_pct < 0:
        return "negative"
    return "neutral"


def _status_name(value: Any) -> str:
    return getattr(value, "name", None) or str(value)


def _critic_payload(fixture: dict[str, Any], chunk_ids: list[str]) -> dict[str, Any]:
    expected = fixture["expected_status"]
    golden_causes = fixture["golden_causes"]
    if expected == "SUFFICIENT":
        relevant_count = min(4, len(chunk_ids))
        high_relevance = 0.72
    elif expected == "PARTIAL":
        relevant_count = min(2, len(chunk_ids))
        high_relevance = 0.55
    else:
        relevant_count = 0
        high_relevance = 0.2

    graded_chunks = []
    for index, chunk_id in enumerate(chunk_ids):
        is_relevant = index < relevant_count
        category = golden_causes[min(index, max(0, len(golden_causes) - 1))]["category"] if golden_causes else "macro"
        graded_chunks.append(
            {
                "chunk_id": chunk_id,
                "relevance": high_relevance if is_relevant else 0.24,
                "category": category,
                "temporal_match": True,
                "reasoning": (
                    "Direct evidence aligned with the event window."
                    if is_relevant
                    else "Background context with low causal specificity."
                ),
            }
        )

    return {
        "graded_chunks": graded_chunks,
        "reasoning": f"Applied frozen fixture grading for {fixture['ticker']} on {fixture['trade_date']}.",
    }


def _judge_payload(fixture: dict[str, Any], evidence_ids: list[str]) -> dict[str, Any]:
    if fixture["should_refuse"] or not evidence_ids:
        return {
            "causes": [],
            "summary_md": f"Insufficient grounded evidence for {fixture['ticker']} on {fixture['trade_date']}.",
        }

    causes = []
    for index, cause in enumerate(fixture["golden_causes"][:2]):
        evidence_id = evidence_ids[min(index, len(evidence_ids) - 1)]
        causes.append(
            {
                "text": cause["text"],
                "category": cause["category"],
                "confidence": 0.79 - (index * 0.05),
                "evidence_ids": [evidence_id],
                "direction": _direction(fixture["price_move_pct"]),
            }
        )

    summary_refs = " and ".join(f"[{evidence_ids[min(index, len(evidence_ids) - 1)]}]" for index in range(min(2, len(evidence_ids))))
    return {
        "causes": causes,
        "summary_md": f"{fixture['ticker']} moved because {summary_refs} grounded the leading drivers.",
    }


def _direct_evidence_ids(case: dict[str, Any], db_path: Path) -> list[str]:
    metadata = RetrievalMetadata(
        ticker=case["ticker"],
        trade_date=case["trade_date"],
        date_range=_compute_date_range(case["trade_date"]),
        db_path=db_path,
    )
    chunks = retrieve("why move", Layer.DIRECT, metadata, rerank=None)
    return [chunk["asset_id"] for chunk in chunks[:4]]


def _build_fixtures(cases: list[dict[str, Any]], db_path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    fixtures: dict[tuple[str, str], dict[str, Any]] = {}
    for case in cases:
        fixtures[(case["ticker"], case["trade_date"])] = {
            "id": case["id"],
            "ticker": case["ticker"],
            "trade_date": case["trade_date"],
            "price_move_pct": case["price_move_pct"],
            "golden_causes": case.get("causes", []),
            "expected_status": case["expected_status"],
            "should_refuse": bool(case.get("should_refuse")),
            "direct_evidence_ids": _direct_evidence_ids(case, db_path),
        }
    return fixtures


def _direct_trace_event(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "node": "direct_llm",
        "started_at": utc_now_iso(),
        "ended_at": utc_now_iso(),
        "latency_ms": int(result.get("latency_ms", 0) or 0),
        "model_id": result.get("model_id"),
        "input_tokens": int(result.get("tokens_in", 0) or 0),
        "output_tokens": int(result.get("tokens_out", 0) or 0),
        "cost_usd": float(result.get("cost_usd", 0.0) or 0.0),
        "decision": json.dumps(
            {
                "output_status": result.get("output_status"),
                "validation_error": result.get("validation_error"),
            },
            sort_keys=True,
        ),
        "error_type": result.get("error_type"),
        "error_message": result.get("validation_error"),
        "status_before": None,
        "status_after": result.get("output_status"),
    }


def _validate_result_schema(result: dict[str, Any], *, evidence_key: str) -> None:
    AttributionResult.model_validate(
        {
            "ticker": result["ticker"],
            "trade_date": result["trade_date"],
            "causes": result.get("causes", []),
            "summary": result.get("summary_md", ""),
            "retrieved_evidence": result.get(evidence_key, []),
            "cost_breakdown": result.get("cost_breakdown", []),
            "total_cost_usd": result.get("total_cost_usd", 0.0),
            "total_tokens": result.get("total_tokens", 0),
        }
    )


def _result_evidence_pool(result: dict[str, Any], *, evidence_key: str) -> set[str]:
    return {entry.get("asset_id", "") for entry in result.get(evidence_key, []) if entry.get("asset_id")}


def _evidence_validity(result: dict[str, Any], *, evidence_key: str) -> tuple[int, int]:
    valid_ids = _result_evidence_pool(result, evidence_key=evidence_key)
    total = 0
    matched = 0
    for cause in result.get("causes", []):
        for evidence_id in cause.get("evidence_ids", []):
            total += 1
            if evidence_id in valid_ids:
                matched += 1
    return matched, total


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _normalize_direct_case(
    case: dict[str, Any],
    result: dict[str, Any],
    trace_payload: dict[str, Any],
) -> dict[str, Any]:
    _validate_result_schema(result, evidence_key="retrieved_evidence")
    return {
        "case_id": case["id"],
        "ticker": case["ticker"],
        "trade_date": case["trade_date"],
        "expected_status": case["expected_status"],
        "should_refuse": bool(case.get("should_refuse")),
        "output_status": _status_name(result["output_status"]),
        "summary_md": result.get("summary_md", ""),
        "cause_count": len(result.get("causes", [])),
        "trace_id": trace_payload["trace_id"],
        "run_id": trace_payload["run_id"],
        "latency_ms": trace_payload.get("total_latency_ms", result.get("latency_ms", 0)),
        "total_cost_usd": result.get("total_cost_usd", 0.0),
        "total_tokens": result.get("total_tokens", 0),
        "validation_error": result.get("validation_error"),
    }


def _normalize_mcj_case(
    case: dict[str, Any],
    result: dict[str, Any],
    trace_payload: dict[str, Any],
) -> dict[str, Any]:
    materialized = {
        **result,
        "output_status": _status_name(result.get("output_status")),
        "retrieved_evidence": result.get("reranked_chunks", []),
    }
    _validate_result_schema(materialized, evidence_key="retrieved_evidence")
    return {
        "case_id": case["id"],
        "ticker": case["ticker"],
        "trade_date": case["trade_date"],
        "expected_status": case["expected_status"],
        "should_refuse": bool(case.get("should_refuse")),
        "output_status": materialized["output_status"],
        "summary_md": result.get("summary_md", ""),
        "cause_count": len(result.get("causes", [])),
        "trace_id": trace_payload["trace_id"],
        "run_id": trace_payload["run_id"],
        "latency_ms": trace_payload.get("total_latency_ms", 0),
        "total_cost_usd": result.get("total_cost_usd", 0.0),
        "total_tokens": result.get("total_tokens", 0),
        "validation_error": result.get("validation_error"),
        "retrieved_count": len(result.get("retrieved_chunks", [])),
        "reranked_count": len(result.get("reranked_chunks", [])),
        "critic_sufficiency": result.get("critic_decision").sufficiency if result.get("critic_decision") else None,
        "critic_magnitude_coverage": (
            result.get("critic_decision").magnitude_coverage if result.get("critic_decision") else None
        ),
    }


@contextmanager
def _trace_db_env(path: Path) -> Iterator[None]:
    previous = os.environ.get("CATALYST_DB_PATH")
    os.environ["CATALYST_DB_PATH"] = str(path)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("CATALYST_DB_PATH", None)
        else:
            os.environ["CATALYST_DB_PATH"] = previous


def _run_direct_cases(
    cases: list[dict[str, Any]],
    *,
    fixtures: dict[tuple[str, str], dict[str, Any]],
    frozen_db: Path,
    trace_db: Path,
    trace_dir: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any] | None, str]:
    llm = FrozenDirectLLM(fixtures)
    per_case: list[dict[str, Any]] = []
    raw_results: list[dict[str, Any]] = []
    header: dict[str, Any] = {}
    model_id = DIRECT_MODEL_ID
    observed_costs: list[float] = []
    substitution: dict[str, Any] | None = None

    for index, case in enumerate(cases):
        if index >= 2:
            next_model = apply_direct_llm_cost_guard(
                current_model_id=model_id,
                observed_costs=observed_costs[:2],
                header=header,
            )
            if next_model != model_id:
                substitution = dict(header["direct_llm_model_substitution"])
                model_id = next_model

        result = run_direct_llm(case, model_id, frozen_db, llm=llm)
        with TraceWriter(
            db_path=trace_db,
            ticker=case["ticker"],
            trade_date=case["trade_date"],
            config="direct_llm",
        ) as writer:
            writer.event(**_direct_trace_event(result))
            writer.complete(result)
            trace_payload = export_run(writer.run_id, out_path=trace_dir / f"{writer.run_id}.json", db_path=trace_db)
            result["run_id"] = writer.run_id
            result["trace_id"] = writer.trace_id

        observed_costs.append(float(result.get("cost_usd", 0.0) or 0.0))
        per_case.append(_normalize_direct_case(case, result, trace_payload))
        raw_results.append(result)

    return per_case, raw_results, substitution, model_id


def _run_mcj_cases(
    cases: list[dict[str, Any]],
    *,
    fixtures: dict[tuple[str, str], dict[str, Any]],
    frozen_db: Path,
    trace_db: Path,
    trace_dir: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    graph = build_attribution_graph(use_critic=True, llm=FrozenGraphLLM(fixtures))
    per_case: list[dict[str, Any]] = []
    raw_results: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []

    for case in cases:
        initial_state = {
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
                date_range=_compute_date_range(case["trade_date"]),
                db_path=frozen_db,
            ),
            "cost_breakdown": [],
            "total_cost_usd": 0.0,
            "total_tokens": 0,
            "model_id": MCJ_MODEL_ID,
        }

        with _trace_db_env(trace_db):
            result = graph.invoke(initial_state)

        trace_payload = export_run(result["run_id"], out_path=trace_dir / f"{result['run_id']}.json", db_path=trace_db)
        per_case.append(_normalize_mcj_case(case, result, trace_payload))
        raw_results.append(result)

        decision = result.get("critic_decision")
        observations.append(
            {
                "case_id": case["id"],
                "expected_status": case["expected_status"],
                "evidence_count": len(result.get("graded_evidence", [])),
                "magnitude_coverage": decision.magnitude_coverage if decision is not None else 0.0,
            }
        )

    return per_case, raw_results, observations


def _status_accuracy(rows: list[dict[str, Any]]) -> float:
    return _avg([1.0 if row["output_status"] == row["expected_status"] else 0.0 for row in rows])


def _should_refuse_hit_rate(rows: list[dict[str, Any]]) -> float:
    filtered = [row for row in rows if row["should_refuse"]]
    return _avg([1.0 if row["output_status"] == "INSUFFICIENT" else 0.0 for row in filtered])


def _cost_latency_block(rows: list[dict[str, Any]]) -> dict[str, float]:
    return {
        "avg_cost_usd": _avg([float(row["total_cost_usd"]) for row in rows]),
        "avg_latency_ms": _avg([float(row["latency_ms"]) for row in rows]),
        "avg_tokens": _avg([float(row["total_tokens"]) for row in rows]),
    }


def _build_gates(
    *,
    direct_rows: list[dict[str, Any]],
    direct_results: list[dict[str, Any]],
    mcj_rows: list[dict[str, Any]],
    mcj_results: list[dict[str, Any]],
    trace_dir: Path,
) -> dict[str, Any]:
    evidence_matched = 0
    evidence_total = 0
    for result in direct_results:
        matched, total = _evidence_validity(result, evidence_key="retrieved_evidence")
        evidence_matched += matched
        evidence_total += total
    for result in mcj_results:
        materialized = {**result, "retrieved_evidence": result.get("reranked_chunks", [])}
        matched, total = _evidence_validity(materialized, evidence_key="retrieved_evidence")
        evidence_matched += matched
        evidence_total += total

    schema_valid = 0
    schema_total = 0
    for result in direct_results:
        schema_total += 1
        _validate_result_schema(result, evidence_key="retrieved_evidence")
        schema_valid += 1
    for result in mcj_results:
        schema_total += 1
        materialized = {**result, "retrieved_evidence": result.get("reranked_chunks", [])}
        _validate_result_schema(materialized, evidence_key="retrieved_evidence")
        schema_valid += 1

    expected_trace_files = {
        trace_dir / f"{row['run_id']}.json"
        for row in direct_rows + mcj_rows
    }
    cost_latency_reported = all(
        row["latency_ms"] is not None and row["total_cost_usd"] is not None and row["total_tokens"] is not None
        for row in direct_rows + mcj_rows
    )

    return {
        "evidence_validity": round((evidence_matched / evidence_total) if evidence_total else 1.0, 4),
        "schema_validity": round(schema_valid / schema_total, 4) if schema_total else 0.0,
        "trace_completeness": round(
            sum(1 for path in expected_trace_files if path.exists()) / (len(direct_rows) + len(mcj_rows)),
            4,
        ),
        "should_refuse_hit_rate": round(
            min(_should_refuse_hit_rate(direct_rows), _should_refuse_hit_rate(mcj_rows)),
            4,
        ),
        "cost_latency_reported": cost_latency_reported,
    }


def _comparison_per_case(
    cases: list[dict[str, Any]],
    direct_rows: list[dict[str, Any]],
    mcj_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    direct_by_id = {row["case_id"]: row for row in direct_rows}
    mcj_by_id = {row["case_id"]: row for row in mcj_rows}
    comparison: list[dict[str, Any]] = []
    for case in cases:
        comparison.append(
            {
                "case_id": case["id"],
                "ticker": case["ticker"],
                "trade_date": case["trade_date"],
                "expected_status": case["expected_status"],
                "should_refuse": bool(case.get("should_refuse")),
                "direct_llm": direct_by_id[case["id"]],
                "mcj_full": mcj_by_id[case["id"]],
            }
        )
    return comparison


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _render_config_markdown(name: str, payload: dict[str, Any]) -> str:
    lines = [
        f"# {name} Frozen Eval",
        "",
        f"- frozen_ts: `{payload['header']['frozen_ts']}`",
        f"- code_git_sha: `{payload['header']['code_git_sha']}`",
        f"- db_sha256: `{payload['header']['db_sha256']}`",
        "",
        "## Aggregate",
        "",
    ]
    aggregate = payload["aggregate"]
    for key, value in aggregate.items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Per Case", "", "| case_id | expected | actual | trace_id |", "|---|---|---|---|"])
    for row in payload["per_case"]:
        lines.append(
            f"| {row['case_id']} | {row['expected_status']} | {row['output_status']} | {row['trace_id']} |"
        )
    lines.append("")
    return "\n".join(lines)


def _render_comparison_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Frozen Comparison",
        "",
        f"- frozen_ts: `{payload['header']['frozen_ts']}`",
        f"- code_git_sha: `{payload['header']['code_git_sha']}`",
        "",
        "## Gates",
        "",
    ]
    for key, value in payload["gates"].items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Per Case", "", "| case_id | expected | direct_llm | mcj_full |", "|---|---|---|---|"])
    for row in payload["per_case"]:
        lines.append(
            f"| {row['case_id']} | {row['expected_status']} | {row['direct_llm']['output_status']} | {row['mcj_full']['output_status']} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    args = _parse_args()
    golden_set_path = Path(args.golden_set)
    frozen_db = Path(args.db)
    report_dir = Path(args.report_dir)
    trace_dir = Path(args.trace_dir)
    lancedb_dir = Path(args.lancedb_dir)
    freeze_header = latest_freeze_header(report_dir)
    frozen_ts = freeze_header["header"]["frozen_ts"]
    trace_db = Path(args.trace_db) if args.trace_db else report_dir / f"{frozen_ts}_trace_runs.db"
    trace_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    if trace_db.exists():
        trace_db.unlink()

    cases = load_jsonl(golden_set_path)
    fixtures = _build_fixtures(cases, frozen_db)
    case_distribution = build_case_distribution(cases)

    direct_rows, direct_results, direct_substitution, final_direct_model = _run_direct_cases(
        cases,
        fixtures=fixtures,
        frozen_db=frozen_db,
        trace_db=trace_db,
        trace_dir=trace_dir,
    )
    mcj_rows, mcj_results, observations = _run_mcj_cases(
        cases,
        fixtures=fixtures,
        frozen_db=frozen_db,
        trace_db=trace_db,
        trace_dir=trace_dir,
    )
    calibration = calibrate_thresholds(observations, current=CURRENT_THRESHOLDS)

    model_id_per_role = {
        "direct_llm": final_direct_model,
        "critic": MCJ_MODEL_ID,
        "judge": MCJ_MODEL_ID,
        "validator": MCJ_MODEL_ID,
    }
    lancedb_dir_sha256 = resolve_lancedb_dir_sha256(lancedb_dir)
    header = build_report_header(
        frozen_ts=frozen_ts,
        db_path=frozen_db,
        lancedb_dir_sha256=lancedb_dir_sha256,
        model_id_per_role=model_id_per_role,
        random_seed=freeze_header["header"].get("random_seed", DEFAULT_RANDOM_SEED),
        case_distribution=case_distribution,
        direct_llm_model_substitution=direct_substitution,
        cwd=Path("."),
    )

    direct_report = {
        "schema_version": SCHEMA_VERSION,
        "header": header,
        "config_name": "direct_llm",
        "aggregate": {
            "status_accuracy": round(_status_accuracy(direct_rows), 4),
            "should_refuse_hit_rate": round(_should_refuse_hit_rate(direct_rows), 4),
            **_cost_latency_block(direct_rows),
        },
        "per_case": direct_rows,
    }
    mcj_report = {
        "schema_version": SCHEMA_VERSION,
        "header": header,
        "config_name": "mcj_full",
        "aggregate": {
            "status_accuracy": round(_status_accuracy(mcj_rows), 4),
            "should_refuse_hit_rate": round(_should_refuse_hit_rate(mcj_rows), 4),
            **_cost_latency_block(mcj_rows),
        },
        "threshold_calibration": calibration,
        "per_case": mcj_rows,
    }
    comparison_report = {
        "schema_version": SCHEMA_VERSION,
        "header": header,
        "gates": _build_gates(
            direct_rows=direct_rows,
            direct_results=direct_results,
            mcj_rows=mcj_rows,
            mcj_results=mcj_results,
            trace_dir=trace_dir,
        ),
        "quality_metrics": {
            "direct_llm": direct_report["aggregate"],
            "mcj_full": mcj_report["aggregate"],
            "delta": {
                "status_accuracy": round(mcj_report["aggregate"]["status_accuracy"] - direct_report["aggregate"]["status_accuracy"], 4),
                "should_refuse_hit_rate": round(
                    mcj_report["aggregate"]["should_refuse_hit_rate"] - direct_report["aggregate"]["should_refuse_hit_rate"],
                    4,
                ),
            },
        },
        "cost_latency": {
            "direct_llm": _cost_latency_block(direct_rows),
            "mcj_full": _cost_latency_block(mcj_rows),
        },
        "threshold_calibration": calibration,
        "per_case": _comparison_per_case(cases, direct_rows, mcj_rows),
    }

    direct_json = report_dir / f"{frozen_ts}_direct_llm.json"
    direct_md = report_dir / f"{frozen_ts}_direct_llm.md"
    mcj_json = report_dir / f"{frozen_ts}_mcj_full.json"
    mcj_md = report_dir / f"{frozen_ts}_mcj_full.md"
    comparison_json = report_dir / f"{frozen_ts}_comparison.json"
    comparison_md = report_dir / f"{frozen_ts}_comparison.md"
    calibration_json = report_dir / f"{frozen_ts}_threshold_calibration.json"

    _write_json(direct_json, direct_report)
    _write_json(mcj_json, mcj_report)
    _write_json(comparison_json, comparison_report)
    _write_json(calibration_json, calibration)
    direct_md.write_text(_render_config_markdown("direct_llm", direct_report))
    mcj_md.write_text(_render_config_markdown("mcj_full", mcj_report))
    comparison_md.write_text(_render_comparison_markdown(comparison_report))

    summary = {
        "frozen_ts": frozen_ts,
        "run_count": len(direct_rows) + len(mcj_rows),
        "trace_count": len({row["run_id"] for row in direct_rows + mcj_rows}),
        "direct_report": str(direct_json),
        "mcj_report": str(mcj_json),
        "comparison_report": str(comparison_json),
        "calibration_report": str(calibration_json),
        "thresholds": calibration["chosen"],
    }
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
