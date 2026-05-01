# Catalyst Full-Version Strategy Spec

**Status:** Proposed (revision 3 — pending review)
**Date:** 2026-04-27
**Owner:** Catalyst core
**Supersedes:** Strategic positioning sections of `docs/full-version-design-delta.md` and the original "stock attribution" framing in `README.md`. Does **not** supersede `docs/full-version-execution-spec.md` — that remains the technical execution baseline. Where this strategy spec narrows the execution spec's scope for the defense window, the divergence is recorded in §11 *Defense-Phase Waivers* below.
**Time horizon:** 14 calendar days to thesis defense; GitHub finalization continues post-defense without time pressure.

**Revision log:**
- r1 (2026-04-27): initial draft.
- r2 (2026-04-27): tier reproducibility (§4.1); remove sample-reduction escape from Day 10 Gate (§6.1); add Defense-Phase Waivers (§11); add Layer 2 data-prerequisite gate (§8.5); make current_situation rewrite explicit in doc sweep (§2.1); add P0 Task Contracts (§13).
- r3 (2026-04-27): pin model_id / provider_version / DB fingerprint / git SHA in Tier-A reproducibility (§4.1); add W-08 refusal-metric substitution and W-09 GDELT contingency (§11); make §8.5 geopolitical threshold conditional with Tier 1/Tier 2 fallback; tighten T-01 verification to recursive header check; tighten T-08 to concrete pytest path with class-tag assertion; tighten T-15 with annotated git tag, freeze-commit SHA cross-reference, and post-freeze touch boundary.

---

## 1. Positioning (Canonical)

### 1.1 Headline

> **From prompt-driven attribution demo to evidence-bounded agent system.**
>
> Catalyst manages uncertainty under evidence constraints, demonstrated on financial event explanation as a high-noise, low-ground-truth testbed.

### 1.2 What Catalyst optimizes for (active claims)

- **Evidence validity** — every output claim maps to a retrieved evidence id present in the run.
- **Refusal quality** — explicit `SUFFICIENT / PARTIAL / INSUFFICIENT / SYSTEM_ERROR` states; the system can refuse rather than fabricate.
- **Replayability** — single `trace_id` covers an entire run; node-level events persisted; runs reproducible from frozen artifacts.
- **Failure governance** — failures are classified into a fixed taxonomy, mapped to deterministic responses, and backfilled as regression tests.

### 1.3 What Catalyst does NOT claim

- Does not optimize for one-shot answer fluency against frontier LLMs.
- Does not claim to prove true economic causality.
- Does not present finance as a product direction. Financial event explanation is the testbed; the engineering is the product.

### 1.4 Default response to "Why not just use Claude?"

> Claude is optimized for one-shot answer fluency. Catalyst is optimized for evidence validity, refusal quality, replayability, and failure governance — a different category of system. The `direct_llm` baseline gives a controlled comparison; the engineering value is in what the system does **before** producing an answer (retrieve, validate, route, refuse) and **after** it fails (classify, regression-test, replay).

Hallucination examples observed during baseline runs are kept as **naturalistic evidence in an appendix slide**, not as the main differentiation argument. The differentiation rests on structural properties of the system, not on opponent failures.

---

## 2. Scope Boundaries (P0 / P1 / P2)

### 2.1 P0 — Two-week hard delivery (defense floor)

P0 is the closed loop required for thesis defense. Any P0 item missing at the Day 10 Gate triggers an immediate freeze on P1/P2 to recover P0.

**Narrative consistency**
- Doc sweep across the four canonical anchors (§7) and all package READMEs to align on the new positioning.
- `README.md` rewritten so the first 30 lines lead with the §1.1 headline, the §1.2 active claims, and the §1.3 non-claims. The old "AI-powered causal attribution" framing is removed entirely, not annotated.
- `docs/current_situation.md` rewritten to reflect the **midterm → defense → roadmap** delta (mirrors deck P7). Sections to update: positioning paragraph (§1.1 of that doc), the "已实现与边界" section (must reference the four-state output, validator, and trace artifacts as the defense-phase additions), and the "下一步" section (must point at this strategy spec).
- Supporting-reference headers added to every non-anchor doc in `docs/` (§7.1 convention).

**Control-plane hardening (minimal viable)**
- Validator + 4-state output enum (`SUFFICIENT / PARTIAL / INSUFFICIENT / SYSTEM_ERROR`).
- Evidence id existence check + schema validation gate before final output.
- Trace persistence (local SQLite) with the minimum field set from `full-version-execution-spec.md` §10.
- Failure taxonomy documentation + at least 2 regression tests.

**Policy-driven graph (minimal viable — see §3)**
- Full graph wiring: `Parser → RetrievalPolicy → Miner → Critic → DecisionRouter → Judge | Expand | Refuse → Validator → Finalizer`.
- DecisionRouter as a deterministic dispatcher (not LLM-driven) reading Critic's contract output.
- Critic decision contract: schema-complete (`sufficiency`, `next_action`, `magnitude_coverage`, `reasoning`); internal judgment may use simple thresholds/rules.
- Layer 1 + Layer 2 retrieval (direct + macro). Layer 3 (related-entity) ships as interface stub with `NotImplementedError`, plus retrieval_metadata field reserved.

**Evaluation (real, not theatrical)**
- `direct_llm` baseline pipeline — minimum viable, runs against the 10-case set.
- 10-case golden set (distribution: 5 sufficient / 2 partial / 3 should-refuse).
- Eval report committed as markdown + json under `data/eval_reports/`.
- Cost / latency / token figures included on every config.

**Defense materials**
- 8-page deck (§5).
- Pre-recorded demo notebook + frozen artifacts (no live API dependency on defense day).

### 2.2 P1 — Enhancement, may be cut at Day 10 Gate

- `rag_only` baseline as a third comparison configuration.
- Expanded regression test coverage (4–6 tests across taxonomy categories).
- LangSmith integration via the same `trace_id`.
- Asset extraction: `packages/eval` packaged as a standalone, finance-agnostic eval harness with `pyproject.toml` and a public API surface (G5 — explicitly cuttable).

### 2.3 P2 — Roadmap (post-defense, no time pressure)

- Layer 3 retrieval (related-entity) full implementation with peer/supplier/sector entity map.
- Critic decision contract upgraded from threshold-based to LLM-graded `sufficiency`.
- Budget circuit breaker with full token / cost / latency caps.
- Model routing policy (cheap models for grading, stronger for synthesis).
- ADR-004 through ADR-007 (per `full-version-execution-spec.md` §15).
- Whitepaper / blog post.
- `packages/data-core` extraction as a standalone publishable package.

---

## 3. Minimal Viable Definitions for Control-Plane Components

This section locks the interpretation of "minimal" for each P0 control-plane component. The intent: ship the **shape, interface, and traceability** of each component; defer semantic depth to P2.

### 3.1 DecisionRouter

- Deterministic dispatcher in code (no LLM call).
- Inputs: Critic contract output (`sufficiency`, `next_action`, `magnitude_coverage`).
- Outputs: next graph edge — one of `judge | expand_macro | expand_related | refuse | system_error`.
- Decision rules per `full-version-execution-spec.md` §6 (deterministic ladder).
- Real participation in routing — no bypass paths.

### 3.2 Three-layer retrieval (Layer 1 + Layer 2 only in P0)

- Layer 1 (direct): ticker filter + date window. Implemented.
- Layer 2 (macro): drop ticker filter, query macro/geopolitical/market sources. Implemented.
- Layer 3 (related): interface declared in retrieval module signatures; `retrieval_metadata` schema reserves the field. **In P0, DecisionRouter short-circuits any `expand_related` decision directly to `INSUFFICIENT` with reason `layer3_not_implemented`.** Layer 3 module body raises `NotImplementedError` as defensive coding only; the path is not exercised in P0 runs.
- Expansion bounds: `max_layers=2` effective in P0 (spec value `max_layers=3` is preserved in code constants for P2). `max_expansions=2`.
- Trace records the short-circuit explicitly so the behavior is auditable rather than silent.

### 3.3 Critic decision contract

- Schema is complete and load-bearing: `sufficiency`, `next_action`, `magnitude_coverage`, `reasoning` — all fields populated, all consumed by DecisionRouter.
- Internal judgment in P0 may use simple rules:
  - `sufficiency = sufficient` if `evidence_count >= K_sufficient` AND `magnitude_coverage >= M_threshold`.
  - `sufficiency = partial` if `K_partial <= evidence_count < K_sufficient`.
  - `sufficiency = insufficient` otherwise.
  - Specific thresholds (`K_*`, `M_threshold`) calibrated against the 10-case golden set during Day 8–9.
- P2 upgrades to LLM-graded sufficiency. The contract stays the same; only the implementation changes.

### 3.4 Graph wiring

- All nodes from `Parser → ... → Finalizer` exist as named modules with declared inputs/outputs.
- Each node may be a thin wrapper over existing midterm logic; depth is not a P0 requirement.
- The graph is real — every run flows through every node, traces capture every transition.

---

## 4. Acceptance Gates (Defense Floor)

The 10-case eval run produces these gate values. Every gate must pass for the defense to proceed without scope reduction.

| Gate | Threshold | Failure consequence |
|---|---|---|
| `evidence_validity` | ≥ 0.95 | Validator not effective; pitch collapses. Hard fail. |
| `schema_validity` | 1.00 (10/10) | Validator not effective. Hard fail. |
| `trace_completeness` | 1.00 (10/10 runs have full `trace_id` chain) | Observability claim unsupported. Hard fail. |
| `should_refuse_hit_rate` | ≥ 2/3 on the should-refuse subset | Refusal claim unsupported. Hard fail. |
| `cost_latency_reported` | All configs report `cost_usd`, `latency_ms_p50`, `latency_ms_p95`, `tokens_in`, `tokens_out` | "Governance" claim unsupported. Hard fail. |

Quality metrics (`attribution_f1`, `category_accuracy`, `grounding_rate`, `temporal_precision`) are **reported** but **not gated** — Catalyst does not commit to beating direct LLM baselines on answer quality.

### 4.1 Reproducibility definition (tiered)

Reproducibility is enforced at three tiers. A second run with the same seed, frozen corpus (`catalyst_eval_frozen.db`), and pinned model versions must hold the strict tier exactly; the variance tier is bounded; the narrative tier is unconstrained.

**Tier A — Strict equality (defense-grade)**

These fields must match byte-for-byte across reruns. Any divergence is a reproducibility failure.

- `status` (4-state enum) — per case.
- `evidence_ids` — set equality per case.
- `next_action` from Critic — per case.
- `magnitude_coverage` rounded to 2 decimal places — per case.
- Quality metrics: `evidence_validity`, `schema_validity`, `trace_completeness`, `should_refuse_hit_rate`, `attribution_f1`, `category_accuracy`, `grounding_rate`, `temporal_precision` — exact match (deterministic given fixed inputs and tier-A outputs).
- Artifact file inventory at fixed paths — every expected file present.

**Tier A also requires the inputs to be pinned.** Any rerun that compares against the frozen artifact must record and verify these freeze-time values; otherwise "exact match" is comparing against a moving target.

| Pinned input | Captured at | Stored where | Verified by |
|---|---|---|---|
| `model_id` per role (Miner / Critic / Judge / Validator / direct_llm baseline) | Day 8 freeze | `data/eval_reports/<frozen_run>.json` header block + every trace row's `model_id` field | Rerun fails closed if any role's `model_id` differs from frozen. |
| `provider_version` (provider-reported, where available) | Day 8 freeze | Same header block | Logged; warning on drift, not hard fail (some providers don't expose). |
| `catalyst_eval_frozen.db` fingerprint | Day 8 freeze, post-preflight | `data/eval_reports/preflight_<timestamp>.json` and `data/eval_reports/<frozen_run>.json` header | SHA-256 of the DB file + row counts of `raw_assets` / `clean_assets` / any retrieval-relevant tables. Mismatch = hard fail. |
| Random seed (for any sampler / shuffle) | At code level | Constant in eval runner | Inspected during audit (T-13). |
| Code git SHA at freeze | Day 8 freeze | Frozen run header | Tagged in git (T-15). |

**Tier B — Bounded variance (operational)**

These fields are reported but expected to vary across reruns due to provider jitter. Defense materials cite the **frozen artifact values**, not live reruns.

- `cost_usd` per run — variance bounded but not enforced.
- `latency_ms_p50` and `latency_ms_p95` — variance bounded but not enforced.
- `tokens_in` / `tokens_out` — small variance permitted (some providers vary tokenization slightly).

For defense use: the frozen `data/eval_reports/<frozen_run>.json` is the authoritative source for these numbers. Live reruns are diagnostic, not evidential.

**Tier C — Unconstrained (free-text)**

- `summary_md` — natural-language summary; not required to match across runs. LLM nondeterminism on free-text generation is accepted.
- `reasoning` field on Critic output — not required to match (the *decision* it produces is tier A; the *reason text* is tier C).

**Defense-day rule:** any reproducibility claim made on stage cites the frozen artifact at a specific path. Live reruns are not invoked to demonstrate reproducibility — they may diverge in tier B/C and confuse the audience.

---

## 5. Defense Deck Structure

8 slides + 1 appendix. Target 15-minute talk.

| Slide | Content |
|---|---|
| P1 | Headline + double-layer positioning (§1.1). "Why not Claude" framing on this page. |
| P2 | Architecture diagram — four pillars + control-plane graph. |
| P3 | Control-plane walkthrough — same query traced through `Parser → RetrievalPolicy → Miner → Critic → DecisionRouter → Validator`. |
| P4 | Structural property table — status enum, validator behavior, trace, regression tests, cost visibility. Each row paired with a screenshot/artifact reference. **No "Claude hallucinated" framing on this page.** |
| P5 | Eval baseline comparison — `direct_llm` vs `mcj_full` (and `rag_only` if P1 ships). Quality metrics + cost/latency/token table. |
| P6 | Failure taxonomy + regression test snapshots. |
| P7 | Three-column delta: **Midterm Freeze** \| **Now (Defense)** \| **Roadmap**. Honest division of done / spec'd / future. |
| P8 | Reframing summary — "What this project demonstrates about agent engineering." |
| Appendix A9 | Naturalistic example (optional, used only if directly questioned about Claude head-to-head). |

---

## 6. Two-Week Schedule

| Day | Block | Deliverable |
|---|---|---|
| 1 | Doc sweep | Four-anchor docs + package READMEs aligned to new positioning. README.md rewritten. |
| 2 AM | DB + paths | `catalyst_dev.db` / `catalyst_eval_frozen.db` / `catalyst_demo.db` split. Artifact paths fixed (`data/eval_reports/`, `data/traces/`, `docs/testing/`). |
| 2 PM | Validator start | Validator + 4-state enum scaffold. |
| 3–4 | Validator + control-plane | Schema validator, evidence-id existence check, 4-state output, single regenerate-on-fail policy. Critic decision contract schema. |
| 4 PM (½) | DecisionRouter | Deterministic dispatcher skeleton + edge wiring. |
| 5 | direct_llm baseline | Minimal pipeline runnable against the 10-case set. |
| 5 PM (½) | Retrieval policy | Layer 1 + Layer 2 implementation; Layer 3 stub; bounded expansion. |
| 6 | Trace persistence | SQLite trace schema + writer. Minimum field set. |
| 7 | Failure taxonomy + regressions | Taxonomy doc + 2 regression tests wired into CI-style runner. |
| 8 | First full eval run | 10-case run, calibrate Critic thresholds against the set. |
| 9 | Eval report + deck draft | Markdown + JSON eval report committed. Deck draft P1–P8. |
| 10 | **Day 10 Gate** | All P0 acceptance gates verified. **Decide: proceed P1, cut to roadmap, or stay on P0 polish.** G5 decision made here. |
| 11 | Verification audit | Run `verification-before-completion` discipline against every "done" claim. Fix gaps. **No new features.** |
| 12 AM | Continued audit | Finish audit; address any drift. |
| 12 PM | Tech dry-run | End-to-end demo notebook run; artifact sanity check; defense day procedure rehearsed. |
| 13 | Code freeze + deck polish | **Code is frozen.** Deck wording, screenshots, case selection finalized. |
| 14 | Defense rehearsal + buffer | Full talk rehearsal. Buffer for any last-minute fix to non-code material. |

### 6.1 Day 10 Gate decision tree

```
All P0 gates green?
├─ Yes → P1 work continues. G5 (eval extraction) ships if buffer allows.
└─ No  → Freeze ALL P1/P2 work. All hands on P0 recovery.
         Day 11 EOD re-evaluation:
           ├─ Now green → Resume polish path (no P1).
           └─ Still red → P0 ships with explicit failure disclosure.
                          Deck P7 reports the failed gate honestly.
                          Defense narrative pivots to "what we
                          learned and what we'd build next."
```

**Sample-protocol immutability rule:** the 10-case set, its 5/2/3 distribution, and the gate denominators (especially `should_refuse_hit_rate ≥ 2/3` of 3 cases) are **fixed at Day 8 freeze and cannot be changed after that point**. Reducing the case count to make a gate pass is forbidden — it would invalidate the protocol and any reviewer who notices it (correctly) reads it as scope-shifting. If P0 cannot pass the original protocol, the protocol stands and the failure is reported honestly. Scope is cut from P1, never from the eval sample.

**Why this rule is hard:** the gates were designed against a specific distribution. Changing the sample changes the meaning of the gates; passing them on a smaller set is not the same achievement and cannot be presented as such. Honest disclosure of a failed gate on the original protocol is worth more — to reviewers and to your engineering credibility — than a passed gate on a retrofitted protocol.

---

## 7. Documentation Anchors (Canonical Sources)

The following four documents are the only authoritative sources for the project narrative. Every other document either references one of these or is marked as supporting reference.

| # | Path | Layer |
|---|---|---|
| 1 | `README.md` | Entry point — public-facing positioning. |
| 2 | `docs/superpowers/specs/2026-04-27-catalyst-fullversion-strategy.md` (this file) | Strategy layer. |
| 3 | `docs/full-version-execution-spec.md` | Technical execution baseline. |
| 4 | `docs/current_situation.md` | State layer — what's done now, what's next. |

### 7.1 Supporting-reference header convention

All non-anchor docs (delta docs, judgment docs, ADRs, philosophy notes) must include this header within the first five lines:

```
> Supporting reference. Canonical source: <path-to-anchor>
```

Documents without this header are considered orphaned and must be either promoted to an anchor, linked from one, or removed. The Day 1 doc sweep enforces this convention across the repository.

---

## 8. Engineering Hard Rules (Day 10+)

These rules are enforced from Day 10 onward and are not negotiable without a formal scope-change decision recorded in this file.

### 8.1 Schema freeze

- After Day 10, no schema changes to:
  - Validator output schema.
  - Trace table columns.
  - Eval report JSON structure.
  - DecisionRouter contract.
- Bug fixes that preserve schema are allowed during the audit window.

### 8.2 Database boundary rules

- `catalyst_eval_frozen.db` — frozen on Day 8 immediately before the first 10-case run. Treated as legal evidence: any reproducibility claim references this file. **No writes** after freeze.
- `catalyst_demo.db` — populated from frozen eval artifacts; demo notebook reads only from this file.
- `catalyst_dev.db` — daily development; never referenced by defense materials.

### 8.3 Artifact commit rules

- Eval reports: `data/eval_reports/<timestamp>_<config>.{md,json}` — committed.
- Traces: `data/traces/<run_id>.json` — committed for the 10-case golden run.
- Test cases / golden set: `docs/testing/<topic>.md` + corresponding fixture files — committed.
- Demo notebook: committed with cleared outputs; a frozen `_executed.ipynb` companion committed with outputs from the demo dry-run.

### 8.4 Branch discipline

- One branch per objective.
- Target lifetime: 1 day. Hard upper bound: 2 days.
- Branches exceeding 2 days are merged or abandoned at end of Day 2; no exceptions during the two-week sprint.

### 8.5 Pre-eval data prerequisite check (Layer 2 must not silently empty-spin)

Before the eval freeze on Day 8, the following data prerequisites are verified by an explicit script. Failure of any check blocks the freeze and triggers ingestion before proceeding.

**Required data classes in `catalyst_eval_frozen.db` (pre-freeze check):**

| Class | Source examples | Minimum count | Conditional | Why required |
|---|---|---|---|---|
| Macro / monetary | FRED rates, CPI, Fed minutes | ≥ 30 documents within the case date windows | Unconditional (FRED ingestion is in midterm scope). | Layer 2 macro retrieval needs hits, not empty results. |
| Direct evidence | Per-ticker news, filings | ≥ 5 documents per case ticker | Unconditional. | Layer 1 baseline. |
| Geopolitical / cross-asset (Tier 1) | GDELT 2.0 events | ≥ 50 documents within the case date windows | **Conditional on GDELT ingestion being committed and frozen by Day 8 AM.** | Layer 2 macro retrieval second source. |
| Geopolitical / cross-asset (Tier 2 — fallback if GDELT not frozen) | News headlines tagged macro / policy / tariff from existing Polygon news corpus | ≥ 20 documents within the case date windows | Activated when GDELT Tier 1 is unavailable. | Layer 2 macro retrieval second source, reduced ambition. |

**GDELT contingency rule:** if Day 8 AM finds GDELT ingestion incomplete or untrusted, the preflight script accepts Tier 2 instead of Tier 1, AND `catalyst_eval_frozen.db` records a flag `geo_corpus_tier=2`, AND the eval report explicitly notes "Layer 2 geopolitical evidence operates on the news-tagged fallback corpus; GDELT integration deferred per W-09 (added below)." This is preferable to silently lowering the threshold under the Tier 1 label. If the user adds GDELT post-defense, the flag flips and the eval becomes a re-runnable comparison artifact.

**Hard check script (committed under `packages/eval/scripts/preflight.py` or equivalent):**

- Runs against `catalyst_eval_frozen.db` candidate **before** the database is renamed/marked frozen.
- Exits non-zero on any class below threshold.
- Output committed alongside the frozen DB as `data/eval_reports/preflight_<timestamp>.json`.

**Rationale.** A "3-layer adaptive retrieval" claim is only credible if Layer 2 has data to retrieve from. Without this check, Layer 2 silently returns empty results, the system falls through to `INSUFFICIENT`, and the policy claim becomes theatrical. The check makes the data dependency explicit and auditable.

**If the check fails on Day 8:** ingestion runs Day 8 morning, freeze pushes to Day 8 EOD or Day 9 AM. This is the only legitimate reason to slip the eval-freeze date; any other slip is a P0 failure.

---

## 9. Risks and Mitigations

| Risk | Probability | Mitigation |
|---|---|---|
| Day 10 P0 gates fail | Medium | Day 10 Gate decision tree (§6.1) — freeze enhancements, recover P0. |
| Should-refuse subset proves brittle (judge thresholds wrong) | Medium | Calibrate thresholds against the actual 10-case set on Day 8; pick should-refuse cases with clear ground truth (events outside corpus, future-dated events, private-company events). |
| Live API failure on defense day | High if live demo | Defense uses pre-recorded artifacts and frozen `catalyst_demo.db`; live API only invoked if examiner explicitly requests a fresh case. |
| Examiner asks "why not just use Claude" with hostile intent | Medium | §1.4 default response, backed by structural property table (deck P4) and naturalistic appendix (A9). |
| Doc inconsistency surfaces during defense Q&A | Medium | Day 1 doc sweep + supporting-reference header convention; verification audit Day 11–12. |
| Two-week scope still too large despite §3 minimization | Medium-High | Day 10 Gate is the explicit checkpoint. P1/P2 scope is designed to be cuttable without affecting P0. |

---

## 10. Non-Goals (Explicit)

- Beating frontier LLMs on `attribution_f1` or other answer-quality metrics.
- Proving true economic causality.
- Productizing Catalyst as a financial tool.
- Multimodal ingestion in v1.
- Long-term user memory in v1.
- Full API or production frontend before defense.

---

## 11. Defense-Phase Waivers (Strategy ↔ Execution Reconciliation)

`docs/full-version-execution-spec.md` is the technical execution baseline and includes scope items appropriate for a non-time-pressured build. This strategy spec narrows that scope for the 14-day defense window. The narrowing is explicit and audit-ready: each item below is a recorded waiver with a target restoration phase.

| # | Execution-spec scope item | Defense-window status | Restored in |
|---|---|---|---|
| W-01 | `rag_only` baseline configuration | **Waived from P0.** Ships in P1 only if Day 10 Gate is green. May further defer to roadmap if buffer absent. | P1 (defense window) or roadmap |
| W-02 | Layer 3 retrieval (related-entity) | **Waived from P0.** Interface declared, body raises `NotImplementedError`, `expand_related` short-circuits to `INSUFFICIENT`. | P2 (post-defense) |
| W-03 | Critic LLM-graded `sufficiency` | **Waived from P0.** P0 uses threshold-based judgment (`K_sufficient`, `K_partial`, `M_threshold`). Schema is unchanged. | P2 (post-defense) |
| W-04 | Budget circuit breaker (full token / cost / latency caps with mid-run stop) | **Waived from P0.** P0 reports cost/latency/tokens; does not enforce caps mid-run. | P2 (post-defense) |
| W-05 | Model routing policy (cheap for grading, strong for synthesis) | **Waived from P0.** Single-model run path in P0. | P2 (post-defense) |
| W-06 | LangSmith integration | **Waived from P0.** Local SQLite trace persistence is the P0 source of truth; LangSmith arrives via shared `trace_id` if P1 ships. | P1 or roadmap |
| W-07 | Multi-config full eval matrix (3 configs × full golden set) | **Waived from P0.** P0 ships `direct_llm` vs `mcj_full` on the 10-case set. | Post-defense GitHub finalization |
| W-08 | `refusal_precision` / `refusal_recall` gates (per execution spec §2) | **Substituted, not waived.** P0 gates on `should_refuse_hit_rate ≥ 2/3` over a 3-case should-refuse subset. Rationale: with n=3, precision/recall produce statistically meaningless single-decimal swings (one mistake = 33-point drop), whereas hit rate is the same information stated as an interpretable count. The execution spec's metrics return in P2 once the should-refuse subset grows beyond ~10 cases. | P2 (post-defense, with expanded golden set) |
| W-09 | GDELT 2.0 geopolitical corpus as Layer 2 source | **Conditional.** If GDELT ingestion is committed and frozen by Day 8 AM, P0 uses GDELT (`geo_corpus_tier=1`, ≥50 documents). If not, P0 falls back to news-tagged Polygon corpus (`geo_corpus_tier=2`, ≥20 documents). Whichever tier is used, the choice is recorded in the frozen DB metadata and the eval report. | P1 or roadmap (depending on Day 8 state) |

**Audit declaration for defense:** these waivers are not hidden. Deck P7 (Roadmap) lists each waived item explicitly under its restoration column. Reviewers asking "why isn't X built yet?" receive the waiver row as the answer: scoped, documented, dated.

**Anti-pattern this section prevents:** a reviewer cross-referencing `full-version-execution-spec.md` and this strategy spec, finding silent disagreements, and concluding the project has scope drift. The waivers make the divergence a controlled, reasoned narrowing rather than an undocumented retreat.

---

## 12. Open Questions (Resolve Before Implementation Plan)

These are items requiring confirmation in spec review before the writing-plans skill is invoked:

1. **`K_sufficient`, `K_partial`, `M_threshold` initial values** — proposed Day 8 calibration. Initial guess: `K_sufficient=4`, `K_partial=2`, `M_threshold=0.6`. To be tuned.
2. **Should-refuse case selection criteria** — proposed: (a) events outside the frozen corpus's date range, (b) events for tickers not in the corpus, (c) deliberately ambiguous queries. Final selection in Day 8.
3. **Demo notebook framework** — Jupyter assumed. Confirm or substitute.
4. **Defense talk length** — 15 minutes assumed for 8-slide deck. Confirm against actual defense format.
5. **Layer 2 data thresholds in §8.5** — proposed counts (≥30 macro, ≥50 geo, ≥5 per ticker). Calibrate against actual `catalyst_eval_frozen.db` candidate before Day 8.

---

## 13. P0 Task Contracts (Entry / Exit / Verification)

Each P0 task has a contract: what must be true to start, what must be true to declare done, and a verification command or check. The implementation plan (writing-plans output) expands these into day-level subtasks.

### T-01 Doc sweep (Day 1)

- **Entry:** None.
- **Exit:**
  - `README.md` first 30 lines lead with §1.1 / §1.2 / §1.3 content; no occurrence of "AI-powered causal attribution".
  - `docs/current_situation.md` updated with midterm→defense→roadmap delta and points at this strategy spec.
  - All non-anchor docs in `docs/` carry the supporting-reference header (§7.1).
- **Verification:**
  - `grep -ri "AI-powered causal attribution" README.md docs/` returns zero hits, OR all hits are inside an explicitly-named history/changelog block (audit reviews each hit individually).
  - Recursive header check on every markdown under `docs/`:
    ```
    find docs -type f -name "*.md" \
      ! -path "docs/superpowers/specs/2026-04-27-catalyst-fullversion-strategy.md" \
      ! -path "docs/full-version-execution-spec.md" \
      ! -path "docs/current_situation.md" \
      ! -path "docs/superpowers/specs/2026-04-02-catalyst-system-design.md" \
      ! -path "docs/superpowers/specs/2026-04-07-catalyst-midterm-freeze-boundary.md" \
      | xargs grep -L "Supporting reference"
    ```
    must return empty (anchor exclusions are explicit; every other doc carries the header).
  - Same recursion run against `packages/*/README.md` to confirm package READMEs reflect the new positioning.

### T-02 DB triple split + artifact paths (Day 2 AM)

- **Entry:** T-01 complete.
- **Exit:**
  - `catalyst_dev.db`, `catalyst_eval_frozen.db`, `catalyst_demo.db` exist (eval/demo may be empty stubs at this point).
  - `data/eval_reports/`, `data/traces/`, `docs/testing/` directories exist with `.gitkeep` or initial content.
  - Configuration in `packages/data-core` reads DB path from environment / config, defaults to `catalyst_dev.db`.
- **Verification:** `ls data/*.db data/eval_reports/ data/traces/ docs/testing/` succeeds.

### T-03 Validator + 4-state output enum + Critic schema (Day 2 PM – Day 4 AM)

- **Entry:** T-02 complete; existing midterm Miner/Critic/Judge runnable.
- **Exit:**
  - Output schema includes `status` enum (`SUFFICIENT/PARTIAL/INSUFFICIENT/SYSTEM_ERROR`).
  - Validator module runs after Judge, before Finalizer; performs evidence-id existence check, schema check, magnitude sanity check.
  - On validator failure: one regenerate attempt with strict instruction; second failure → downgrade to `PARTIAL` or `SYSTEM_ERROR` per failure type.
  - Critic output schema includes `sufficiency`, `next_action`, `magnitude_coverage`, `reasoning` — all four fields populated on every run.
- **Verification:**
  - Unit test: validator rejects output with non-existent `evidence_id` and triggers regenerate.
  - Unit test: validator forces `PARTIAL` when `magnitude_coverage` below threshold for a "complete" claim.
  - Schema test: every Critic output instance validates against the contract schema.

### T-04 DecisionRouter (Day 4 PM)

- **Entry:** T-03 complete.
- **Exit:**
  - DecisionRouter is a deterministic dispatcher in code (no LLM call).
  - Reads Critic contract output; emits one of `judge | expand_macro | expand_related | refuse | system_error`.
  - `expand_related` short-circuits to `INSUFFICIENT` with reason `layer3_not_implemented` (per §3.2).
  - Routing decisions appear in trace.
- **Verification:** Unit tests cover each Critic-output → router-decision mapping, including the Layer 3 short-circuit path.

### T-05 direct_llm baseline pipeline (Day 5 AM)

- **Entry:** T-03 complete; 10-case set candidate exists (final freeze on Day 8).
- **Exit:**
  - Pipeline accepts a case file, calls a configured frontier LLM, returns the same output schema as `mcj_full` (so they are diffable).
  - No retrieval, no validator on the LLM's free-text output, but `evidence_ids` on output are validated for existence (the LLM cannot cite something that doesn't exist in the frozen corpus).
- **Verification:** Run on 1 case end-to-end; output JSON conforms to schema; cost/latency/tokens recorded.

### T-06 Retrieval policy Layer 1 + Layer 2 (Day 5 PM)

- **Entry:** T-04 complete.
- **Exit:**
  - Layer 1: ticker-filtered, date-windowed retrieval implemented.
  - Layer 2: macro/geopolitical retrieval implemented (no ticker filter).
  - Layer 3: function signature exists; body raises `NotImplementedError`. Not invoked by P0 router (per T-04 short-circuit).
  - `retrieval_metadata` in trace records: layers attempted, expansion reason, stop reason, hit counts per layer.
  - `max_expansions=2` enforced.
- **Verification:** Test that a query with insufficient Layer 1 evidence triggers Layer 2; test that Layer 3 condition triggers short-circuit.

### T-07 Trace persistence (Day 6)

- **Entry:** T-04, T-06 complete.
- **Exit:**
  - SQLite trace table created in `catalyst_dev.db` and reflected in `catalyst_eval_frozen.db` schema.
  - Trace writer captures fields per `full-version-execution-spec.md` §10 (run_id, trace_id, node, timing, tokens, cost, decision, error, status_before/after).
  - Trace export utility writes `data/traces/<run_id>.json`.
- **Verification:**
  - Run any case; trace row count > 0; export JSON loads and contains all required fields.
  - Query utility returns rows by `error_type` and by ticker/date.

### T-08 Failure taxonomy + 2 regression tests (Day 7)

- **Entry:** T-07 complete.
- **Exit:**
  - `docs/testing/failure-taxonomy.md` documents the 5 failure classes from `full-version-execution-spec.md` §9 with the deterministic action for each.
  - At least 2 regression tests exist in `packages/agents/tests/test_failure_taxonomy.py`, each tied to a documented failure class. Each test asserts both the failure detection AND the deterministic recovery path. Each test maps to a named failure class via a docstring tag (e.g. `# class: retrieval_failure`).
- **Verification:**
  - `uv run pytest packages/agents/tests/test_failure_taxonomy.py -v` returns exit 0 with at least 2 passing tests.
  - `grep -l "# class:" packages/agents/tests/test_failure_taxonomy.py` finds the file (tag presence asserted).

### T-09 Pre-eval data preflight (Day 8 AM)

- **Entry:** T-02 through T-08 complete; ingestion paths populated.
- **Exit:**
  - Preflight script runs against `catalyst_eval_frozen.db` candidate.
  - Output written to `data/eval_reports/preflight_<timestamp>.json`.
  - All thresholds in §8.5 met; if not, ingestion fills the gap before freeze.
- **Verification:** Preflight exit code = 0; report committed.

### T-10 First full eval run + threshold calibration (Day 8 PM)

- **Entry:** T-09 passed; eval DB frozen.
- **Exit:**
  - 10-case set finalized (5/2/3 distribution, never to be modified after this point).
  - `direct_llm` and `mcj_full` runs complete on all 10 cases.
  - Critic thresholds (`K_sufficient`, `K_partial`, `M_threshold`) calibrated against the run; recorded in this spec under §12.
- **Verification:** Both runs produced and committed under `data/eval_reports/`; trace files committed under `data/traces/`.

### T-11 Eval report + deck draft (Day 9)

- **Entry:** T-10 complete.
- **Exit:**
  - Markdown + JSON eval report committed comparing `direct_llm` vs `mcj_full` across all gates and Tier-A/B reproducibility values.
  - Deck draft (P1–P8 + appendix) covering §5 structure exists.
- **Verification:** Report file exists at fixed path; deck file exists; both committed.

### T-12 Day 10 Gate (Day 10)

- **Entry:** T-11 complete.
- **Exit:** Gate decision recorded (proceed P1 / freeze / disclose failure) per §6.1 decision tree.
- **Verification:** Gate decision recorded in this spec or in a dated decision log under `docs/`.

### T-13 Verification audit (Day 11 – Day 12 AM)

- **Entry:** T-12 decision recorded.
- **Exit:** Every "done" claim made in T-01 through T-11 re-verified against the actual artifact / test / file. No new features added.
- **Verification:** Audit checklist file under `docs/testing/audit_<date>.md` lists each T-XX item and its re-verification result.

### T-14 Tech dry-run (Day 12 PM)

- **Entry:** T-13 complete.
- **Exit:** Demo notebook executed end-to-end against `catalyst_demo.db`; all artifact references resolve; defense-day procedure rehearsed (open notebook, run cells, refer to deck).
- **Verification:** `_executed.ipynb` companion file committed with outputs.

### T-15 Code freeze + deck polish (Day 13)

- **Entry:** T-14 complete.
- **Exit:**
  - Annotated git tag `defense-freeze-YYYY-MM-DD` created at the freeze commit.
  - The freeze commit SHA is recorded in `data/eval_reports/<frozen_run>.json` header (matches the value captured at Day 8 freeze under §4.1 Tier-A pinning row "Code git SHA at freeze").
  - All subsequent commits between Day 13 and Day 14 touch only files under `docs/` or the deck file path; no edits under `packages/` or `data/eval_reports/` or `data/traces/`.
- **Verification:**
  - `git tag --list 'defense-freeze-*'` returns at least one entry; `git show defense-freeze-YYYY-MM-DD` shows an annotated tag (not lightweight).
  - `git log defense-freeze-YYYY-MM-DD..HEAD --name-only --pretty=format:` lists only files matching `^docs/` or the deck file path. Any other path = freeze violation.
  - The SHA in the frozen eval report matches `git rev-parse defense-freeze-YYYY-MM-DD^{commit}`.

### T-16 Defense rehearsal (Day 14)

- **Entry:** T-15 complete.
- **Exit:** Full talk rehearsed end-to-end at least twice; timing within target.
- **Verification:** Rehearsal notes file (informal, not committed required).

---

## 14. Approvals

- **Author:** Catalyst core
- **Strategy review:** Pending
- **Technical review against `full-version-execution-spec.md`:** Pending
- **Final acceptance:** Pending

Once approved, this document is the canonical strategy source until thesis defense. Updates require a new dated revision in this folder.
