#!/usr/bin/env python3
"""P1 single-case trace report runner.

Runs one golden-set case on the latest P1 stack:
  - Frozen DB: data/catalyst_eval_frozen_v2.db
  - LanceDB:   data/lancedb_gold/eval_frozen (L1+L2)
  - Agent:     Miner -> Critic -> Judge graph with trace persistence

Outputs:
  1) trace JSON (raw run + node events)
  2) summary JSON (structured audit metrics)
  3) markdown report (human-readable)
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, Iterator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "packages" / "data-core"))
sys.path.insert(0, str(PROJECT_ROOT / "packages" / "agents"))
sys.path.insert(0, str(PROJECT_ROOT / "packages" / "eval"))

from dotenv import load_dotenv

from catalyst_agents.graph import build_attribution_graph
from catalyst_agents.nodes.miner import _compute_date_range
from catalyst_agents.retrieval.policy import Layer, RetrievalMetadata
from catalyst_agents.trace.exporter import export_run
from catalyst_agents.cost_tracker import MODEL_PRICING
from catalyst_eval.harness.frozen_eval import resolve_lancedb_dir_sha256, sha256_file, utc_now_iso
from catalyst_eval.metrics.cause_match import CauseMatch
from catalyst_eval.metrics.category_accuracy import CategoryAccuracy
from catalyst_eval.metrics.confidence_calibration import ConfidenceCalibration
from catalyst_eval.metrics.citation_faithfulness import CitationFaithfulness
from catalyst_eval.metrics.refusal_correctness import RefusalCorrectness
from catalyst_eval.schema.golden_event import GoldenEvent
from catalyst_eval.schema.result import AttributionResult, PredictedCause, RetrievedEvidence


DEFAULT_GOLDEN_SET = PROJECT_ROOT / "packages" / "eval" / "golden_set" / "v1_2_p1_set.jsonl"
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "catalyst_eval_frozen_v2.db"
DEFAULT_LANCEDB_DIR = PROJECT_ROOT / "data" / "lancedb_gold" / "eval_frozen"
DEFAULT_OUT_DIR = PROJECT_ROOT / "data" / "eval_reports"
DEFAULT_TRACE_DIR = PROJECT_ROOT / "data" / "traces"
DEFAULT_PROVIDER = "aihubmix"
DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_BASE_URL = "https://aihubmix.com/v1"


def resolve_query_with_source(case: dict[str, Any], cli_query_override: str | None) -> tuple[str, str]:
    if cli_query_override is not None:
        return cli_query_override, "cli_override"
    if case.get("query_override"):
        return str(case["query_override"]), "case_override"
    return f"Why did {case['ticker']} move on {case['trade_date']}?", "default_template"


def resolve_query(case: dict[str, Any], cli_query_override: str | None) -> str:
    query, _ = resolve_query_with_source(case, cli_query_override)
    return query


@dataclass
class Usage:
    input_tokens: int
    output_tokens: int
    total_tokens: int


@dataclass
class LLMResponse:
    content: str
    usage: Usage


class OpenAICompatLLM:
    """Adapter for OpenAI-compatible chat completion endpoints (AIHubMix)."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> None:
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens

    def invoke(self, prompt: str) -> LLMResponse:
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
            temperature=self._temperature,
            max_tokens=self._max_tokens,
        )
        text = _extract_completion_text(resp)
        usage = resp.usage
        input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        total_tokens = int(getattr(usage, "total_tokens", input_tokens + output_tokens) or (input_tokens + output_tokens))
        return LLMResponse(
            content=text,
            usage=Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
            ),
        )


class DeterministicReranker:
    """Fallback reranker for observability-only runs when no cross-encoder is available."""

    def compute_score(self, pairs: list[tuple[str, str]]) -> list[float]:
        import re

        scores: list[float] = []
        for query, text in pairs:
            q_tokens = {t for t in re.findall(r"\w+", query.lower()) if t}
            t_tokens = {t for t in re.findall(r"\w+", text.lower()) if t}
            overlap = len(q_tokens & t_tokens)
            denom = max(1, len(q_tokens))
            scores.append(overlap / denom)
        return scores


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a P1 single-case trace report.")
    parser.add_argument("--case-id", default="g013")
    parser.add_argument("--golden-set", default=str(DEFAULT_GOLDEN_SET))
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--lancedb-dir", default=str(DEFAULT_LANCEDB_DIR))
    parser.add_argument("--provider", choices=["aihubmix"], default=DEFAULT_PROVIDER)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--window-days", type=int, default=3)
    parser.add_argument("--reranker-mode", choices=["off", "auto", "deterministic"], default="auto")
    parser.add_argument(
        "--use-critic",
        choices=["on", "off"],
        default="on",
        help="on: Miner->Critic->Judge; off: Miner->Judge baseline.",
    )
    parser.add_argument(
        "--query-embedder-mode",
        choices=["bge", "deterministic"],
        default="bge",
        help="bge uses local/Hub BGE-M3 query encoder; deterministic skips model download for fast local demo.",
    )
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--trace-dir", default=str(DEFAULT_TRACE_DIR))
    parser.add_argument("--tag", default=None, help="Optional run tag; defaults to UTC timestamp.")
    parser.add_argument("--query-override", default=None, help="Override query for this run.")
    return parser.parse_args()


def _load_case(golden_set_path: Path, case_id: str) -> dict[str, Any]:
    for line in golden_set_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if row.get("id") == case_id:
            return row
    raise ValueError(f"Case id not found: {case_id} in {golden_set_path}")


def _extract_completion_text(resp: Any) -> str:
    try:
        content = resp.choices[0].message.content
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            chunks: list[str] = []
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str):
                        chunks.append(text)
                else:
                    text = getattr(part, "text", None)
                    if isinstance(text, str):
                        chunks.append(text)
            if chunks:
                return "".join(chunks)
    except Exception:
        pass

    try:
        raw = resp.model_dump()
        msg = raw["choices"][0]["message"]
        if isinstance(msg.get("content"), str):
            return msg["content"]
    except Exception:
        pass
    return ""


def _resolve_aihubmix_key() -> str:
    key = os.environ.get("AIHUBMIX_API_KEY") or os.environ.get("aihubmix_api_key")
    if not key:
        raise RuntimeError("Missing AIHubMix key. Set AIHUBMIX_API_KEY (or aihubmix_api_key in .env).")
    return key


def _open_lancedb_table(lancedb_dir: Path) -> Any:
    import lancedb

    db = lancedb.connect(str(lancedb_dir))
    return db.open_table("chunks")


def _build_query_embedder() -> Any:
    from FlagEmbedding import BGEM3FlagModel  # type: ignore
    from catalyst_data.storage.lancedb_store import EMBEDDING_MODEL

    device_override = os.getenv("CATALYST_BGE_DEVICES")
    use_fp16 = os.getenv("CATALYST_BGE_USE_FP16", "1") != "0"

    if device_override:
        try:
            model = BGEM3FlagModel(EMBEDDING_MODEL, use_fp16=use_fp16, devices=device_override)
        except TypeError:
            model = BGEM3FlagModel(EMBEDDING_MODEL, use_fp16=use_fp16)
    else:
        model = BGEM3FlagModel(EMBEDDING_MODEL, use_fp16=use_fp16)

    def _embed(text: str) -> list[float]:
        return model.encode([text])["dense_vecs"][0].tolist()

    return _embed


def _build_deterministic_query_embedder(dim: int = 1024) -> Any:
    import hashlib

    def _embed(query: str) -> list[float]:
        digest = hashlib.sha256(query.encode("utf-8")).digest()
        values: list[float] = []
        for idx in range(dim):
            byte = digest[idx % len(digest)]
            values.append((byte / 255.0) - 0.5)
        return values

    return _embed


def _resolve_pricing_model_id(model: str) -> str:
    if model in MODEL_PRICING:
        return model
    if model.startswith("gemini-2.5-flash"):
        return "gemini-2.5-flash"
    return model


def _resolve_reranker(mode: str) -> Any:
    if mode == "off":
        return None
    if mode == "deterministic":
        return DeterministicReranker()

    from catalyst_data.storage.lancedb_store import load_reranker

    reranker = load_reranker()
    return reranker


def _fetch_one_int(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0] if row else 0)


def _collect_db_stats(case: dict[str, Any], db_path: Path, window_days: int) -> dict[str, Any]:
    ticker = case["ticker"]
    trade_date = case["trade_date"]
    start_date, end_date = _compute_date_range(trade_date, window_days)
    conn = sqlite3.connect(str(db_path))
    try:
        raw_ticker = _fetch_one_int(
            conn,
            "SELECT COUNT(*) FROM raw_assets WHERE ticker = ? AND reference_date BETWEEN ? AND ?",
            (ticker, start_date, end_date),
        )
        clean_ticker = _fetch_one_int(
            conn,
            "SELECT COUNT(*) FROM clean_assets WHERE ticker = ? AND reference_date BETWEEN ? AND ?",
            (ticker, start_date, end_date),
        )
        clean_ticker_nondup = _fetch_one_int(
            conn,
            "SELECT COUNT(*) FROM clean_assets WHERE ticker = ? AND reference_date BETWEEN ? AND ? AND is_duplicate = 0",
            (ticker, start_date, end_date),
        )
        clean_ticker_nondup_nonempty = _fetch_one_int(
            conn,
            (
                "SELECT COUNT(*) FROM clean_assets "
                "WHERE ticker = ? AND reference_date BETWEEN ? AND ? AND is_duplicate = 0 AND LENGTH(content_md) > 0"
            ),
            (ticker, start_date, end_date),
        )
        by_source_rows = conn.execute(
            (
                "SELECT source_type, COUNT(*) AS n "
                "FROM clean_assets "
                "WHERE ticker = ? AND reference_date BETWEEN ? AND ? AND is_duplicate = 0 AND LENGTH(content_md) > 0 "
                "GROUP BY source_type ORDER BY n DESC, source_type ASC"
            ),
            (ticker, start_date, end_date),
        ).fetchall()
    finally:
        conn.close()

    return {
        "ticker": ticker,
        "trade_date": trade_date,
        "window_days": window_days,
        "window_start": start_date,
        "window_end": end_date,
        "raw_assets_ticker_window": raw_ticker,
        "clean_assets_ticker_window": clean_ticker,
        "clean_nondup_ticker_window": clean_ticker_nondup,
        "clean_nondup_nonempty_ticker_window": clean_ticker_nondup_nonempty,
        "clean_by_source_ticker_window": [{"source_type": row[0], "count": int(row[1])} for row in by_source_rows],
    }


def _collect_index_stats(lancedb_dir: Path) -> dict[str, Any]:
    table = _open_lancedb_table(lancedb_dir)
    out: dict[str, Any] = {
        "lancedb_dir": str(lancedb_dir),
        "lancedb_dir_sha256": resolve_lancedb_dir_sha256(lancedb_dir),
        "total_rows": table.count_rows(),
        "l1_rows": table.count_rows("chunk_level = 'l1'"),
        "l2_rows": table.count_rows("chunk_level = 'l2'"),
        "l2_polygon_news_rows": table.count_rows("chunk_level = 'l2' AND source_type = 'polygon_news'"),
        "null_vector_rows": table.count_rows("vector IS NULL"),
    }
    return out


def _build_initial_state(
    case: dict[str, Any],
    *,
    query: str,
    db_path: Path,
    lancedb_dir: Path,
    model: str,
    window_days: int,
) -> dict[str, Any]:
    return {
        "ticker": case["ticker"],
        "trade_date": case["trade_date"],
        "query": query,
        "price_move_pct": case.get("price_move_pct"),
        "query_ticker_raw": case.get("query_ticker_raw"),
        "ticker_consistent": None,
        "market_session_valid": None,
        "magnitude_plausible": None,
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
            date_range=_compute_date_range(case["trade_date"], window_days),
            db_path=db_path,
            lancedb_dir=lancedb_dir,
        ),
        "cost_breakdown": [],
        "total_cost_usd": 0.0,
        "total_tokens": 0,
        "model_id": _resolve_pricing_model_id(model),
    }


def _citation_stats(result: dict[str, Any]) -> dict[str, Any]:
    retrieved = {chunk.get("asset_id") for chunk in result.get("reranked_chunks", []) if chunk.get("asset_id")}
    cited: list[str] = []
    for cause in result.get("causes", []):
        cited.extend(cause.get("evidence_ids", []))
    cited_unique = sorted(set(cited))
    matched = [cid for cid in cited_unique if cid in retrieved]
    return {
        "cited_unique": len(cited_unique),
        "cited_matched_in_reranked": len(matched),
        "citation_precision": (len(matched) / len(cited_unique)) if cited_unique else 1.0,
        "matched_ids": matched[:20],
    }


def _layer_counts(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    l1 = sum(1 for chunk in chunks if chunk.get("chunk_level") == "l1")
    l2 = sum(1 for chunk in chunks if chunk.get("chunk_level") == "l2")
    by_source: dict[str, int] = {}
    for chunk in chunks:
        source = str(chunk.get("source_type", "unknown"))
        by_source[source] = by_source.get(source, 0) + 1
    return {
        "total": len(chunks),
        "l1": l1,
        "l2": l2,
        "by_source": dict(sorted(by_source.items(), key=lambda kv: (-kv[1], kv[0]))),
    }


def _graded_counts(graded: list[dict[str, Any]], id_to_chunk: dict[str, dict[str, Any]]) -> dict[str, Any]:
    mapped = [id_to_chunk[c.get("chunk_id", "")] for c in graded if c.get("chunk_id", "") in id_to_chunk]
    return _layer_counts(mapped)


def _trace_event_summary(trace_payload: dict[str, Any]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for event in trace_payload.get("events", []):
        node = str(event.get("node", "unknown"))
        bucket = grouped.setdefault(
            node,
            {
                "node": node,
                "calls": 0,
                "latency_ms_sum": 0,
                "input_tokens_sum": 0,
                "output_tokens_sum": 0,
                "cost_usd_sum": 0.0,
            },
        )
        bucket["calls"] += 1
        bucket["latency_ms_sum"] += int(event.get("latency_ms", 0) or 0)
        bucket["input_tokens_sum"] += int(event.get("input_tokens", 0) or 0)
        bucket["output_tokens_sum"] += int(event.get("output_tokens", 0) or 0)
        bucket["cost_usd_sum"] += float(event.get("cost_usd", 0.0) or 0.0)

    rows = list(grouped.values())
    rows.sort(key=lambda row: row["latency_ms_sum"], reverse=True)
    return rows


def _eval_metrics(case: dict[str, Any], result: dict[str, Any]) -> dict[str, float]:
    golden = GoldenEvent.model_validate(case)
    predicted = AttributionResult(
        ticker=case["ticker"],
        trade_date=case["trade_date"],
        causes=[
            PredictedCause(
                text=cause.get("text", ""),
                category=cause.get("category", "unknown"),
                confidence=float(cause.get("confidence", 0.0) or 0.0),
                evidence_ids=list(cause.get("evidence_ids", [])),
                direction=cause.get("direction", "unknown"),
            )
            for cause in result.get("causes", [])
        ],
        summary=result.get("summary_md", ""),
        retrieved_evidence=[
            RetrievedEvidence(
                asset_id=chunk.get("asset_id", ""),
                content_md=chunk.get("content_md", ""),
                source_type=chunk.get("source_type", ""),
                rrf_score=float(chunk.get("rrf_score", 0.0) or 0.0),
            )
            for chunk in result.get("reranked_chunks", [])
        ],
        cost_breakdown=result.get("cost_breakdown", []),
        total_cost_usd=float(result.get("total_cost_usd", 0.0) or 0.0),
        total_tokens=int(result.get("total_tokens", 0) or 0),
    )

    return {
        "cause_match_f1": _compute_cause_match_f1(predicted, golden),
        "category_accuracy": CategoryAccuracy().compute(predicted, golden),
        "citation_faithfulness": _compute_faithfulness(predicted, golden),
        "refusal_correctness": _compute_refusal(predicted, golden),
        "confidence_calibration": ConfidenceCalibration().compute(predicted, golden),
    }



def _compute_cause_match_f1(predicted, golden):
    """Bridge: CauseMatch returns dict, extract F1 as float."""
    from catalyst_eval.metrics.cause_match import CauseMatch
    result = CauseMatch().compute(predicted, golden)
    return result.get("f1", 0.0) if isinstance(result, dict) else float(result)

def _compute_faithfulness(predicted, golden):
    """Bridge: CitationFaithfulness returns dict, extract score as float."""
    from catalyst_eval.metrics.citation_faithfulness import CitationFaithfulness
    result = CitationFaithfulness().compute(predicted, golden)
    return result.get("score", 0.0) if isinstance(result, dict) else float(result)

def _compute_refusal(predicted, golden):
    """Bridge: RefusalCorrectness returns dict, extract score as float."""
    from catalyst_eval.metrics.refusal_correctness import RefusalCorrectness
    result = RefusalCorrectness().compute(predicted, golden)
    return result.get("score", 0.0) if isinstance(result, dict) else float(result)

def _status_name(value: Any) -> str:
    return getattr(value, "name", None) or str(value)


def _render_markdown(summary: dict[str, Any]) -> str:
    run = summary["run"]
    case = summary["case"]
    dbs = summary["database"]
    idx = summary["index"]
    retr = summary["retrieval"]
    crit = summary["critic"]
    judge = summary["judge"]
    cite = summary["citations"]
    costs = summary["cost_and_trace"]
    metrics = summary["metrics"]

    lines: list[str] = []
    lines.append(f"# P1 Trace Report: {case['id']} ({case['ticker']} {case['trade_date']})")
    lines.append("")
    lines.append("## Run Context")
    lines.append("")
    lines.append(f"- run_tag: `{run['run_tag']}`")
    lines.append(f"- provider/model: `{run['provider']}` / `{run['model']}`")
    lines.append(f"- pricing_model_id: `{run['pricing_model_id']}`")
    lines.append(f"- query_embedder_mode: `{run['query_embedder_mode']}`")
    lines.append(f"- reranker_mode: `{run['reranker_mode']}`")
    lines.append(f"- run_id / trace_id: `{run['run_id']}` / `{run['trace_id']}`")
    lines.append(f"- db_sha256: `{run['db_sha256']}`")
    lines.append(f"- lancedb_dir_sha256: `{run['lancedb_dir_sha256']}`")
    lines.append(f"- report_ts_utc: `{run['report_ts_utc']}`")
    lines.append("")
    lines.append("## Data Collection Coverage")
    lines.append("")
    lines.append(f"- ticker window: `{dbs['window_start']} .. {dbs['window_end']}` (±{dbs['window_days']}d)")
    lines.append(f"- raw_assets_ticker_window: `{dbs['raw_assets_ticker_window']}`")
    lines.append(f"- clean_assets_ticker_window: `{dbs['clean_assets_ticker_window']}`")
    lines.append(f"- clean_nondup_ticker_window: `{dbs['clean_nondup_ticker_window']}`")
    lines.append(f"- clean_nondup_nonempty_ticker_window: `{dbs['clean_nondup_nonempty_ticker_window']}`")
    lines.append("")
    lines.append("| source_type | count |")
    lines.append("|---|---:|")
    for row in dbs["clean_by_source_ticker_window"]:
        lines.append(f"| {row['source_type']} | {row['count']} |")
    lines.append("")
    lines.append("## Index Readiness")
    lines.append("")
    lines.append(f"- total_rows: `{idx['total_rows']}`")
    lines.append(f"- l1_rows: `{idx['l1_rows']}`")
    lines.append(f"- l2_rows: `{idx['l2_rows']}`")
    lines.append(f"- l2_polygon_news_rows: `{idx['l2_polygon_news_rows']}`")
    lines.append(f"- null_vector_rows: `{idx['null_vector_rows']}`")
    lines.append("")
    lines.append("## Retrieval / Critic / Judge")
    lines.append("")
    lines.append(f"- layers_attempted: `{retr['layers_attempted']}`")
    lines.append(f"- hit_counts_per_layer: `{retr['hit_counts_per_layer']}`")
    lines.append(f"- stop_reason: `{retr['stop_reason']}`")
    lines.append(f"- retrieved_counts: `{retr['retrieved_counts']}`")
    lines.append(f"- reranked_counts: `{retr['reranked_counts']}`")
    lines.append(f"- graded_counts: `{retr['graded_counts']}`")
    lines.append("")
    lines.append(f"- critic_sufficiency: `{crit['sufficiency']}`")
    lines.append(f"- critic_next_action: `{crit['next_action']}`")
    lines.append(f"- critic_magnitude_coverage: `{crit['magnitude_coverage']}`")
    lines.append("")
    lines.append(f"- output_status: `{judge['output_status']}`")
    lines.append(f"- causes_count: `{judge['causes_count']}`")
    lines.append(f"- grounding_rate_field: `{judge['grounding_rate_field']}`")
    lines.append("")
    lines.append("## Grounding and Attribution Quality")
    lines.append("")
    lines.append(f"- cited_unique: `{cite['cited_unique']}`")
    lines.append(f"- cited_matched_in_reranked: `{cite['cited_matched_in_reranked']}`")
    lines.append(f"- citation_precision: `{cite['citation_precision']:.4f}`")
    lines.append("")
    for metric_name, metric_value in metrics.items():
        lines.append(f"- {metric_name}: `{metric_value:.4f}`")
    lines.append("")
    lines.append("## Node-Level Trace")
    lines.append("")
    lines.append(f"- total_latency_ms: `{costs['total_latency_ms']}`")
    lines.append(f"- total_tokens: `{costs['total_tokens']}`")
    lines.append(f"- total_cost_usd: `{costs['total_cost_usd']:.6f}`")
    lines.append("")
    lines.append("| node | calls | latency_ms_sum | input_tokens_sum | output_tokens_sum | cost_usd_sum |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for row in costs["events_by_node"]:
        lines.append(
            f"| {row['node']} | {row['calls']} | {row['latency_ms_sum']} | "
            f"{row['input_tokens_sum']} | {row['output_tokens_sum']} | {row['cost_usd_sum']:.6f} |"
        )
    lines.append("")
    lines.append("## Final Summary")
    lines.append("")
    summary_md = judge.get("summary_md", "").strip()
    lines.append(summary_md if summary_md else "_(empty summary)_")
    lines.append("")
    return "\n".join(lines)


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


def main() -> int:
    load_dotenv(PROJECT_ROOT / "packages" / "data-core" / ".env")
    args = _parse_args()

    case_id = args.case_id
    golden_set_path = Path(args.golden_set)
    db_path = Path(args.db)
    lancedb_dir = Path(args.lancedb_dir)
    out_dir = Path(args.out_dir)
    trace_dir = Path(args.trace_dir)

    if not golden_set_path.exists():
        raise FileNotFoundError(f"golden set not found: {golden_set_path}")
    if not db_path.exists():
        raise FileNotFoundError(f"db not found: {db_path}")
    if not lancedb_dir.exists():
        raise FileNotFoundError(f"lancedb dir not found: {lancedb_dir}")

    case = _load_case(golden_set_path, case_id)
    effective_query, effective_query_source = resolve_query_with_source(case, args.query_override)
    db_stats = _collect_db_stats(case, db_path, args.window_days)
    index_stats = _collect_index_stats(lancedb_dir)

    run_tag = args.tag or time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    run_prefix = f"{run_tag}_{case_id}_p1_trace"
    out_dir.mkdir(parents=True, exist_ok=True)
    trace_dir.mkdir(parents=True, exist_ok=True)
    trace_db = out_dir / f"{run_prefix}.trace.db"
    trace_json_path = out_dir / f"{run_prefix}.trace.json"
    summary_json_path = out_dir / f"{run_prefix}.summary.json"
    report_md_path = out_dir / f"{run_prefix}.report.md"

    if trace_db.exists():
        trace_db.unlink()

    api_key = _resolve_aihubmix_key()
    llm = OpenAICompatLLM(
        api_key=api_key,
        base_url=args.base_url,
        model=args.model,
        temperature=args.temperature,
        max_tokens=args.max_output_tokens,
    )
    table = _open_lancedb_table(lancedb_dir)
    if args.query_embedder_mode == "deterministic":
        embedding_fn = _build_deterministic_query_embedder()
    else:
        embedding_fn = _build_query_embedder()
    reranker = _resolve_reranker(args.reranker_mode)

    graph = build_attribution_graph(
        use_critic=(args.use_critic == "on"),
        table=table,
        embedding_fn=embedding_fn,
        reranker=reranker,
        llm=llm,
    )

    state = _build_initial_state(
        case,
        query=effective_query,
        db_path=db_path,
        lancedb_dir=lancedb_dir,
        model=args.model,
        window_days=args.window_days,
    )

    with _trace_db_env(trace_db):
        result = graph.invoke(state)

    trace_payload = export_run(result["run_id"], out_path=trace_json_path, db_path=trace_db)

    id_to_chunk = {chunk.get("asset_id"): chunk for chunk in result.get("retrieved_chunks", []) if chunk.get("asset_id")}
    metadata = result.get("retrieval_metadata")
    metadata_layers = []
    metadata_hits: dict[str, int] = {}
    stop_reason = None
    if metadata is not None:
        metadata_layers = [getattr(layer, "value", str(layer)) for layer in getattr(metadata, "layers_attempted", [])]
        raw_hits = getattr(metadata, "hit_counts_per_layer", {})
        metadata_hits = {getattr(layer, "value", str(layer)): int(count) for layer, count in raw_hits.items()}
        stop_reason = getattr(metadata, "stop_reason", None)

    citation = _citation_stats(result)
    metrics = _eval_metrics(case, result)
    event_summary = _trace_event_summary(trace_payload)

    critic_decision = result.get("critic_decision")
    summary: dict[str, Any] = {
        "run": {
            "run_tag": run_tag,
            "provider": args.provider,
            "model": args.model,
            "pricing_model_id": _resolve_pricing_model_id(args.model),
            "reranker_mode": args.reranker_mode,
            "use_critic": args.use_critic,
            "query_embedder_mode": args.query_embedder_mode,
            "run_id": result.get("run_id"),
            "trace_id": result.get("trace_id"),
            "report_ts_utc": utc_now_iso(),
            "db_path": str(db_path),
            "lancedb_dir": str(lancedb_dir),
            "db_sha256": sha256_file(db_path),
            "lancedb_dir_sha256": index_stats["lancedb_dir_sha256"],
            "trace_db": str(trace_db),
            "trace_json": str(trace_json_path),
            "effective_query_source": effective_query_source,
            "effective_query": effective_query,
            "query_ticker_raw": result.get("query_ticker_raw"),
            "ticker_consistent": result.get("ticker_consistent"),
            "market_session_valid": result.get("market_session_valid"),
            "magnitude_plausible": result.get("magnitude_plausible"),
        },
        "case": case,
        "database": db_stats,
        "index": index_stats,
        "retrieval": {
            "layers_attempted": metadata_layers,
            "hit_counts_per_layer": metadata_hits,
            "stop_reason": stop_reason,
            "retrieved_counts": _layer_counts(result.get("retrieved_chunks", [])),
            "reranked_counts": _layer_counts(result.get("reranked_chunks", [])),
            "graded_counts": _graded_counts(result.get("graded_evidence", []), id_to_chunk),
        },
        "critic": {
            "sufficiency": critic_decision.sufficiency if critic_decision else None,
            "next_action": critic_decision.next_action if critic_decision else None,
            "magnitude_coverage": critic_decision.magnitude_coverage if critic_decision else None,
            "reasoning_preview": (result.get("critic_reasoning", "") or "")[:1200],
            "all_graded_count": len(result.get("all_graded_chunks", [])),
            "all_graded_scores": [
                {
                    "chunk_id": c.get("chunk_id", ""),
                    "relevance": c.get("relevance", 0.0),
                    "category": c.get("category", ""),
                }
                for c in result.get("all_graded_chunks", [])
            ],
            "below_threshold_count": max(0, len(result.get("all_graded_chunks", [])) - len(result.get("graded_evidence", []))),
            "query_ticker_raw": result.get("query_ticker_raw"),
            "ticker_consistent": result.get("ticker_consistent"),
            "market_session_valid": result.get("market_session_valid"),
            "magnitude_plausible": result.get("magnitude_plausible"),
        },
        "judge": {
            "output_status": _status_name(result.get("output_status")),
            "validation_error": result.get("validation_error"),
            "causes_count": len(result.get("causes", [])),
            "causes": result.get("causes", []),
            "summary_md": result.get("summary_md", ""),
            "grounding_rate_field": result.get("grounding_rate"),
        },
        "citations": citation,
        "metrics": metrics,
        "cost_and_trace": {
            "total_latency_ms": int(trace_payload.get("total_latency_ms", 0) or 0),
            "total_tokens": int(result.get("total_tokens", 0) or 0),
            "total_cost_usd": float(result.get("total_cost_usd", 0.0) or 0.0),
            "events_by_node": event_summary,
        },
        "paths": {
            "summary_json": str(summary_json_path),
            "report_md": str(report_md_path),
            "trace_json": str(trace_json_path),
        },
    }

    summary_json_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    report_md_path.write_text(_render_markdown(summary))

    print(f"[ok] run_id={result.get('run_id')} trace_id={result.get('trace_id')}")
    print(f"[ok] summary_json={summary_json_path}")
    print(f"[ok] report_md={report_md_path}")
    print(f"[ok] trace_json={trace_json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
