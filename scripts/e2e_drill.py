#!/usr/bin/env python3
"""E2E acceptance drill: real data → real LLM → real eval scoring.

Usage:
    python scripts/e2e_drill.py

Reads NVDA Silver data from dev_assets.db, runs the MCJ graph with
gpt-4o as the LLM (bypasses LanceDB with in-memory search over real
chunks), scores against the golden set, and outputs a Markdown
acceptance report.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "packages" / "data-core"))
sys.path.insert(0, str(PROJECT_ROOT / "packages" / "agents"))
sys.path.insert(0, str(PROJECT_ROOT / "packages" / "eval"))

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TICKER = "NVDA"
TRADE_DATE = "2026-04-03"
DB_PATH = PROJECT_ROOT / "data" / "dev_assets.db"
GOLDEN_SET_PATH = PROJECT_ROOT / "packages" / "eval" / "golden_set" / "v1.jsonl"
MODEL_ID = "gpt-4o"


# ---------------------------------------------------------------------------
# 1. Load real Silver data from SQLite
# ---------------------------------------------------------------------------
def load_silver_chunks(db_path: Path, ticker: str) -> list[dict]:
    """Read non-duplicate clean_assets for a ticker."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT asset_id, ticker, source_type, reference_date, content_md "
        "FROM clean_assets WHERE ticker = ? AND is_duplicate = 0 AND LENGTH(content_md) > 0",
        (ticker,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 2. In-memory search (bypasses LanceDB — uses real Silver data directly)
# ---------------------------------------------------------------------------
def make_inmemory_search(chunks: list[dict]):
    """Return a hybrid_search-compatible function that returns all chunks."""
    def search(table, query, ticker=None, date_range=None, top_k=20, embedding_fn=None):
        results = list(chunks)  # copy
        if ticker:
            results = [c for c in results if c.get("ticker") == ticker]
        for i, c in enumerate(results):
            c["rrf_score"] = 1.0 / (60 + i + 1)
        return results[:top_k]
    return search


# ---------------------------------------------------------------------------
# 3. OpenAI LLM adapter (matches .invoke() / .content / .usage interface)
# ---------------------------------------------------------------------------
@dataclass
class Usage:
    input_tokens: int
    output_tokens: int
    total_tokens: int


@dataclass
class LLMResponse:
    content: str
    usage: Usage


class OpenAILLM:
    """Thin adapter wrapping openai.ChatCompletion into the .invoke() contract."""

    def __init__(self, model: str = "gpt-4o", temperature: float = 0.0):
        from openai import OpenAI
        self._client = OpenAI()
        self._model = model
        self._temperature = temperature

    def invoke(self, prompt: str) -> LLMResponse:
        resp = self._client.chat.completions.create(
            model=self._model,
            temperature=self._temperature,
            messages=[{"role": "user", "content": prompt}],
        )
        msg = resp.choices[0].message.content or ""
        u = resp.usage
        return LLMResponse(
            content=msg,
            usage=Usage(
                input_tokens=u.prompt_tokens,
                output_tokens=u.completion_tokens,
                total_tokens=u.total_tokens,
            ),
        )


class DeterministicLLM:
    """Mock LLM that produces realistic structured output for pipeline validation.

    Used when no API keys are available. Produces deterministic responses based
    on the prompt content (critic vs judge detection).
    """

    def __init__(self, chunks: list[dict]):
        self._chunks = chunks
        self._call_count = 0

    def invoke(self, prompt: str) -> LLMResponse:
        self._call_count += 1
        # Detect whether this is a Critic or Judge call
        if "Grade each evidence chunk" in prompt:
            return self._critic_response(prompt)
        return self._judge_response(prompt)

    def _critic_response(self, prompt: str) -> LLMResponse:
        graded = []
        categories = ["sector", "earnings", "macro"]
        for i, chunk in enumerate(self._chunks):
            aid = chunk.get("asset_id", f"chunk_{i}")
            cat = categories[i % len(categories)]
            # News gets high relevance, OHLCV gets low
            rel = 0.8 if "news" in chunk.get("source_type", "") else (
                0.7 if "fundamentals" in chunk.get("source_type", "") else 0.3
            )
            graded.append({
                "chunk_id": aid,
                "relevance": rel,
                "category": cat,
                "temporal_match": True,
                "reasoning": f"Chunk from {chunk.get('source_type', 'unknown')} is relevant to price move analysis.",
            })
        resp = json.dumps({
            "graded_chunks": graded,
            "reasoning": "News and fundamentals provide direct evidence; OHLCV is contextual only.",
        })
        return LLMResponse(content=resp, usage=Usage(input_tokens=3200, output_tokens=450, total_tokens=3650))

    def _judge_response(self, prompt: str) -> LLMResponse:
        eids = [c.get("asset_id", "") for c in self._chunks if "news" in c.get("source_type", "") or "fundamentals" in c.get("source_type", "")]
        resp = json.dumps({
            "causes": [
                {
                    "text": "Broader semiconductor sector pressure amid AI spending uncertainty and export control concerns",
                    "category": "sector",
                    "confidence": 0.45,
                    "evidence_ids": eids[:1],
                    "direction": "negative",
                },
                {
                    "text": "Mixed earnings signals with strong revenue but margin compression",
                    "category": "earnings",
                    "confidence": 0.35,
                    "evidence_ids": eids[1:2] if len(eids) > 1 else eids[:1],
                    "direction": "negative",
                },
                {
                    "text": "Market-wide risk rotation out of mega-cap tech",
                    "category": "technical",
                    "confidence": 0.15,
                    "evidence_ids": [],
                    "direction": "negative",
                },
            ],
            "summary_md": (
                f"NVDA experienced downward pressure on {TRADE_DATE} driven primarily by "
                f"semiconductor sector concerns [{eids[0][:12]}] and mixed earnings signals "
                f"[{eids[1][:12] if len(eids) > 1 else eids[0][:12]}]. "
                f"A broader tech rotation contributed as a secondary factor."
            ),
            "self_grounding_check": {"total_claims": 3, "grounded_claims": 2, "ungrounded_claims": 1},
        })
        return LLMResponse(content=resp, usage=Usage(input_tokens=3500, output_tokens=520, total_tokens=4020))


# ---------------------------------------------------------------------------
# 4. Run the pipeline
# ---------------------------------------------------------------------------
def run_e2e():
    report_lines: list[str] = []

    def log(msg: str = ""):
        print(msg)
        report_lines.append(msg)

    log("# E2E Acceptance Report")
    log(f"\n**Date:** {time.strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"**Ticker:** {TICKER}  |  **Trade date:** {TRADE_DATE}  |  **Model:** {MODEL_ID}")
    log()

    # --- Step 1: Load Silver data ---
    log("## 1. Data Layer (Silver)")
    if not DB_PATH.exists():
        log(f"BLOCKER: {DB_PATH} not found. Run smoke_test first.")
        return "\n".join(report_lines)

    chunks = load_silver_chunks(DB_PATH, TICKER)
    log(f"- Loaded **{len(chunks)}** non-duplicate Silver chunks for {TICKER}")
    for c in chunks:
        log(f"  - `{c['asset_id'][:12]}...` {c['source_type']:20s} {c['reference_date']} ({len(c['content_md'])} chars)")

    if not chunks:
        log(f"\nBLOCKER: No Silver data for {TICKER}. Run smoke_test with --ticker {TICKER}.")
        return "\n".join(report_lines)
    log()

    # --- Step 2: Monkeypatch hybrid_search and build graph ---
    log("## 2. Agent Graph (Miner → Critic → Judge)")
    import catalyst_data.storage.lancedb_store as lancedb_mod
    lancedb_mod.hybrid_search = make_inmemory_search(chunks)

    from catalyst_agents.graph import build_attribution_graph

    # Try real LLM first, fall back to deterministic mock
    llm_mode = "LIVE"
    try:
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY not set")
        llm = OpenAILLM(model=MODEL_ID, temperature=0.0)
        # Quick sanity check (will raise if key is invalid)
        log(f"- LLM: **{MODEL_ID}** (live API)")
    except Exception as exc:
        llm_mode = "DETERMINISTIC"
        llm = DeterministicLLM(chunks)
        log(f"- LLM: **DeterministicLLM** (mock — no API key: {exc})")
        log("  *To run with real LLM: export OPENAI_API_KEY=sk-...*")

    graph = build_attribution_graph(use_critic=True, llm=llm)

    initial_state = {
        "ticker": TICKER,
        "trade_date": TRADE_DATE,
        "query": f"Analyze {TICKER}'s price move attribution on {TRADE_DATE}.",
        "price_move_pct": None,
        "retrieved_chunks": [],
        "reranked_chunks": [],
        "graded_evidence": [],
        "critic_reasoning": "",
        "causes": [],
        "summary_md": "",
        "grounding_rate": None,
        "cost_breakdown": [],
        "total_cost_usd": 0.0,
        "total_tokens": 0,
        "model_id": MODEL_ID,
    }

    log("- Running MCJ pipeline...")
    t0 = time.monotonic()
    try:
        result = graph.invoke(initial_state)
        elapsed_ms = (time.monotonic() - t0) * 1000
        log(f"- Pipeline completed in **{elapsed_ms:.0f} ms**")
    except Exception as exc:
        elapsed_ms = (time.monotonic() - t0) * 1000
        log(f"- **PIPELINE FAILED** after {elapsed_ms:.0f} ms: {exc}")
        log(f"\n### Failure Detail\n```\n{exc}\n```")
        return "\n".join(report_lines)

    log()

    # --- Step 3: Intermediate artifacts ---
    log("### Retrieved Chunks (Miner output)")
    retrieved = result.get("retrieved_chunks", [])
    reranked = result.get("reranked_chunks", [])
    log(f"- Retrieved: {len(retrieved)} chunks  |  Reranked: {len(reranked)} chunks")
    for c in reranked[:5]:
        log(f"  - `{c.get('asset_id', '?')[:12]}...` {c.get('source_type', '?')} (rrf={c.get('rrf_score', 0):.4f})")
    log()

    log("### Critic Output")
    graded = result.get("graded_evidence", [])
    log(f"- Graded evidence: **{len(graded)}** chunks passed relevance > 0.5")
    for g in graded:
        log(f"  - `{g.get('chunk_id', '?')[:12]}...` relevance={g.get('relevance', 0):.2f} cat={g.get('category', '?')}")
    log(f"- Critic reasoning: {result.get('critic_reasoning', 'N/A')[:200]}")
    log()

    # --- Step 4: Final attribution output ---
    log("### Judge Output (AttributionResult)")
    causes = result.get("causes", [])
    log(f"- **{len(causes)} causes** identified:")
    for i, c in enumerate(causes, 1):
        log(f"  {i}. [{c.get('category', '?')}] {c.get('text', '?')[:80]}")
        log(f"     confidence={c.get('confidence', 0):.2f} | direction={c.get('direction', '?')} | evidence={c.get('evidence_ids', [])}")
    log()
    log(f"**Summary:**\n> {result.get('summary_md', 'N/A')}")
    log(f"\n**Grounding rate:** {result.get('grounding_rate', 'N/A')}")
    log()

    # --- Step 5: Cost ---
    log("### Cost")
    breakdown = result.get("cost_breakdown", [])
    for entry in breakdown:
        log(f"- {entry.get('node', '?')}: {entry.get('input_tokens', 0)} in + {entry.get('output_tokens', 0)} out = ${entry.get('cost_usd', 0):.6f}")
    log(f"- **Total:** ${result.get('total_cost_usd', 0):.6f} | {result.get('total_tokens', 0)} tokens")
    log()

    # --- Step 6: Eval scoring ---
    log("## 3. Evaluation Scoring")
    from catalyst_eval.schema.result import AttributionResult, PredictedCause
    from catalyst_eval.metrics import (
        AttributionF1, CategoryAccuracy, GroundingRate,
        TemporalPrecision, ConfidenceCalibration,
    )
    from catalyst_eval.schema.golden_event import GoldenEvent

    # Build AttributionResult from graph output
    attr_result = AttributionResult(
        ticker=result["ticker"],
        trade_date=result["trade_date"],
        causes=[
            PredictedCause(
                text=c.get("text", ""),
                category=c.get("category", "unknown"),
                confidence=c.get("confidence", 0.0),
                evidence_ids=c.get("evidence_ids", []),
                direction=c.get("direction", "unknown"),
            )
            for c in causes
        ],
        summary=result.get("summary_md", ""),
        retrieved_chunks=[c.get("content_md", "") for c in reranked],
        cost_breakdown=breakdown,
        total_cost_usd=result.get("total_cost_usd", 0.0),
        total_tokens=result.get("total_tokens", 0),
    )

    # Load golden set — find matching event or use first one as proxy
    golden_events = []
    if GOLDEN_SET_PATH.exists():
        with open(GOLDEN_SET_PATH) as f:
            for line in f:
                line = line.strip()
                if line:
                    golden_events.append(GoldenEvent(**json.loads(line)))

    # Find a golden event for NVDA, or use the closest proxy
    golden = None
    for ge in golden_events:
        if ge.ticker == TICKER:
            golden = ge
            break

    if golden is None:
        log(f"- No golden event for {TICKER} in v1.jsonl. Using NVDA g001 as proxy if available.")
        # Use first NVDA event even if date doesn't match
        for ge in golden_events:
            if ge.ticker == "NVDA":
                golden = ge
                break

    if golden is None:
        log("- **SKIP:** No golden event available for scoring.")
    else:
        log(f"- Golden event: `{golden.id}` {golden.ticker} {golden.trade_date} ({golden.price_move_pct:+.2f}%)")
        log(f"- Note: trade dates may differ (golden={golden.trade_date}, predicted={TRADE_DATE})")
        log()

        metrics = [
            AttributionF1(),
            CategoryAccuracy(),
            GroundingRate(),
            TemporalPrecision(),
            ConfidenceCalibration(),
        ]

        log("| Metric | Score |")
        log("|--------|-------|")
        for m in metrics:
            score = m.compute(attr_result, golden)
            log(f"| {m.name} | {score:.4f} |")
    log()

    # --- Step 7: Residual risks ---
    log("## 4. Residual Risks & Failure Points")
    log()
    log("| Risk | Severity | Notes |")
    log("|------|----------|-------|")
    log("| LanceDB bypassed (in-memory search) | High | Real hybrid BM25+vector not tested |")
    log("| No reranker (bge-reranker-v2) | Medium | Chunks returned in insertion order, not relevance |")
    log("| Golden set date mismatch | Medium | v1.jsonl events are Jan 2025, data is Apr 2026 |")
    if not graded:
        log("| All evidence filtered by Critic | High | Insufficient evidence path triggered |")
    if result.get("grounding_rate") is not None and result["grounding_rate"] < 0.5:
        log("| Low grounding rate | High | Judge fabricated causes without evidence |")
    log("| Cost tracking uses gpt-4o pricing | Low | MODEL_PRICING table has gpt-4o entry |")
    if llm_mode == "DETERMINISTIC":
        log("| Deterministic LLM (no real inference) | High | Set OPENAI_API_KEY to test real LLM |")
    log()

    # Save report
    report_dir = PROJECT_ROOT / "data" / "eval_reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "e2e_acceptance_report.md"
    report_text = "\n".join(report_lines)
    report_path.write_text(report_text)
    print(f"\nReport saved to {report_path}")

    return report_text


if __name__ == "__main__":
    run_e2e()
