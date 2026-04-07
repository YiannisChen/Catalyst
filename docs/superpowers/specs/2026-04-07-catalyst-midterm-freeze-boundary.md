# Catalyst Midterm Freeze Boundary

**Date:** 2026-04-07
**Status:** Proposed freeze decision
**Scope:** April 10, 2026 midterm demo
**Reviewed against:** [README](../../../README.md), [2026-04-02 Catalyst System Design Spec](2026-04-02-catalyst-system-design.md)

---

## 1. Midterm Objective

Freeze Catalyst as a **script-driven research prototype** that can demonstrate one complete attribution loop for a single stock/date pair:

1. fetch live financial evidence,
2. persist Bronze and Silver artifacts,
3. build a Gold retrieval index,
4. run the Miner-Critic-Judge workflow,
5. score the result with the evaluation harness, and
6. present a concrete acceptance report for the demo.

This is the midterm boundary. It is narrower than the full product vision described in the README and system design spec.

## 2. Boundary Statement

The midterm version is **not** the full Catalyst product. It is the combination of:

- `catalyst-data` as the ingestion and storage layer,
- `catalyst-eval` as the scoring and comparison layer,
- `catalyst-agents` as the MCJ attribution layer,
- repo-level scripts and reports that prove the end-to-end chain works.

For midterm purposes, Catalyst should be described as a **validated package stack with a canonical demo script**, not as a finished platform with API, MCP, or frontend surfaces.

## 3. In-Scope Deliverables

The following are considered **working and demo-ready** for the midterm freeze:

- Bronze/Silver ingestion for the canonical demo sources: `polygon_news`, `polygon_ohlcv`, and `fmp_fundamentals`.
- SQLite schema initialization and persistence for `raw_assets` and `clean_assets`.
- Gold indexing over Silver assets via LanceDB, with hybrid retrieval support.
- Miner-Critic-Judge execution through `packages/agents`.
- Baseline ablation capability (`Miner -> Judge`) versus full MCJ (`Miner -> Critic -> Judge`) at the graph level.
- Evaluation harness with the five implemented metrics: attribution F1, category accuracy, grounding rate, temporal precision, and confidence calibration.
- Markdown comparison/report generation in `catalyst-eval`.
- A canonical live acceptance path via [scripts/e2e_strict.py](../../../scripts/e2e_strict.py).
- Existing evidence artifacts showing end-to-end execution, especially [data/eval_reports/e2e_strict_acceptance_report.md](../../../data/eval_reports/e2e_strict_acceptance_report.md).

The following targeted package-level verification runs were executed during this review:

- `packages/data-core`: targeted suite passed (`25 passed`).
- `packages/eval`: targeted suite passed (`39 passed`).
- `packages/agents`: targeted suite passed (`90 passed`).
- Total from those targeted package-level verification runs: `154` passing tests.

## 4. Partially Implemented But Non-Blocking

These items exist in some form, but they should **not block the midterm freeze**:

- `fred_macro` and `yfinance_fundamentals` are wired into source selection and smoke-test flows, but they are not required for the canonical midterm demo.
- Trading-day alignment logic exists in [packages/data-core/catalyst_data/pipeline/align.py](../../../packages/data-core/catalyst_data/pipeline/align.py), and `news_alignment` / `ohlcv` tables exist in SQLite schema, but the current end-to-end orchestrator does not use them as part of the canonical demo path.
- The comparison infrastructure exists in [packages/eval/catalyst_eval/harness/experiment.py](../../../packages/eval/catalyst_eval/harness/experiment.py) and the graph supports `use_critic=True/False`, but [scripts/run_experiments.py](../../../scripts/run_experiments.py) is not a one-command live demo runner because it still expects retrieval dependencies to be injected by the caller.
- The Gold layer code targets `BAAI/bge-m3` and supports a reranker interface, but the strict acceptance flow currently uses `all-MiniLM-L6-v2` and no cross-encoder reranker.

## 5. Out of Scope for Midterm

The following must be treated as **deferred to final/thesis**, even if they appear in the broader design:

- `packages/mcp` / `catalyst-mcp`.
- Any FastAPI backend or integrated API surface.
- Any `web/` React frontend or dashboard work.
- The integrated `catalyst` application package described in the system design spec.
- MCP, Claude Desktop, or assistant integration polish.
- Additional providers not needed for the canonical midterm demo: GDELT, SEC EDGAR, Finnhub, and related enrichment flows.
- Full backfill, production cron, daily operations, and scaling work.
- LangSmith tracing, guardrail nodes, checkpointing, and production observability beyond what already exists.
- Thesis-level evaluation completeness, large-sample experiment stability, and polished publishable packaging.

## 6. Known Limitations

The frozen midterm version has the following accepted limitations:

- The strict end-to-end report currently uses `all-MiniLM-L6-v2` embeddings and **no reranker**, so it does not fully realize the spec's target retrieval stack.
- The canonical live acceptance example is narrow: one ticker, one trade date, and a three-chunk corpus.
- The current acceptance report scores against a golden event with a date mismatch, so evaluation numbers in that report are evidence of pipeline execution, not thesis-grade quality claims.
- The demo is script-driven and operator-led; it is not yet a user-facing product.
- Live demo execution depends on external API keys and upstream provider availability.
- The README describes the end-state product more broadly than the midterm implementation actually supports.

## 7. Required Demo Path

The required midterm demo should follow this exact boundary:

1. Confirm `POLYGON_API_KEY`, `FMP_API_KEY`, and `GEMINI_API_KEY` are set.
2. Run the canonical acceptance script: `python scripts/e2e_strict.py`.
3. Show that the run creates a fresh SQLite database and fresh LanceDB index.
4. Show Bronze/Silver rows, hybrid retrieval output, Critic filtering, Judge synthesis, and eval scoring.
5. Use [data/eval_reports/e2e_strict_acceptance_report.md](../../../data/eval_reports/e2e_strict_acceptance_report.md) as the primary demo artifact.
6. State explicitly that the demo proves the package stack and end-to-end attribution workflow, not a completed product surface.

If a live rerun is blocked by temporary provider or network instability, previously generated acceptance artifacts may be used as evidence, but that fallback does **not** expand the claimed scope.

## 8. Freeze Criteria

The midterm version should be considered **frozen** only when all of the following are true:

- No new product features are added before the demo.
- The project team agrees that the midterm claim is limited to the frozen boundary in this document.
- The canonical package stack remains `data-core + eval + agents`, not API/frontend/MCP.
- The targeted package-level verification suites run for those three packages are passing locally.
- The canonical demo path remains runnable in principle and supported by the current acceptance artifacts.
- Any discussion of missing API, frontend, MCP, broader provider coverage, or thesis-level evaluation is framed as post-midterm work.

## 9. Midterm Boundary Summary

**Freeze this:** a validated, package-level causal attribution prototype with a script-based end-to-end demo.

**Do not freeze this:** the full product platform envisioned in the README and long-form system design.
