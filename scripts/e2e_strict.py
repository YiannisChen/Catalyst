#!/usr/bin/env python3
"""STRICT full-chain E2E acceptance validation.

Every stage is real — no mocks, no bypasses, no pre-existing data.

Flow:
  1. NLP input request
  2. data-core pipeline: real connectors → ingest → clean → transform → Bronze/Silver (fresh DB)
  3. Build real LanceDB Gold index from freshly-stored Silver data
  4. Real hybrid search (BM25 + vector → RRF) via project retrieval path
  5. MCJ pipeline (Miner → Critic → Judge) with Gemini 2.5 Flash (live API)
  6. Eval scoring against golden set v1

Usage:
    python scripts/e2e_strict.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "packages" / "data-core"))
sys.path.insert(0, str(PROJECT_ROOT / "packages" / "agents"))
sys.path.insert(0, str(PROJECT_ROOT / "packages" / "eval"))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / "packages" / "data-core" / ".env")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TICKER = "NVDA"
TRADE_DATE = "2026-04-03"
NLP_INPUT = f"Analyze {TICKER}'s price move attribution on {TRADE_DATE}."
SOURCES = ["polygon_news", "polygon_ohlcv", "fmp_fundamentals"]
GEMINI_MODEL = "gemini-2.5-flash"
GOLDEN_SET_PATH = PROJECT_ROOT / "packages" / "eval" / "golden_set" / "v1.jsonl"

# Fresh paths — separate from dev data to prove this run produced the data
E2E_DATA_DIR = PROJECT_ROOT / "data" / "e2e_strict"
E2E_DB_PATH = E2E_DATA_DIR / "e2e_assets.db"
E2E_LANCEDB_PATH = E2E_DATA_DIR / "lancedb_gold"

# ---------------------------------------------------------------------------
# Report buffer
# ---------------------------------------------------------------------------
_report: list[str] = []
_commands: list[str] = []


def log(msg: str = ""):
    print(msg)
    _report.append(msg)


def cmd(description: str):
    _commands.append(description)


# ===================================================================
# STAGE 1: Pre-flight checks
# ===================================================================
def preflight() -> bool:
    """Verify all required API keys are present."""
    required = {
        "GEMINI_API_KEY": "LLM (Gemini 2.5 Flash)",
        "POLYGON_API_KEY": "Polygon news + OHLCV connector",
        "FMP_API_KEY": "FMP fundamentals connector",
    }
    all_ok = True
    for key, purpose in required.items():
        val = os.environ.get(key, "")
        if val:
            log(f"  - {key}: SET ({len(val)} chars) — {purpose}")
        else:
            log(f"  - {key}: **NOT SET** — {purpose}")
            all_ok = False
    return all_ok


# ===================================================================
# STAGE 2: Real data-core pipeline (ingest → clean → transform → store)
# ===================================================================
async def run_data_pipeline() -> tuple[list[dict], sqlite3.Connection]:
    """Execute the full data-core pipeline with real connectors.

    Creates a fresh SQLite DB, fetches from real APIs, and stores
    Bronze + Silver assets. Returns pipeline summaries and the open connection.
    """
    from catalyst_data.storage.sqlite import init_db
    from catalyst_data.orchestrator import process_request
    from catalyst_data.config import RATE_POLICIES
    from catalyst_data.rate_limiter import TokenBucketLimiter
    from catalyst_data.connectors.polygon import create_polygon_fetcher
    from catalyst_data.connectors.fmp import create_fmp_fetcher
    from catalyst_data.connectors.base import FetchResult
    import httpx

    # Fresh database
    E2E_DATA_DIR.mkdir(parents=True, exist_ok=True)
    if E2E_DB_PATH.exists():
        E2E_DB_PATH.unlink()
    conn = sqlite3.connect(str(E2E_DB_PATH))
    init_db(conn)
    cmd(f"Created fresh database at {E2E_DB_PATH}")

    # Build connectors with real API keys
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

    cmd(f"Calling process_request(ticker={TICKER}, date={TRADE_DATE}, sources={SOURCES})")

    try:
        summaries = await process_request(
            ticker=TICKER,
            date=TRADE_DATE,
            sources=SOURCES,
            conn=conn,
            fetch_fn=fetch,
        )
    finally:
        await client.aclose()

    return summaries, conn


# ===================================================================
# STAGE 3: Build LanceDB Gold index from fresh Silver data
# ===================================================================
def build_gold_index(conn: sqlite3.Connection):
    """Build real LanceDB Gold from the freshly-stored Silver rows."""
    import lancedb
    from sentence_transformers import SentenceTransformer

    # Read Silver data from this run's database
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT asset_id, ticker, source_type, reference_date, content_md "
        "FROM clean_assets WHERE ticker = ? AND is_duplicate = 0 AND LENGTH(content_md) > 0",
        (TICKER,),
    ).fetchall()
    conn.row_factory = None
    chunks = [dict(r) for r in rows]

    if not chunks:
        return None, None, chunks

    embedding_model = "all-MiniLM-L6-v2"
    log(f"- Loading embedding model ({embedding_model})...")
    t0 = time.monotonic()
    model = SentenceTransformer(embedding_model)
    load_ms = (time.monotonic() - t0) * 1000
    log(f"- Model loaded in **{load_ms:.0f} ms**")

    texts = [c["content_md"] for c in chunks]
    log(f"- Embedding {len(texts)} Silver chunks...")
    t0 = time.monotonic()
    vectors = model.encode(texts, show_progress_bar=False, normalize_embeddings=True)
    embed_ms = (time.monotonic() - t0) * 1000
    log(f"- Embeddings computed in **{embed_ms:.0f} ms** (dim={vectors.shape[1]})")

    records = []
    for i, chunk in enumerate(chunks):
        records.append({
            "asset_id": chunk["asset_id"],
            "ticker": chunk["ticker"],
            "source_type": chunk["source_type"],
            "reference_date": chunk["reference_date"],
            "content_md": chunk["content_md"],
            "vector": vectors[i].tolist(),
        })

    lancedb_dir = Path(E2E_LANCEDB_PATH)
    lancedb_dir.mkdir(parents=True, exist_ok=True)
    db = lancedb.connect(str(lancedb_dir))
    if "chunks" in db.table_names():
        db.drop_table("chunks")
    table = db.create_table("chunks", data=records)
    table.create_fts_index("content_md", replace=True)
    log(f"- LanceDB Gold table: **{len(records)}** chunks indexed at {lancedb_dir}")
    cmd(f"Built LanceDB Gold index with {len(records)} chunks")

    def embedding_fn(text: str) -> list[float]:
        vec = model.encode([text], normalize_embeddings=True)
        return vec[0].tolist()

    return table, embedding_fn, chunks


# ===================================================================
# STAGE 4: Gemini LLM adapter
# ===================================================================
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


# ===================================================================
# STAGE 5: MCJ pipeline
# ===================================================================
def run_mcj_pipeline(table, embedding_fn, llm):
    from catalyst_agents.graph import build_attribution_graph

    graph = build_attribution_graph(
        use_critic=True,
        table=table,
        embedding_fn=embedding_fn,
        reranker=None,
        llm=llm,
    )

    initial_state = {
        "ticker": TICKER,
        "trade_date": TRADE_DATE,
        "query": NLP_INPUT,
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
        "model_id": "gemini-2.5-flash",
    }

    cmd("Running MCJ graph: Miner → Critic → Judge")
    return graph.invoke(initial_state)


# ===================================================================
# STAGE 6: Eval scoring
# ===================================================================
def run_eval(result, reranked):
    from catalyst_eval.schema.result import AttributionResult, PredictedCause
    from catalyst_eval.metrics import (
        AttributionF1, CategoryAccuracy, GroundingRate,
        TemporalPrecision, ConfidenceCalibration,
    )
    from catalyst_eval.schema.golden_event import GoldenEvent

    causes = result.get("causes", [])
    breakdown = result.get("cost_breakdown", [])

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

    golden = None
    if GOLDEN_SET_PATH.exists():
        with open(GOLDEN_SET_PATH) as f:
            for line in f:
                line = line.strip()
                if line:
                    ge = GoldenEvent(**json.loads(line))
                    if ge.ticker == TICKER:
                        golden = ge
                        break

    metrics = [
        AttributionF1(),
        CategoryAccuracy(),
        GroundingRate(),
        TemporalPrecision(),
        ConfidenceCalibration(),
    ]

    return attr_result, golden, metrics


# ===================================================================
# Main
# ===================================================================
async def main():
    wall_t0 = time.monotonic()

    log("# Strict Full-Chain E2E Acceptance Report")
    log(f"\n**Date:** {time.strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"**Ticker:** {TICKER}  |  **Trade date:** {TRADE_DATE}")
    log(f"**NLP Input:** \"{NLP_INPUT}\"")
    log(f"**LLM:** {GEMINI_MODEL} (live Gemini API)  |  **Embedding:** all-MiniLM-L6-v2")
    log(f"**Reranker:** None (RRF fallback)  |  **Database:** Fresh (not reusing dev_assets.db)")
    log()

    # ---- Preflight ----
    log("## 0. Pre-flight Checks")
    if not preflight():
        log("\n**BLOCKER:** Missing required API keys. Cannot proceed.")
        return
    log()

    # ---- Stage 2: Data pipeline ----
    log("## 1. Data Pipeline (Real Connectors → Bronze → Silver)")
    log(f"- Sources: {SOURCES}")
    log(f"- Target: {E2E_DB_PATH} (fresh, created this run)")
    log()

    t0 = time.monotonic()
    try:
        summaries, conn = await run_data_pipeline()
        pipeline_ms = (time.monotonic() - t0) * 1000
    except Exception as exc:
        pipeline_ms = (time.monotonic() - t0) * 1000
        log(f"**BLOCKER:** Data pipeline failed after {pipeline_ms:.0f} ms: {exc}")
        import traceback
        log(f"\n```\n{traceback.format_exc()}\n```")
        return

    log(f"### Pipeline Results ({pipeline_ms:.0f} ms)")
    ingest_ok = 0
    ingest_fail = 0
    for s in summaries:
        status = "OK" if s.get("ok") else "FAIL"
        if s.get("ok"):
            ingest_ok += 1
        else:
            ingest_fail += 1
        asset_id = s.get("asset_id", "N/A")
        error = f" — {s.get('error', '')}" if s.get("error") else ""
        latencies = s.get("stage_latencies", {})
        lat_str = ", ".join(f"{k}={v:.1f}ms" for k, v in latencies.items()) if latencies else ""

        log(f"- **{s.get('source', '?')}**: [{status}] asset_id=`{str(asset_id)[:16]}...`{error}")
        if lat_str:
            log(f"  - Stage latencies: {lat_str}")

        # Report endpoint details
        ep_statuses = s.get("endpoint_statuses", {})
        failed_eps = s.get("failed_endpoints", {})
        if ep_statuses:
            for ep, st in ep_statuses.items():
                ep_status = "OK" if st == 200 else f"HTTP {st}"
                fail_detail = ""
                if ep in failed_eps:
                    fail_detail = f" — {failed_eps[ep].get('error', '')}"
                log(f"  - endpoint `{ep}`: {ep_status}{fail_detail}")
    log()

    # Verify Bronze/Silver counts
    raw_count = conn.execute("SELECT COUNT(*) FROM raw_assets").fetchone()[0]
    clean_count = conn.execute("SELECT COUNT(*) FROM clean_assets").fetchone()[0]
    log(f"### Storage Verification")
    log(f"- Bronze (raw_assets): **{raw_count}** rows")
    log(f"- Silver (clean_assets): **{clean_count}** rows")

    # Show Silver data details
    conn.row_factory = sqlite3.Row
    silver_rows = conn.execute(
        "SELECT asset_id, ticker, source_type, reference_date, LENGTH(content_md) as md_len, is_duplicate "
        "FROM clean_assets WHERE ticker = ?",
        (TICKER,),
    ).fetchall()
    conn.row_factory = None

    for r in silver_rows:
        dup_tag = " [DUPLICATE]" if r["is_duplicate"] else ""
        log(f"  - `{r['asset_id'][:16]}...` {r['source_type']:25s} {r['reference_date']} ({r['md_len']:,} chars){dup_tag}")

    if clean_count == 0:
        log(f"\n**BLOCKER:** No Silver data stored. Cannot proceed to Gold/Agent stages.")
        conn.close()
        return
    log()

    # ---- Stage 3: Gold index ----
    log("## 2. LanceDB Gold Index (Real Embeddings from Fresh Silver)")
    t0 = time.monotonic()
    try:
        table, embedding_fn, chunks = build_gold_index(conn)
        gold_ms = (time.monotonic() - t0) * 1000
    except Exception as exc:
        gold_ms = (time.monotonic() - t0) * 1000
        log(f"**BLOCKER:** Gold index build failed after {gold_ms:.0f} ms: {exc}")
        conn.close()
        return

    if table is None:
        log("**BLOCKER:** No non-duplicate Silver chunks to index.")
        conn.close()
        return

    log(f"- Total Gold index build: **{gold_ms:.0f} ms**")
    log()

    # ---- Stage 4: LLM init ----
    log("## 3. LLM Initialization")
    try:
        llm = GeminiLLM(model=GEMINI_MODEL, temperature=0.0)
        log(f"- **{GEMINI_MODEL}** initialized (live API, temperature=0.0)")
    except Exception as exc:
        log(f"**BLOCKER:** LLM init failed: {exc}")
        conn.close()
        return
    log()

    # ---- Stage 5: MCJ pipeline ----
    log("## 4. Agent Workflow (Miner → Critic → Judge)")
    log(f"- Input query: \"{NLP_INPUT}\"")
    t0 = time.monotonic()
    try:
        result = run_mcj_pipeline(table, embedding_fn, llm)
        mcj_ms = (time.monotonic() - t0) * 1000
        log(f"- Pipeline completed in **{mcj_ms:.0f} ms**")
    except Exception as exc:
        mcj_ms = (time.monotonic() - t0) * 1000
        log(f"**BLOCKER:** Pipeline failed after {mcj_ms:.0f} ms: {exc}")
        import traceback
        log(f"\n```\n{traceback.format_exc()}\n```")
        conn.close()
        return
    log()

    # ---- Retrieval evidence ----
    log("### 4a. Retrieval Evidence (Real LanceDB Hybrid Search)")
    retrieved = result.get("retrieved_chunks", [])
    reranked = result.get("reranked_chunks", [])
    log(f"- Hybrid search (BM25 + Vector → RRF) returned: **{len(retrieved)}** chunks")
    log(f"- After RRF top-8 cutoff: **{len(reranked)}** reranked chunks")
    for c in reranked:
        log(f"  - `{c.get('asset_id', '?')[:16]}...` {c.get('source_type', '?'):25s} rrf={c.get('rrf_score', 0):.6f}")
    log()

    # ---- Critic output ----
    log("### 4b. Critic Output (Real Gemini LLM Call)")
    graded = result.get("graded_evidence", [])
    log(f"- Graded evidence: **{len(graded)}** chunks passed relevance > 0.5")
    for g in graded:
        log(f"  - `{g.get('chunk_id', '?')[:16]}...` relevance={g.get('relevance', 0):.2f} cat={g.get('category', '?')} temporal={g.get('temporal_match', '?')}")
    log(f"- Critic reasoning: {result.get('critic_reasoning', 'N/A')[:400]}")
    log()

    # ---- Judge output ----
    log("### 4c. Final Attribution Output (Real Gemini LLM Call)")
    causes = result.get("causes", [])
    log(f"- **{len(causes)} causes** identified:")
    for i, c in enumerate(causes, 1):
        log(f"  {i}. **[{c.get('category', '?')}]** {c.get('text', '?')}")
        log(f"     confidence={c.get('confidence', 0):.2f} | direction={c.get('direction', '?')} | evidence={c.get('evidence_ids', [])}")
    log()
    log(f"**Summary:**")
    log(f"> {result.get('summary_md', 'N/A')}")
    log(f"\n**Pipeline grounding rate:** {result.get('grounding_rate', 'N/A')}")
    log()

    # ---- Cost/timing ----
    log("### 4d. Cost & Timing")
    breakdown = result.get("cost_breakdown", [])
    for entry in breakdown:
        log(f"- {entry.get('node', '?')}: {entry.get('input_tokens', 0):,} in + {entry.get('output_tokens', 0):,} out = ${entry.get('cost_usd', 0):.6f}")
    log(f"- **Total tokens:** {result.get('total_tokens', 0):,}")
    log(f"- **Total cost:** ${result.get('total_cost_usd', 0):.6f}")
    log(f"- **MCJ pipeline latency:** {mcj_ms:.0f} ms")
    log(f"- **Data pipeline latency:** {pipeline_ms:.0f} ms")
    log(f"- **Gold index build:** {gold_ms:.0f} ms")
    log()

    # ---- Stage 6: Eval scoring ----
    log("## 5. Evaluation Scores")
    attr_result, golden, metrics = run_eval(result, reranked)
    if golden is None:
        log(f"- **SKIP:** No golden event for {TICKER}.")
    else:
        log(f"- Golden event: `{golden.id}` {golden.ticker} {golden.trade_date} ({golden.price_move_pct:+.2f}%)")
        log(f"- **Note:** golden trade_date={golden.trade_date} vs predicted={TRADE_DATE} (date mismatch expected)")
        log()
        log("| Metric | Score |")
        log("|--------|-------|")
        for m in metrics:
            score = m.compute(attr_result, golden)
            log(f"| {m.name} | **{score:.4f}** |")
    log()

    # ---- Failure points ----
    log("## 6. Failure Points & Residual Risks")
    log()
    log("| Risk | Severity | Notes |")
    log("|------|----------|-------|")
    if ingest_fail > 0:
        log(f"| {ingest_fail} source(s) failed ingest | Medium | See pipeline results above |")
    log("| No cross-encoder reranker | Medium | bge-reranker-v2 not used; RRF ordering only |")
    log("| Embedding model: all-MiniLM-L6-v2 | Low | 384d instead of bge-m3 1024d; CPU-practical; production uses bge-m3 on GPU |")
    log("| Golden set date mismatch | Medium | v1.jsonl golden=2025-01-27, data=2026-04-03 |")
    if not graded:
        log("| All evidence filtered by Critic | High | Insufficient evidence path triggered |")
    gr = result.get("grounding_rate")
    if gr is not None and gr < 0.5:
        log("| Low grounding rate | High | Judge may have cited non-matching evidence IDs |")
    log()

    # ---- Commands run ----
    log("## 7. Commands & Operations Executed")
    for i, c in enumerate(_commands, 1):
        log(f"{i}. {c}")
    log()

    # ---- Verdict ----
    wall_total = (time.monotonic() - wall_t0) * 1000
    log("## 8. Verdict")
    log()

    stage_checklist = {
        "NLP input provided": True,
        "Real connector fetch (Polygon/FMP)": ingest_ok > 0,
        "Bronze storage (raw_assets)": raw_count > 0,
        "Silver storage (clean_assets)": clean_count > 0,
        "LanceDB Gold index built": table is not None,
        "Real hybrid search (BM25+Vector→RRF)": len(retrieved) > 0,
        "Real Critic LLM call (Gemini)": len(breakdown) >= 1,
        "Real Judge LLM call (Gemini)": len(breakdown) >= 2,
        "AttributionResult produced": len(causes) > 0,
        "Eval scoring completed": golden is not None,
    }

    all_pass = all(stage_checklist.values())

    log("| Stage | Status |")
    log("|-------|--------|")
    for stage, ok in stage_checklist.items():
        log(f"| {stage} | {'PASS' if ok else '**FAIL**'} |")
    log()
    log(f"**Wall-clock time:** {wall_total / 1000:.1f}s")
    log()

    if all_pass:
        log("### **FULL E2E VALIDATED**")
        log()
        log("All stages executed with real components in a single chain:")
        log(f"- Fresh database created and populated via real API calls ({ingest_ok}/{len(SOURCES)} sources)")
        log("- Real LanceDB Gold index built from freshly-ingested Silver data")
        log("- Real hybrid search (BM25 + vector → RRF fusion)")
        log(f"- Real LLM inference ({GEMINI_MODEL} via Google GenAI API)")
        log("- Real eval scoring against golden set v1")
    else:
        log("### **NOT YET VALIDATED**")
        log()
        log("Blockers:")
        for stage, ok in stage_checklist.items():
            if not ok:
                log(f"- **{stage}** did not pass")

    # ---- Save report ----
    report_dir = PROJECT_ROOT / "data" / "eval_reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "e2e_strict_acceptance_report.md"
    report_text = "\n".join(_report)
    report_path.write_text(report_text)
    print(f"\nReport saved to {report_path}")

    conn.close()


if __name__ == "__main__":
    asyncio.run(main())
