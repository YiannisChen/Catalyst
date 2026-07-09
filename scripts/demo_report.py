"""Generate a presentation-friendly demo report from a completed pipeline run.

Formats intermediate pipeline state (run metadata, silver chunks, pipeline state)
into a readable Markdown report suitable for walkthroughs and presentations —
no jargon, readable chunk previews, and an honest limitations section.

Usage:
    from demo_report import write_demo_report
    write_demo_report(run_metadata, silver_chunks, pipeline_state, output_path)
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _preview(text: str, max_chars: int = 250) -> str:
    """Collapse whitespace and truncate to a readable preview."""
    cleaned = re.sub(r"\s+", " ", text).strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[:max_chars].rsplit(" ", 1)[0] + " ..."


def _source_label(source_type: str) -> str:
    """Human-readable label for a source_type value."""
    labels = {
        "polygon_news": "News articles (Polygon)",
        "polygon_ohlcv": "Price data (Polygon OHLCV)",
        "fmp_fundamentals": "Financial statements (FMP)",
        "fred_macro": "Macro indicators (FRED)",
        "yfinance_ohlcv": "Price data (Yahoo Finance)",
    }
    return labels.get(source_type, source_type)


# ---------------------------------------------------------------------------
# Data container
# ---------------------------------------------------------------------------

@dataclass
class RunMetadata:
    ticker: str
    trade_date: str
    query: str
    model: str
    embedding_model: str
    timestamp: str = ""
    is_fresh_run: bool = True
    wall_seconds: float = 0.0
    data_pipeline_ms: float = 0.0
    gold_index_ms: float = 0.0
    mcj_pipeline_ms: float = 0.0

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------

def write_demo_report(
    meta: RunMetadata,
    silver_chunks: list[dict],
    state: dict[str, Any],
    output_path: Path,
    trace_path: Path | None = None,
) -> Path:
    """Write the midterm demo report and optional JSON trace.

    Args:
        meta:          Run metadata (ticker, model, timings, etc.)
        silver_chunks: List of dicts with asset_id, source_type,
                       reference_date, content_md from Silver storage.
        state:         Final AttributionState dict from the MCJ pipeline.
        output_path:   Where to write the Markdown report.
        trace_path:    If provided, write a JSON trace alongside.

    Returns:
        The output_path that was written.
    """
    lines: list[str] = []

    def w(text: str = ""):
        lines.append(text)

    # ==================================================================
    # 1. Title / Run Metadata
    # ==================================================================
    w("# Catalyst — Midterm Demo Report")
    w()
    w(f"> **What is this?** A full end-to-end trace of Catalyst answering "
      f"*\"Why did {meta.ticker} move on {meta.trade_date}?\"* — "
      f"showing every intermediate step from data fetch to final attribution.")
    w()
    w("| Field | Value |")
    w("|-------|-------|")
    w(f"| Run timestamp | {meta.timestamp} |")
    w(f"| Ticker | {meta.ticker} |")
    w(f"| Trade date | {meta.trade_date} |")
    w(f"| LLM | {meta.model} |")
    w(f"| Embedding model | {meta.embedding_model} |")
    w(f"| Fresh data run | {'Yes' if meta.is_fresh_run else 'No (cached)'} |")
    w(f"| Total wall time | {meta.wall_seconds:.1f}s |")
    w()

    # ==================================================================
    # 2. Input
    # ==================================================================
    w("---")
    w("## Step 1: Input Query")
    w()
    w(f"```")
    w(meta.query)
    w(f"```")
    w()

    # ==================================================================
    # 3. Fetched Data
    # ==================================================================
    w("---")
    w("## Step 2: Data Collection")
    w()
    w("Catalyst fetches real-time data from multiple financial APIs, "
      "cleans it, and converts it into searchable text chunks.")
    w()
    if silver_chunks:
        w(f"**{len(silver_chunks)} data sources** collected and processed:")
        w()
        w("| # | Source | Date | Size |")
        w("|---|--------|------|------|")
        for i, chunk in enumerate(silver_chunks, 1):
            label = _source_label(chunk.get("source_type", "unknown"))
            ref_date = chunk.get("reference_date", "—")
            content = chunk.get("content_md", "")
            size = f"{len(content):,} chars"
            w(f"| {i} | {label} | {ref_date} | {size} |")
        w()
    else:
        w("*No data chunks were produced in this run.*")
        w()

    # ==================================================================
    # 4. Searchable Chunks (previews)
    # ==================================================================
    w("---")
    w("## Step 3: Searchable Chunks")
    w()
    w("Each data source is converted to a text chunk and indexed "
      "for both keyword (BM25) and semantic (vector) search.")
    w()
    for i, chunk in enumerate(silver_chunks, 1):
        aid = chunk.get("asset_id", "?")[:12]
        src = _source_label(chunk.get("source_type", "unknown"))
        content = chunk.get("content_md", "")
        w(f"### Chunk {i} — {src}")
        w(f"*ID: `{aid}...` | {len(content):,} chars*")
        w()
        w(f"> {_preview(content, 300)}")
        w()

    # ==================================================================
    # 5. Miner Retrieval
    # ==================================================================
    retrieved = state.get("retrieved_chunks", [])
    reranked = state.get("reranked_chunks", [])

    w("---")
    w("## Step 4: Retrieval (Miner)")
    w()
    w(f"The Miner searches all indexed chunks using hybrid retrieval "
      f"(keyword + semantic), then merges results with Reciprocal Rank Fusion.")
    w()
    w(f"- **{len(retrieved)}** chunks retrieved from hybrid search")
    w(f"- **{len(reranked)}** chunks kept after ranking cutoff")
    w()
    if reranked:
        w("| Rank | Source | Score | Preview |")
        w("|------|--------|-------|---------|")
        for rank, chunk in enumerate(reranked, 1):
            src = _source_label(chunk.get("source_type", "unknown"))
            score = chunk.get("rrf_score", chunk.get("rerank_score", 0.0))
            content = chunk.get("content_md", "")
            w(f"| {rank} | {src} | {score:.4f} | {_preview(content, 120)} |")
        w()

    # ==================================================================
    # 6. Critic Filtering
    # ==================================================================
    graded = state.get("graded_evidence", [])
    critic_reasoning = state.get("critic_reasoning", "")

    w("---")
    w("## Step 5: Evidence Filtering (Critic)")
    w()
    w("The Critic — an LLM call — grades each retrieved chunk for relevance, "
      "assigns a category, and filters out low-quality evidence (relevance < 0.5).")
    w()
    if graded:
        w(f"**{len(graded)} chunk(s)** passed the relevance threshold:")
        w()
        w("| Chunk | Relevance | Category | Temporal Match |")
        w("|-------|-----------|----------|----------------|")
        for g in graded:
            cid = g.get("chunk_id", "?")[:12]
            rel = g.get("relevance", 0.0)
            cat = g.get("category", "?")
            temp = "Yes" if g.get("temporal_match") else "No"
            w(f"| `{cid}...` | {rel:.2f} | {cat} | {temp} |")
        w()
        if g.get("reasoning"):
            w(f"*Critic reasoning (sample):* {_preview(g['reasoning'], 200)}")
            w()
    else:
        w("*No chunks passed the relevance threshold. The insufficient-evidence "
          "fallback was triggered.*")
        w()

    if critic_reasoning:
        w("**Overall Critic assessment:**")
        w()
        w(f"> {_preview(critic_reasoning, 400)}")
        w()

    # ==================================================================
    # 7. Judge Attribution
    # ==================================================================
    causes = state.get("causes", [])
    summary = state.get("summary_md", "")
    grounding = state.get("grounding_rate")

    w("---")
    w("## Step 6: Final Attribution (Judge)")
    w()
    w("The Judge — a second LLM call — synthesizes the filtered evidence "
      "into structured causal explanations for the price move.")
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
            w(f"{text}")
            w()
            w(f"- **Confidence:** {conf:.0%}")
            w(f"- **Direction:** {direction}")
            w(f"- **Evidence:** {ev_str}")
            w()
    else:
        w("*No causes were produced — the pipeline may have hit the "
          "insufficient-evidence path.*")
        w()

    if summary:
        w("### Summary")
        w()
        # Strip inline evidence IDs for readability
        clean_summary = re.sub(r"\s*\[[0-9a-f]{8,}\]", "", summary)
        w(f"> {clean_summary}")
        w()

    if grounding is not None:
        w(f"**Grounding rate:** {grounding:.0%} of cited evidence IDs "
          f"match retrieved chunks")
        w()

    # ==================================================================
    # 8. Limitations
    # ==================================================================
    w("---")
    w("## Limitations")
    w()
    w("This demo report proves the **end-to-end chain works** and exposes "
      "intermediate reasoning artifacts at every stage. It is **not** a claim "
      "of final retrieval quality or benchmark performance.")
    w()
    w("- **Small corpus:** This run ingested data for a single ticker on a "
      "single date. Production would have a much larger retrieval index.")
    w("- **No cross-encoder reranker:** Retrieval ranking uses RRF fusion "
      "only. The bge-reranker-v2 cross-encoder would improve precision.")
    w("- **Lightweight embeddings:** Using all-MiniLM-L6-v2 (384-dim) "
      "instead of bge-m3 (1024-dim) for CPU practicality.")
    w("- **Single-event validation:** The golden set comparison is "
      "illustrative, not statistically meaningful with one event.")
    w()

    # ==================================================================
    # 9. Appendix: Timing & Cost
    # ==================================================================
    breakdown = state.get("cost_breakdown", [])
    total_cost = state.get("total_cost_usd", 0.0)
    total_tokens = state.get("total_tokens", 0)

    w("---")
    w("## Appendix: Demo Runtime & Cost")
    w()
    w("*These numbers reflect this specific demo run, not system benchmarks.*")
    w()
    w("| Stage | Time |")
    w("|-------|------|")
    w(f"| Data collection & processing | {meta.data_pipeline_ms / 1000:.1f}s |")
    w(f"| Retrieval index build | {meta.gold_index_ms / 1000:.1f}s |")
    w(f"| Agent pipeline (Miner → Critic → Judge) | {meta.mcj_pipeline_ms / 1000:.1f}s |")
    w(f"| **Total** | **{meta.wall_seconds:.1f}s** |")
    w()

    if breakdown:
        w("| LLM Call | Tokens | Cost |")
        w("|----------|--------|------|")
        for entry in breakdown:
            node = entry.get("node", "?").title()
            tokens = entry.get("input_tokens", 0) + entry.get("output_tokens", 0)
            cost = entry.get("cost_usd", 0.0)
            w(f"| {node} | {tokens:,} | ${cost:.4f} |")
        w(f"| **Total** | **{total_tokens:,}** | **${total_cost:.4f}** |")
        w()

    # ==================================================================
    # Write files
    # ==================================================================
    report_text = "\n".join(lines)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report_text)

    # Optional JSON trace
    if trace_path is not None:
        trace = {
            "metadata": {
                "ticker": meta.ticker,
                "trade_date": meta.trade_date,
                "query": meta.query,
                "model": meta.model,
                "embedding_model": meta.embedding_model,
                "timestamp": meta.timestamp,
                "is_fresh_run": meta.is_fresh_run,
                "wall_seconds": meta.wall_seconds,
            },
            "silver_chunks": [
                {
                    "asset_id": c.get("asset_id"),
                    "source_type": c.get("source_type"),
                    "reference_date": c.get("reference_date"),
                    "content_length": len(c.get("content_md", "")),
                    "preview": _preview(c.get("content_md", ""), 300),
                }
                for c in silver_chunks
            ],
            "retrieved_chunks": [
                {
                    "asset_id": c.get("asset_id"),
                    "source_type": c.get("source_type"),
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
            "total_cost_usd": total_cost,
            "total_tokens": total_tokens,
        }
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        trace_path.write_text(json.dumps(trace, indent=2, default=str))

    return output_path
