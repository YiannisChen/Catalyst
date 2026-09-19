# M8-B FTS Ceiling and Dense/Hybrid Cutover (Amendment)

**Status:** binding amendment to `docs/plans/2026-09-18-m8-executor-lock.md`.
**Author:** Grok (reviewer), recorded by dscodex executor.
**Applies to:** worktree `/Users/yiannischen/Desktop/Catalyst-v1.1-m8`, branch
`v1.1/m8-causal-quality-recovery`.
**Authority:** the executor lock, then this amendment, then the recovery design,
then the implementation plan TDD steps, then the original M8 cleanup plan.

Recorded at executor HEAD `c9528f8` (`fix(eval): pick M8-B display ordinals
inside the identified documents`).

---

## 1. M8-B FTS is sealed FAIL

`v1.1/m8-causal-quality-recovery` at `c9528f8`; candidate build
`bec845b416114d412ccd1ce15888b8294ce4ebda5287be3ff03366f50f54d8e1`,
corpus manifest `4a022928b60ae530e55ac1cc3f064172c848754e810fa3e706bca2f5c8c98316`,
derivative `data/snapshots/catalyst_m8a_derivative_7a004acc.db` (sha256
`6925858d…`), candidate FTS digest `8c83406a…`, `is_current=0`.

Sealed M8-B reports (all three are terminal evidence; do not overwrite).
Recall@8 is a macro mean over the nine scorable cases, so `numerator/denominator`
and `value` are reported separately; the value column is the metric's own
`value` field.

| Report | Policy | Recall@8 | primary hit | dup-adj P@8 |
|---|---|---|---|---|
| `…bec845b4.json` | citable slots, unranked | 2/29, value 0.056 | 0/9, value 0.000 | 1/88, value 0.011 |
| `…bec845b4_ftdisplay.json` | + class-tier display order | 10/29, value 0.289 | 2/9, value 0.222 | 3/44, value 0.068 |
| `…bec845b4_sectiondisplay.json` | + shared within-document policy | 9/29, value 0.294 | 5/9, value 0.556 | 6/26, value 0.231 |

Numerators for the final (`_sectiondisplay`) run, with `candidate_depth=100`:

- Recall@8 **9/29**, value **0.294** (gate ≥ 0.75) — FAIL
- primary-source hit **5/9**, value **0.556** (gate ≥ 0.80) — FAIL
- duplicate-adjusted Precision@8 **6/26**, value **0.231** (gate ≥ 0.60) — FAIL
- required-primary non-empty pack **8/9**, missing `c01` only (not c03/c04/c12)
  — FAIL
- ticker/cutoff violations **0** — PASS

**M8-B FTS is finished.** No further lexical ranking change is authorized on
this candidate. Another FTS-only knife would be the third heuristic pass; it is
refused.

## 2. Cause: price-move questions against long filing tables

The within-document policy did what it claimed: XBRL scaffolding, cover-page
boilerplate and litigation boilerplate stopped taking slots, and results tables
started taking them (c02/c05/c09 previously displayed `0001/0018/0030…`
scaffolding; they now display statement/segment tables). It still could not
recover the human gold ordinals.

- The benchmark questions are price-move questions ("Why did MSFT jump on
  2025-05-01?"), not table-lookup questions. Nothing in the question, ticker or
  cutoff names a revenue line, an EPS figure or a page.
- A 10-Q/8-K body is 190–372 chunks per document; hundreds of chunks are
  revenue-bearing prose, notes and segment tables. Public keyword scoring cannot
  separate "the chunk the human cited" from its neighbours: the cited and
  uncited chunks are frequently the same content region (e.g. c05:0048–0050),
  differ only by the table rows that fell inside the chunk window, and include
  prose citations (c08:0161, c10:0012) with no distinctive marker.
- Expansion proved this is not a window problem: every reachable gold ordinal
  (0072/0073, 0094/0117, 0048–0050, 0041/0058, 0001/0002, 0004, 0015) was
  present in the expanded `FULL_TEXT` document set and was scored; it simply
  ranked below other results chunks.
- The ceiling is structural, not a tuning miss. An offline sweep over the
  mandated marker family (same window, same expansion, same ≤3-per-document
  cap, same 8 slots) tops out near Recall 0.37 / primary 0.78 / precision 0.26,
  versus 0.75/0.80/0.60 required. Chunk-level gold here is a semantic judgement
  over a long table corpus; lexical selection is the wrong instrument.

`STAGE1_RETRIEVAL_GATES` (`recall_at_8` 0.75, `primary_source_hit` 0.80,
`duplicate_adjusted_precision` 0.60, `no_ticker_or_cutoff_violations` 0.0) and
`evaluate_recovery_retrieval_gates` are **unchanged** by this amendment. M8-B's
formulas are still the gate; only the retrieval path changes.

## 3. M8-C FTS-only probe is not a GPU precondition

The executor lock previously chained M8-B → M8-C → M8-D. That chain is broken
here:

- M8-C's own first gate is "c01 non-ABSTAIN". On the current FTS packs c01 has
  an empty citable pack (`required_primary_citable` FAIL, missing `c01`), so the
  probe would fail on a retrieval defect, not on Analyst behaviour. Running it
  would spend provider budget to measure a known-empty pack.
- M8-C therefore runs **after** hybrid retrieval exists, on the packs hybrid
  produces. It is not a GPU precondition and is not a precondition for M8-D.

## 4. Next retrieval path: M8-D dense/hybrid on THIS candidate

M8-D is the remaining retrieval path. It is still gated by `GPU_OK=yes` in the
prompt; this amendment does not authorize GPU work by itself.

1. Embed the **new** candidate's `to_embed` set: **374,179 `FULL_TEXT` bodies**
   of build `bec845b4…` (verified: 536,613 chunks total = 374,179 `FULL_TEXT` +
   159,982 `METADATA_ONLY` + 2,452 `TITLE_ONLY`, 167,666 documents), pinned
   BGE-M3 `5617a9f61b028005a4858fdac845db406aefb181`, dim 1024, new vectors.
2. **Do not reuse the Q-011 LanceDB** or its 374,179 vectors even though the
   count matches: Q-011 is audit-only (`bc80d9bc…`), and its vector identity,
   manifest, and derivative provenance are not this candidate's.
3. **Hybrid retrieval contract** (replaces FTS-only ranking; no gold ids):
   - ticker/cutoff eligibility filters first (unchanged semantics);
   - FTS identifies documents (window at the FTS cap, floor 50);
   - **dense ranks chunks**, including eligible `FULL_TEXT` outside the FTS
     window. This is required for c01: its live evidence is
     `34e2da1cb7dfd931541c33a78873ddfa79820960654b409de82001a72657e0b1`
     (`news_v2`, `official_government`, available 2025-05-12T07:01:01Z, the
     US–China Geneva joint statement), which is present as `FULL_TEXT` in this
     candidate but never entered c01's FTS window;
   - display cap **≤ 3 chunks per document**, `FULL_TEXT` slots only, no
     unscored padding, no gold fields anywhere in retrieval.
4. Promote only after **both** hold:
   - the inactive hybrid tuple passes the unchanged M8-B formulas on the same
     12-case frozen scoring path; and
   - a Stage-1 attribution run under a **new `eval_id`** (new output dir, never
     `2792d333…` / `v1_1_stage1_327b3eed`) passes the recovery gates.

## 5. c01 is not a gold problem

c01's expected evidence ids
(`5fefbba59f328481a31dbdcc2e44d37d0f503e77684c5d39b9d947277d49c2b6:news_v2:body:0004/0005`)
do not exist in the candidate build; c01 is the accepted `identity_mismatch`.
Its live document is present and is the correct evidence for the question. The
response is a retrieval-path change (dense reach outside the FTS window), never
a gold rewrite, never a special case for c01, never an ID-based boost.

## 6. What remains refused

- No further FTS/lexical ranking change on `bec845b4`.
- No M8-E cleanup/RC; M8-E stays out of this campaign.
- No GPU, provider, prepare, promote, audit or seal without an explicit prompt
  (GPU additionally requires `GPU_OK=yes`).
- No gold-force, no gold-ID boosting, no "just promote Q-011", no weaker
  citation/materiality rule.
