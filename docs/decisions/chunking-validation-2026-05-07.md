# T03-preb Chunking Validation & Final Parameter Lock

Date: 2026-05-07  
Scope: T03-preb decision lock only. No T03 pipeline implementation in this task.

## Inputs
- Golden set (10 representative queries): `packages/eval/golden_set/v1_2_p0_set.jsonl`
- L1 index: `data/lancedb_gold/eval_frozen`  
  (`lancedb_dir_sha256 = 006d35397ce6086cedf9f046b1a5ac74a30ef68dc27a00d301269847ae03d09d`)
- DB: `data/catalyst_eval_frozen_v2.db`

## 1) 10-query RRF candidate validation (real retrieval distribution)

Method:
- Query text: `case.query` if present, else template `Explain the price move for {ticker} on {trade_date}.`
- Real embedding: `BAAI/bge-m3` query embedding.
- Direct layer candidate pool: `(vector_top20 ∪ fts_top20)` under `ticker + trade_date±3d`, filtered by direct source types.
- Macro layer candidate pool: `(vector_top20 ∪ fts_top20)` under `trade_date±3d` (no ticker), filtered by macro source types.
- Per-query RRF candidate count = `|direct ∪ macro|`.

Result summary:
- Feasibility rule: `combined_rrf_candidates >= 20` in at least 80% queries.
- Observed: `3/10 = 30%` (fails the 80% rule).

Per-query:

| case | ticker | direct | macro | combined | >=20 |
|---|---|---:|---:|---:|---|
| g006 | TSLA | 10 | 9 | 19 | no |
| g013 | NVDA | 10 | 7 | 17 | no |
| g017 | AMD | 10 | 5 | 15 | no |
| g024 | UNH | 9 | 11 | 20 | yes |
| g041 | AMZN | 10 | 6 | 16 | no |
| g005 | TSLA | 10 | 10 | 20 | yes |
| g007 | TSLA | 10 | 10 | 20 | yes |
| g001 | TSLA | 9 | 10 | 19 | no |
| g009 | NVDA | 10 | 6 | 16 | no |
| p0r001 | OPENAI | 0 | 0 | 0 | no |

Interpretation:
- In frozen-window retrieval (`±3d`), corpus sparsity and strict filtering make `top-k=20` frequently unattainable.
- This is a runtime behavior issue, not a reason to drop L2 scope.

## 2) 5-case long-article manual truncation audit (>=30 sentences)

Audit set (`polygon_news`, NLTK `punkt_tab` sentence split):
- `91638df1...` (125 sents)
- `fcbacc87...` (119 sents)
- `2253975a...` (117 sents)
- `0c7b15d7...` (45 sents)
- `17e4e010...` (45 sents)

Manual findings:
- 5/5 cases contain new headline-level evidence blocks in sentences 21-30.
- `cap=20` truncates these additional blocks in all 5 audited cases.
- `cap=30` materially reduces truncation risk while avoiding the larger growth jump of `cap=50`.

Conclusion:
- `max_sentences_per_asset` should be raised from candidate-20 to final-30.

## 3) Final lock values (for T03 coding)

| Parameter | Final lock | Reason | Adjustment condition |
|---|---|---|---|
| `splitter` | `nltk_punkt_tab` | Lowest pathological rate in pre-a and stable on markdown-heavy news text. | Re-open only if T03-preb replay shows >10% evidence-loss vs spaCy on frozen eval. |
| `max_sentences_per_asset` | `30` | 5/5 long-doc audit shows cap=20 truncates meaningful tail evidence. | Increase to 50 only if >=5% frozen cases still lose evidence beyond sentence 30; decrease to 20 only if latency/storage breach with no measurable quality loss. |
| `min_sentence_length` | `12` | Removes very short noise while preserving short but meaningful clauses/titles. | Raise to 20 only if >15% produced L2 rows are judged noise in T03 QA sample. |
| `L2 eligible source_types` | `['polygon_news']` | Prose-heavy and highest immediate benefit; keeps blast radius controlled. | Add `fred_macro` only if T03-preb shows macro evidence recall gap not solved by L1. |
| `reranker_top_k` | `8` | Aligns with existing ADR/data-core contract and current Miner behavior. | Raise to 12 only if critic-stage false-negative rate remains elevated after L2 rollout. |
| `guardrail` | `effective_top_k = min(20, available_rrf_candidates)` + mandatory fallback logging | Current frozen retrieval fails 80% rule for fixed-20 feasibility; runtime must degrade gracefully. | Remove guardrail only after revalidation shows `>=20 candidates` in >=80% production-like queries. |

## 4) Go/No-Go for T03 coding

Decision: **GO (with locked guardrail above).**

Rationale:
- Parameter lock is complete and actionable.
- `top-k=20` infeasibility is explicitly handled by the guardrail, without changing P1/P2 scope or Lance schema.
- No evidence in this pre-b step justifies deferring L2 to P2.
