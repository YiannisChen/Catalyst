#!/usr/bin/env python3
"""Event case-study workflow: build a local corpus around one golden-set event,
run retrieval + MCJ attribution, and produce a presentation-friendly report.

Target: g013 — NVDA +4.98% on 2025-10-28 (AI infrastructure / DoE contract)

Corpus scope:
  - Primary ticker: NVDA, trade_date ±3 calendar days
  - Distractor tickers: AMD, ORCL (same window)
  - Sources: polygon_news, polygon_ohlcv per ticker per date

Usage:
    python scripts/event_case_study.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "packages" / "data-core"))
sys.path.insert(0, str(PROJECT_ROOT / "packages" / "agents"))
sys.path.insert(0, str(PROJECT_ROOT / "packages" / "eval"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / "packages" / "data-core" / ".env")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
GOLDEN_SET_PATH = PROJECT_ROOT / "packages" / "eval" / "golden_set" / "v1_2.jsonl"
TARGET_EVENT_ID = "g013"

# Corpus construction
PRIMARY_TICKER = "NVDA"
DISTRACTOR_TICKERS = ["AMD", "ORCL"]
DATE_WINDOW_DAYS = 3
SOURCES = ["polygon_news", "polygon_ohlcv"]

# Models
GEMINI_MODEL = "gemini-2.5-flash"
EMBEDDING_MODEL = "all-MiniLM-L6-v2"

# Output paths
DATA_DIR = PROJECT_ROOT / "data" / "case_studies" / TARGET_EVENT_ID
DB_PATH = DATA_DIR / "case_study.db"
LANCEDB_PATH = DATA_DIR / "lancedb_index"
REPORT_DIR = PROJECT_ROOT / "data" / "eval_reports"
REPORT_PATH = REPORT_DIR / f"{TARGET_EVENT_ID}_case_study_report.md"
TRACE_PATH = REPORT_DIR / f"{TARGET_EVENT_ID}_case_study_trace.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _preview(text: str, max_chars: int = 250) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[:max_chars].rsplit(" ", 1)[0] + " ..."


def _source_label(source_type: str) -> str:
    labels = {
        "polygon_news": "News (Polygon)",
        "polygon_ohlcv": "Price data (OHLCV)",
        "fmp_fundamentals": "Financials (FMP)",
    }
    return labels.get(source_type, source_type)


def _date_range(center: str, window: int) -> list[str]:
    """Generate YYYY-MM-DD strings for center ± window calendar days."""
    dt = datetime.strptime(center, "%Y-%m-%d")
    dates = []
    for offset in range(-window, window + 1):
        d = dt + timedelta(days=offset)
        # skip weekends — no market data
        if d.weekday() < 5:
            dates.append(d.strftime("%Y-%m-%d"))
    return sorted(set(dates))


# ---------------------------------------------------------------------------
# Step 1: Load golden event
# ---------------------------------------------------------------------------

def load_golden_event(event_id: str) -> dict:
    with open(GOLDEN_SET_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            event = json.loads(line)
            if event["id"] == event_id:
                return event
    raise ValueError(f"Golden event {event_id} not found in {GOLDEN_SET_PATH}")


# ---------------------------------------------------------------------------
# Step 2: Build corpus via real connectors
# ---------------------------------------------------------------------------

async def build_corpus(
    tickers: list[str],
    dates: list[str],
    sources: list[str],
) -> tuple[sqlite3.Connection, list[dict]]:
    """Fetch data for each ticker × date × source and store in fresh SQLite."""
    from catalyst_data.storage.sqlite import init_db
    from catalyst_data.orchestrator import process_request
    from catalyst_data.config import RATE_POLICIES
    from catalyst_data.rate_limiter import TokenBucketLimiter
    from catalyst_data.connectors.polygon import create_polygon_fetcher
    from catalyst_data.connectors.fmp import create_fmp_fetcher
    from catalyst_data.connectors.base import FetchResult
    import httpx

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(str(DB_PATH))
    init_db(conn)

    policies = RATE_POLICIES.get("dev", {})
    limiters = {name: TokenBucketLimiter(policy) for name, policy in policies.items()}
    client = httpx.AsyncClient(timeout=30.0)

    polygon_fetch = create_polygon_fetcher(
        api_key=os.environ["POLYGON_API_KEY"],
        limiter=limiters.get("polygon"),
        client=client,
    )
    fmp_fetch = create_fmp_fetcher(
        api_key=os.environ["FMP_API_KEY"],
        limiter=limiters.get("fmp"),
        client=client,
    )

    _POLYGON_ENDPOINTS = {"ohlcv", "news"}
    _FMP_ENDPOINTS = {"income_statement", "balance_sheet", "cash_flow"}

    async def fetch(ticker: str, endpoint: str, date: str) -> FetchResult:
        if endpoint in _POLYGON_ENDPOINTS:
            return await polygon_fetch(ticker, endpoint, date)
        if endpoint in _FMP_ENDPOINTS:
            return await fmp_fetch(ticker, endpoint, date)
        return FetchResult(status=0, error=f"No connector for: {endpoint}")

    all_summaries: list[dict] = []
    total_requests = len(tickers) * len(dates) * len(sources)
    completed = 0

    for ticker in tickers:
        for date in dates:
            for source in sources:
                completed += 1
                tag = f"[{completed}/{total_requests}] {ticker}/{source}/{date}"
                try:
                    summaries = await process_request(
                        ticker=ticker,
                        date=date,
                        sources=[source],
                        db_path=str(DB_PATH),
                        fetch_fn=fetch,
                    )
                    for s in summaries:
                        s["_ticker"] = ticker
                        s["_date"] = date
                        status = "OK" if s.get("ok") else f"FAIL: {s.get('error', '?')}"
                        print(f"  {tag} -> {status}")
                    all_summaries.extend(summaries)
                except Exception as exc:
                    print(f"  {tag} -> ERROR: {exc}")
                    all_summaries.append({
                        "source": source, "_ticker": ticker, "_date": date,
                        "ok": False, "error": str(exc),
                    })

    await client.aclose()
    return conn, all_summaries


# ---------------------------------------------------------------------------
# Step 3: Build retrieval index
# ---------------------------------------------------------------------------

def build_index(conn: sqlite3.Connection, *, reuse_index: bool = False) -> tuple[Any, Any, list[dict]]:
    """Build LanceDB index from all non-duplicate Silver chunks."""
    import lancedb

    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT asset_id, ticker, source_type, reference_date, content_md "
        "FROM clean_assets WHERE is_duplicate = 0 AND LENGTH(content_md) > 0"
    ).fetchall()
    conn.row_factory = None
    chunks = [dict(r) for r in rows]

    if not chunks:
        return None, None, chunks

    LANCEDB_PATH.mkdir(parents=True, exist_ok=True)
    db = lancedb.connect(str(LANCEDB_PATH))

    model_cache: dict[str, Any] = {"model": None}

    def embedding_fn(text: str) -> list[float]:
        if model_cache["model"] is None:
            from sentence_transformers import SentenceTransformer
            model_cache["model"] = SentenceTransformer(EMBEDDING_MODEL)
        vec = model_cache["model"].encode([text], normalize_embeddings=True)
        return vec[0].tolist()

    existing = "chunks" in db.table_names()
    if reuse_index and existing:
        table = db.open_table("chunks")
        return table, embedding_fn, chunks

    print(f"  Loading embedding model ({EMBEDDING_MODEL})...")
    # Reuse the same lazy-loaded model instance used by embedding_fn.
    _ = embedding_fn("warmup")

    texts = [c["content_md"] for c in chunks]
    print(f"  Embedding {len(texts)} chunks...")
    vectors = model_cache["model"].encode(texts, show_progress_bar=False, normalize_embeddings=True)

    records = [
        {
            "asset_id": chunks[i]["asset_id"],
            "ticker": chunks[i]["ticker"],
            "source_type": chunks[i]["source_type"],
            "reference_date": chunks[i]["reference_date"],
            "content_md": chunks[i]["content_md"],
            "vector": vectors[i].tolist(),
        }
        for i in range(len(chunks))
    ]

    if existing:
        db.drop_table("chunks")
    table = db.create_table("chunks", data=records)
    table.create_fts_index("content_md", replace=True)

    return table, embedding_fn, chunks


# ---------------------------------------------------------------------------
# Step 4: Gemini LLM adapter (same as e2e_strict)
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

class GeminiLLM:
    def __init__(self, model: str = GEMINI_MODEL, temperature: float = 0.0):
        from google import genai
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY not set")
        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._temperature = temperature

    def invoke(self, prompt: str) -> LLMResponse:
        resp = self._client.models.generate_content(
            model=self._model,
            contents=prompt,
            config={
                "temperature": self._temperature,
                "max_output_tokens": 4096,
            },
        )
        text = resp.text or ""
        usage = resp.usage_metadata
        input_tokens = getattr(usage, "prompt_token_count", 0) or 0
        output_tokens = getattr(usage, "candidates_token_count", 0) or 0
        return LLMResponse(
            content=text,
            usage=Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
            ),
        )


# ---------------------------------------------------------------------------
# Step 5: Run MCJ pipeline
# ---------------------------------------------------------------------------

def run_mcj(table, embedding_fn, llm, golden: dict) -> dict:
    from catalyst_agents.graph import build_attribution_graph

    graph = build_attribution_graph(
        use_critic=True,
        table=table,
        embedding_fn=embedding_fn,
        reranker=None,
        llm=llm,
    )

    query = f"Why did {golden['ticker']} rise on {golden['trade_date']}?"
    initial_state = {
        "ticker": golden["ticker"],
        "trade_date": golden["trade_date"],
        "query": query,
        "price_move_pct": golden.get("price_move_pct"),
        "retrieved_chunks": [],
        "reranked_chunks": [],
        "graded_evidence": [],
        "critic_reasoning": "",
        "error_type": None,
        "causes": [],
        "summary_md": "",
        "grounding_rate": None,
        "cost_breakdown": [],
        "total_cost_usd": 0.0,
        "total_tokens": 0,
        "model_id": GEMINI_MODEL,
    }

    result = graph.invoke(initial_state)
    result["_query"] = query
    return result


# ---------------------------------------------------------------------------
# Step 6: Write case-study report
# ---------------------------------------------------------------------------

def write_report(
    golden: dict,
    dates_used: list[str],
    tickers_used: list[str],
    corpus_summaries: list[dict],
    silver_chunks: list[dict],
    state: dict,
    timings: dict[str, float],
) -> None:
    """Write Markdown report and JSON trace."""
    lines: list[str] = []

    def w(text: str = ""):
        lines.append(text)

    # ---- 1. Case metadata ----
    w(f"# Case Study: {golden['id']} — {golden['ticker']} on {golden['trade_date']}")
    w()
    w(f"> {golden['ticker']} moved **{golden['price_move_pct']:+.2f}%** on "
      f"{golden['trade_date']}. This report traces Catalyst's full pipeline "
      f"from data collection to final attribution.")
    w()
    w("| Field | Value |")
    w("|-------|-------|")
    w(f"| Golden ID | {golden['id']} |")
    w(f"| Ticker | {golden['ticker']} |")
    w(f"| Trade date | {golden['trade_date']} |")
    w(f"| Price move | {golden['price_move_pct']:+.2f}% |")
    w(f"| Date window | {dates_used[0]} to {dates_used[-1]} ({len(dates_used)} trading days) |")
    w(f"| Corpus tickers | {', '.join(tickers_used)} |")
    w(f"| Sources | {', '.join(SOURCES)} |")
    w(f"| LLM | {GEMINI_MODEL} |")
    w(f"| Embedding | {EMBEDDING_MODEL} |")
    w(f"| Run timestamp | {time.strftime('%Y-%m-%d %H:%M:%S')} |")
    w()

    # ---- 2. Golden reference ----
    w("---")
    w("## Golden Reference (Ground Truth)")
    w()
    w(f"From `v1_2.jsonl`, event **{golden['id']}** has "
      f"**{len(golden['causes'])} causes**:")
    w()
    for i, cause in enumerate(golden["causes"], 1):
        w(f"{i}. **[{cause['category']}]** {cause['text']}")
    w()

    # ---- 3. Corpus construction ----
    w("---")
    w("## Corpus Construction")
    w()
    if corpus_summaries:
        ok_count = sum(1 for s in corpus_summaries if s.get("ok"))
        fail_count = len(corpus_summaries) - ok_count
        w(f"**{ok_count}** sources fetched successfully, **{fail_count}** failed/empty.")
    else:
        w("Data fetch stage was skipped in this run; report reuses the existing local corpus.")
    w()

    # Chunk counts by ticker × source
    chunk_counts: dict[str, dict[str, int]] = {}
    for c in silver_chunks:
        tk = c.get("ticker", "?")
        src = c.get("source_type", "?")
        chunk_counts.setdefault(tk, {}).setdefault(src, 0)
        chunk_counts[tk][src] += 1

    if chunk_counts:
        all_sources = sorted({src for by_src in chunk_counts.values() for src in by_src})
        w("| Ticker | " + " | ".join(_source_label(s) for s in all_sources) + " | Total |")
        w("|--------|" + "|".join("---" for _ in all_sources) + "|-------|")
        for tk in sorted(chunk_counts):
            by_src = chunk_counts[tk]
            counts = [str(by_src.get(s, 0)) for s in all_sources]
            total = sum(by_src.values())
            w(f"| {tk} | " + " | ".join(counts) + f" | {total} |")
        grand = sum(sum(v.values()) for v in chunk_counts.values())
        w(f"| **Total** | " + " | ".join(
            str(sum(chunk_counts.get(tk, {}).get(s, 0) for tk in chunk_counts))
            for s in all_sources
        ) + f" | **{grand}** |")
    w()
    w(f"**Total indexed chunks:** {len(silver_chunks)}")
    w()

    # ---- 4. Searchable chunks ----
    w("---")
    w("## Searchable Chunks (Previews)")
    w()
    # Group by ticker for readability
    by_ticker: dict[str, list[dict]] = {}
    for c in silver_chunks:
        by_ticker.setdefault(c.get("ticker", "?"), []).append(c)

    for tk in sorted(by_ticker):
        w(f"### {tk}")
        w()
        for c in sorted(by_ticker[tk], key=lambda x: (x.get("reference_date", ""), x.get("source_type", ""))):
            aid = c.get("asset_id", "?")[:12]
            src = _source_label(c.get("source_type", "?"))
            ref = c.get("reference_date", "?")
            content = c.get("content_md", "")
            w(f"**{src}** | {ref} | `{aid}...` | {len(content):,} chars")
            w(f"> {_preview(content, 200)}")
            w()

    # ---- 5. Miner retrieval ----
    retrieved = state.get("retrieved_chunks", [])
    reranked = state.get("reranked_chunks", [])

    w("---")
    w("## Retrieval (Miner)")
    w()
    w(f"Query: *\"{state.get('_query', '')}\"*")
    w()
    w(f"- **{len(retrieved)}** chunks from hybrid search (BM25 + vector → RRF)")
    w(f"- **{len(reranked)}** chunks after ranking cutoff")
    w()
    if reranked:
        w("| Rank | Ticker | Source | Date | Score | Preview |")
        w("|------|--------|--------|------|-------|---------|")
        for rank, c in enumerate(reranked, 1):
            tk = c.get("ticker", "?")
            src = _source_label(c.get("source_type", "?"))
            ref = c.get("reference_date", "?")
            score = c.get("rrf_score", c.get("rerank_score", 0.0))
            content = c.get("content_md", "")
            w(f"| {rank} | {tk} | {src} | {ref} | {score:.4f} | {_preview(content, 100)} |")
        w()

    # ---- 6. Critic filtering ----
    graded = state.get("graded_evidence", [])
    critic_reasoning = state.get("critic_reasoning", "")

    w("---")
    w("## Evidence Filtering (Critic)")
    w()
    w(f"**{len(graded)} chunk(s)** passed relevance threshold (> 0.5):")
    w()
    if graded:
        w("| # | Chunk ID | Relevance | Category | Temporal Match | Reasoning |")
        w("|---|----------|-----------|----------|----------------|-----------|")
        for i, g in enumerate(graded, 1):
            cid = g.get("chunk_id", "?")[:12]
            rel = g.get("relevance", 0.0)
            cat = g.get("category", "?")
            temp = "Yes" if g.get("temporal_match") else "No"
            reason = _preview(g.get("reasoning", ""), 120)
            w(f"| {i} | `{cid}...` | {rel:.2f} | {cat} | {temp} | {reason} |")
        w()

    if critic_reasoning:
        w("**Critic assessment:**")
        w(f"> {_preview(critic_reasoning, 400)}")
        w()

    # ---- 7. Judge output ----
    causes = state.get("causes", [])
    summary = state.get("summary_md", "")
    grounding = state.get("grounding_rate")

    w("---")
    w("## Final Attribution (Judge)")
    w()
    if causes:
        for i, c in enumerate(causes, 1):
            cat = c.get("category", "unknown")
            text = c.get("text", "—")
            conf = c.get("confidence", 0.0)
            direction = c.get("direction", "—")
            evidence = c.get("evidence_ids", [])
            ev_str = ", ".join(f"`{e[:12]}...`" for e in evidence) if evidence else "none"

            w(f"### Cause {i}: {cat.title()}")
            w()
            w(text)
            w()
            w(f"- **Confidence:** {conf:.0%}")
            w(f"- **Direction:** {direction}")
            w(f"- **Evidence:** {ev_str}")
            w()
    else:
        w("*No causes produced — insufficient evidence path may have triggered.*")
        w()

    if summary:
        w("### Summary")
        w()
        clean_summary = re.sub(r"\s*\[[0-9a-f]{8,}\]", "", summary)
        w(f"> {clean_summary}")
        w()

    if grounding is not None:
        w(f"**Grounding rate:** {grounding:.0%}")
        w()

    # ---- 8. Comparison with golden ----
    w("---")
    w("## Comparison with Golden Reference")
    w()
    w("### Golden causes:")
    for i, gc in enumerate(golden["causes"], 1):
        w(f"{i}. **[{gc['category']}]** {gc['text']}")
    w()
    w("### Predicted causes:")
    for i, pc in enumerate(causes, 1):
        w(f"{i}. **[{pc.get('category', '?')}]** {pc.get('text', '—')} "
          f"(confidence: {pc.get('confidence', 0):.0%})")
    w()

    # Simple text overlap analysis
    golden_texts = [gc["text"].lower() for gc in golden["causes"]]
    predicted_texts = [pc.get("text", "").lower() for pc in causes]
    golden_cats = {gc["category"] for gc in golden["causes"]}
    predicted_cats = {pc.get("category", "") for pc in causes}

    w("### Analysis:")
    w()
    cat_overlap = golden_cats & predicted_cats
    if cat_overlap:
        w(f"- **Category overlap:** {', '.join(sorted(cat_overlap))} "
          f"({len(cat_overlap)}/{len(golden_cats)} golden categories matched)")
    else:
        w(f"- **Category overlap:** none (golden: {', '.join(sorted(golden_cats))}; "
          f"predicted: {', '.join(sorted(predicted_cats))})")

    # Check if key terms from golden causes appear in predicted causes
    all_pred_text = " ".join(predicted_texts)
    for i, gt in enumerate(golden_texts, 1):
        key_terms = [w_term for w_term in gt.split() if len(w_term) > 5][:5]
        matched_terms = [t for t in key_terms if t in all_pred_text]
        coverage = len(matched_terms) / max(len(key_terms), 1)
        w(f"- **Golden cause {i} keyword coverage:** {coverage:.0%} "
          f"({len(matched_terms)}/{len(key_terms)} key terms found in predictions)")
    w()

    # Run eval metrics if possible
    w("### Metric scores:")
    w()
    try:
        from catalyst_eval.schema.golden_event import GoldenEvent
        from catalyst_eval.schema.result import AttributionResult, PredictedCause, RetrievedEvidence
        from catalyst_eval.metrics import (
            AttributionF1, CategoryAccuracy, GroundingRate,
            TemporalPrecision, ConfidenceCalibration,
        )

        ge = GoldenEvent(**golden)
        ar = AttributionResult(
            ticker=golden["ticker"],
            trade_date=golden["trade_date"],
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
            summary=summary,
            retrieved_evidence=[
                RetrievedEvidence(
                    asset_id=c.get("asset_id", ""),
                    content_md=c.get("content_md", ""),
                    source_type=c.get("source_type", ""),
                    rrf_score=c.get("rrf_score", 0.0),
                )
                for c in reranked
            ],
            cost_breakdown=state.get("cost_breakdown", []),
            total_cost_usd=state.get("total_cost_usd", 0.0),
            total_tokens=state.get("total_tokens", 0),
        )

        metrics_list = [
            AttributionF1(), CategoryAccuracy(), GroundingRate(),
            TemporalPrecision(), ConfidenceCalibration(),
        ]

        w("| Metric | Score |")
        w("|--------|-------|")
        for m in metrics_list:
            score = m.compute(ar, ge)
            w(f"| {m.name} | **{score:.4f}** |")
        w()
    except Exception as exc:
        w(f"*Metric scoring failed: {exc}*")
        w()

    # ---- 9. Limitations ----
    w("---")
    w("## Limitations")
    w()
    w("This case study demonstrates Catalyst's end-to-end pipeline on a single "
      "golden-set event with a small local corpus. It is **not** a claim of "
      "benchmark-grade retrieval or attribution quality.")
    w()
    w(f"- **Small corpus:** {len(silver_chunks)} chunks across "
      f"{len(set(c.get('ticker') for c in silver_chunks))} tickers and "
      f"{len(dates_used)} trading days")
    w("- **No cross-encoder reranker:** Ranking uses RRF fusion only")
    w(f"- **Lightweight embeddings:** {EMBEDDING_MODEL} (384-dim), "
      f"not bge-m3 (1024-dim)")
    w("- **Distractor tickers are helpful but limited:** AMD and ORCL "
      "create realistic noise, but a production corpus would be much larger")
    w()

    # ---- 10. Appendix: timing & cost ----
    w("---")
    w("## Appendix: Runtime & Cost")
    w()
    w("*Demo run timings, not system benchmarks.*")
    w()
    w("| Stage | Time |")
    w("|-------|------|")
    for stage, ms in timings.items():
        w(f"| {stage} | {ms / 1000:.1f}s |")
    w()

    breakdown = state.get("cost_breakdown", [])
    if breakdown:
        total_cost = state.get("total_cost_usd", 0.0)
        total_tokens = state.get("total_tokens", 0)
        w("| LLM Call | Tokens | Cost |")
        w("|----------|--------|------|")
        for entry in breakdown:
            node = entry.get("node", "?").title()
            tokens = entry.get("input_tokens", 0) + entry.get("output_tokens", 0)
            cost = entry.get("cost_usd", 0.0)
            w(f"| {node} | {tokens:,} | ${cost:.4f} |")
        w(f"| **Total** | **{total_tokens:,}** | **${total_cost:.4f}** |")
        w()

    # ---- Write files ----
    report_text = "\n".join(lines)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(report_text)
    print(f"\nReport: {REPORT_PATH}")

    # JSON trace
    trace = {
        "golden_event": golden,
        "corpus": {
            "tickers": tickers_used,
            "dates": dates_used,
            "sources": SOURCES,
            "chunk_count": len(silver_chunks),
            "chunks_by_ticker": {
                tk: len([c for c in silver_chunks if c.get("ticker") == tk])
                for tk in tickers_used
            },
        },
        "retrieved_chunks": [
            {
                "asset_id": c.get("asset_id"),
                "ticker": c.get("ticker"),
                "source_type": c.get("source_type"),
                "reference_date": c.get("reference_date"),
                "rrf_score": c.get("rrf_score"),
                "preview": _preview(c.get("content_md", ""), 200),
            }
            for c in reranked
        ],
        "graded_evidence": graded,
        "critic_reasoning": critic_reasoning,
        "causes": causes,
        "summary": summary,
        "grounding_rate": grounding,
        "cost_breakdown": breakdown,
        "timings": timings,
    }
    TRACE_PATH.write_text(json.dumps(trace, indent=2, default=str))
    print(f"Trace:  {TRACE_PATH}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(description="Run g013 case study end-to-end.")
    parser.add_argument(
        "--skip-fetch",
        action="store_true",
        help="Reuse existing case_study.db instead of fetching data again.",
    )
    parser.add_argument(
        "--reuse-index",
        action="store_true",
        help="Reuse existing LanceDB table if present; otherwise rebuild index.",
    )
    args = parser.parse_args()

    wall_t0 = time.monotonic()

    # ---- Load golden event ----
    print(f"Loading golden event {TARGET_EVENT_ID}...")
    golden = load_golden_event(TARGET_EVENT_ID)
    trade_date = golden["trade_date"]
    print(f"  {golden['id']}: {golden['ticker']} {trade_date} ({golden['price_move_pct']:+.2f}%)")

    # ---- Preflight ----
    print("\nPreflight checks:")
    missing = []
    for key in ["GEMINI_API_KEY", "POLYGON_API_KEY"]:
        val = os.environ.get(key, "")
        if val:
            print(f"  {key}: SET")
        else:
            print(f"  {key}: MISSING")
            missing.append(key)
    if missing:
        print(f"\nBLOCKER: Missing API keys: {missing}")
        return

    # ---- Compute date window ----
    dates_used = _date_range(trade_date, DATE_WINDOW_DAYS)
    tickers_used = [PRIMARY_TICKER] + DISTRACTOR_TICKERS
    print(f"\nCorpus scope:")
    print(f"  Tickers: {tickers_used}")
    print(f"  Dates: {dates_used[0]} to {dates_used[-1]} ({len(dates_used)} trading days)")
    print(f"  Sources: {SOURCES}")
    total_requests = len(tickers_used) * len(dates_used) * len(SOURCES)
    print(f"  Total fetch requests: {total_requests}")

    # ---- Build or reuse corpus ----
    if args.skip_fetch:
        if not DB_PATH.exists():
            print(f"\nBLOCKER: --skip-fetch set but DB missing: {DB_PATH}")
            return
        print("\nSkipping fetch: reusing existing corpus DB")
        conn = sqlite3.connect(str(DB_PATH))
        corpus_summaries: list[dict] = []
        corpus_ms = 0.0
    else:
        print(f"\nBuilding corpus...")
        t0 = time.monotonic()
        conn, corpus_summaries = await build_corpus(tickers_used, dates_used, SOURCES)
        corpus_ms = (time.monotonic() - t0) * 1000

        ok_count = sum(1 for s in corpus_summaries if s.get("ok"))
        print(f"\n  Corpus: {ok_count}/{len(corpus_summaries)} sources OK in {corpus_ms/1000:.1f}s")

    # ---- Check Silver data ----
    conn.row_factory = sqlite3.Row
    silver_rows = conn.execute(
        "SELECT asset_id, ticker, source_type, reference_date, content_md "
        "FROM clean_assets WHERE is_duplicate = 0 AND LENGTH(content_md) > 0"
    ).fetchall()
    conn.row_factory = None
    silver_chunks = [dict(r) for r in silver_rows]
    print(f"  Silver chunks: {len(silver_chunks)}")

    if not silver_chunks:
        print("\nBLOCKER: No Silver chunks — cannot proceed.")
        conn.close()
        return

    # ---- Build index ----
    print(f"\nBuilding retrieval index...")
    t0 = time.monotonic()
    table, embedding_fn, _ = build_index(conn, reuse_index=args.reuse_index)
    index_ms = (time.monotonic() - t0) * 1000
    print(f"  Index built: {len(silver_chunks)} chunks in {index_ms/1000:.1f}s")

    if table is None:
        print("\nBLOCKER: Failed to build index.")
        conn.close()
        return

    # ---- Init LLM ----
    print(f"\nInitializing LLM ({GEMINI_MODEL})...")
    llm = GeminiLLM(model=GEMINI_MODEL, temperature=0.0)

    # ---- Run MCJ ----
    print(f"\nRunning MCJ pipeline...")
    t0 = time.monotonic()
    state = run_mcj(table, embedding_fn, llm, golden)
    mcj_ms = (time.monotonic() - t0) * 1000

    causes = state.get("causes", [])
    graded = state.get("graded_evidence", [])
    print(f"  MCJ completed in {mcj_ms/1000:.1f}s")
    print(f"  Retrieved: {len(state.get('retrieved_chunks', []))} chunks")
    print(f"  Reranked:  {len(state.get('reranked_chunks', []))} chunks")
    print(f"  Graded:    {len(graded)} chunks passed critic")
    print(f"  Causes:    {len(causes)} attributed")

    wall_total = (time.monotonic() - wall_t0) * 1000

    # ---- Write report ----
    print(f"\nWriting report...")
    timings = {
        "Data collection": corpus_ms,
        "Index build": index_ms,
        "Agent pipeline (Miner → Critic → Judge)": mcj_ms,
        "Total wall time": wall_total,
    }
    write_report(
        golden=golden,
        dates_used=dates_used,
        tickers_used=tickers_used,
        corpus_summaries=corpus_summaries,
        silver_chunks=silver_chunks,
        state=state,
        timings=timings,
    )

    conn.close()
    print(f"\nDone in {wall_total/1000:.1f}s")


if __name__ == "__main__":
    asyncio.run(main())
