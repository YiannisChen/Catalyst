# Binding Evaluation Design — Three-Surface Architecture

- Date: 2026-07-19
- Reviewer role: architecture reviewer (evaluation surfaces, metrics, UI visibility, package boundaries)
- Subject: the three-surface evaluation architecture (offline benchmark / per-run runtime checks / optional user feedback) under the canonical design `2026-07-19-catalyst-open-source-workbench-design.md` and the provenance/chunking design
- Status: **binding evaluation design** (ratified 2026-07-19 with consolidation amendments: case counts §D, effort estimate §D, grounding denominators §E-L3, assurance ownership §J — resolved to agents/runtime; correction amendment 2026-07-21: two-slice implementation order §D/§L, source-support assurance flags §F, cluster/representative fields in BenchmarkCase §D, ragas-extra removal §J)
- Discipline: review only; no code, staging, commits, providers, or secrets; DB SHAs verified before/after
- Verified state: branch `ws4b/article-level-data` at `4d4e3a9`; Git index empty; Dev DB SHA `92731fb7c5c3…`; Frozen DB SHA `0d97a7ec61b6…`

---

## A. Verdict

**Approve the three-surface architecture with amendments.** The separation — pinned-benchmark metrics for developers, deterministic per-run assurance for every run, quarantined optional feedback — is the correct shape for a bounded open-source systems project. Runtime assurance is the differentiating asset because it works on arbitrary runs. The compact benchmark exists only for named-case engineering decisions; it does not support generalized accuracy claims.

## B. Lessons from each reference project

| Project | Adopted pattern | Rejected for Catalyst |
|---|---|---|
| DeepEval (`references/deepeval`, metrics tree inspected) | metrics-as-test-assertions wired into pytest/CI — the pattern used by Catalyst's zero-network regression gate; threshold-bearing metric classes with explicit pass/fail | the judge-metric library (G-Eval, DAG, relevancy/bias/hallucination judges) as the measurement core — reintroduces LLM-judge theater; heavy dependency chain |
| Ragas (`references/ragas`, inspected 07-18) | Dataset + Experiment + Metric with per-case records before aggregation; noise-sensitivity as a *perturbation*, not a standing metric | reference-answer metrics (answer relevance/similarity/correctness), LLM-graded context precision/recall as primary retrieval measures, runtime dependency |
| TruLens (`references/trulens-main`, inspected 07-18) | the triad *separation* — retrieval, groundedness, answer quality are distinct failure surfaces with distinct labels; feedback attached to trace spans | feedback functions as primary measurement; dashboard/OTel machinery at this scope |
| GPT-Researcher evals (`references/gpt-researcher/evals`) | per-query records + aggregate report kept in version control; grading claims against retrieved context rather than a reference answer | single zero-shot judge as the whole methodology; no labels, no audit |
| LangGraph (`references/langgraph-main`, examples/chatbot-simulation-evaluation) | evaluate graph workflows from persisted traces/checkpoints rather than re-instrumenting the runtime — Catalyst's TraceWriter already persists node events, so L4 metrics read traces | simulated-user dialog evaluation (no conversational surface in Catalyst); LangSmith-hosted evaluation dependency |

## C. Final evaluation architecture

Three surfaces, strictly separated by what they may consume and when they run:

1. **Surface A — offline benchmark (developers, pinned corpus only):** BenchmarkCase dataset (§D) + component metrics (§E, layers L1–L3-judged) + workflow scorecard. Runs only against a Certified Snapshot and pinned index manifest; requires labels; may use the cached, governed LLM judge. Never runs against live data; its numbers describe the benchmark, nothing else.
2. **Surface B — per-run runtime assurance (every run, production included):** deterministic checks only (§F). No labels, no judges, no aggregates required. Emits a per-run assurance record persisted with the trace. This is the contract a contributor can rely on for any run.
3. **Surface C — optional user feedback (quarantined):** §H. Advisory signal storage; never truth, never training data, never a metric input.

The boundary rule that keeps the design honest: **a metric that requires labels or a judge belongs to Surface A only; anything on Surface B must be computable from the run's own artifacts.** Every metric in §E is tagged accordingly; any future metric proposal must declare its surface first.

## D. Benchmark schema and labeling protocol

**Case set: 12 MUST base records** — 6 answerable workflow + 2 should-abstain + 4 retrieval-only. Retrieval comparisons use the 10 answerable-class cases; the workflow scorecard uses 6+2. Expansion is allowed only when a concrete failure cannot be represented by the existing cases.

Effort estimate (amended, binding schedule anchor): initial evidence grading ≈ 12–18 hours; second adjudication pass ≈ 4–6 hours, separated by at least seven calendar days; setup/validation ≈ 3–5 hours; total ≈ 19–29 hours.

**Implementation order (amended 2026-07-21, binding — mirrors workbench design §6.4):** the eval package lands in two logical slices. The **Eval Foundation** scaffold is delivered in B4 and must exist before any pool or labeling work; labeling then follows B6 retrieval-arm completion → union pool generation → grading → delayed second pass → label/pool freeze. The **Eval Metrics/Gate** slice lands in B7 after labels freeze. Labeling never precedes the scaffold.

BenchmarkCase evidence records also retain `dedup_cluster_id`, `cluster_first_available_at`, and `representative_document_id` per the provenance design §8.2, so representative changes never silently alter labeled novelty context.

**Sufficiency ruling (question 3): sufficient for keep/kill only under two binding rules.** (a) *Named-case justification:* every keep/kill decision must cite the specific cases that moved ("dense retrieval recovered primary evidence in cases R3, R7 that FTS5 missed"), never an aggregate delta alone — retrieval N = 10, so one case is 10 percentage points and aggregates alone are noise-vulnerable. (b) *Asymmetric standard:* killing a component on measured absence of benefit is valid at this N; "keeping" earns only a case-scoped claim ("fixed X, Y at Z ms cost"), never a general quality claim. If a comparison is genuinely borderline (1-case delta both directions), the cheaper configuration wins by default. No expansion of the case set is required for the Core milestone; the labeled re-entry condition is a post-milestone decision that a specific comparison needs more resolution.

**BenchmarkCase schema (minimum):** case_id; schema_version; dataset_version; split (core_answerable | core_abstain | retrieval_only | adversarial with parent_case_id); ticker; session_date; cutoff_ts (explicit UTC instant); observable_facts (computed, not annotated); answerability + expected_abstention_reason_class for abstain cases; acceptable_cause_labels (typed, ≤ 20-word descriptors + direction; only where unambiguous — no prose answers anywhere); evidence_judgments (chunk_id, grade 2/1/0, one-line rationale); pool_manifest (which arms' top-K unions were judged, with versions); unjudged handling: any chunk outside the pool is **explicitly unjudged** — reported as unjudged-in-top-K counts, never silently graded 0; lineage (candidate_generation_source, model_assisted_fields, human_confirmed_fields, real per-action timestamps, adjudication_status); annotator_notes.

**Smallest credible labeling protocol (question 6):** (1) select cases via the Context Builder's decomposition over the certified snapshot (material idiosyncratic moves for answerable; quiet/coverage-poor sessions for abstain); (2) build judgment pools from the union of FTS5, dense, hybrid, and reranked top-K plus targeted manual queries (~25–45 chunks/case) — all retrieval arms therefore run before labels freeze; (3) grade each pooled chunk 2/1/0 with a one-line rationale; (4) write acceptable-cause labels *from the graded evidence only* (evidence-first; blocks hindsight import); (5) delayed second pass ≥ 7 days later, disagreements resolved, self-agreement rate recorded and published (the honest solo substitute for inter-annotator agreement); (6) real timestamps per action — the batch-stamp landmine from the legacy set stays as a validator. The binding effort estimate for the 12 Core records is 19–29 total hours (§D line 40). Legacy 65 cases remain dev fixtures with model-assisted lineage; nothing is silently promoted.

## E. Per-layer metric registry (formulas and denominators)

Tags: [B] = Surface A benchmark-only; [R] = Surface B every-run; [J] = cached governed judge; all others deterministic.

**L0 Contract/safety [R]:**
- cutoff_violation_count = |{retrieved or cited chunks with available_at > cutoff}|; denominator: all chunks entering the run; required 0.
- citation_resolution_rate = resolvable cited IDs ÷ all cited IDs; required 1.0.
- visibility_ok = cited IDs ⊆ Judge-visible pack IDs (boolean; catches citations fabricated from parametric memory).
- gate_violation_count = emitted hypotheses whose prerequisite gate failed ÷ n/a (count; required 0).
- trace_completeness = required node events present ÷ required nodes for the taken path; required 1.0.
- identity_ok = corpus snapshot SHA + index manifest ID + prompt SHAs + model IDs all present in the run record (boolean).

**L1 Retrieval [B]:** per case, then mean over cases with a non-empty gold set:
- Recall@8 = |gold(grade ≥ 1) ∩ top-8| ÷ |gold(grade ≥ 1)|.
- primary_hit@8 = 1 if any grade-2 chunk in top-8; aggregate denominator: answerable-class cases whose corpus contains ≥ 1 grade-2 chunk (corpus-coverage failures are excluded from the rate and reported separately as coverage, not retrieval, failures).
- nDCG@8 uses `DCG@8 = Σ((2^rel_i - 1) / log2(i + 1))` for ranks 1–8, grades 2/1/0, and `nDCG@8 = DCG@8 / IDCG@8`; unjudged items remain unjudged rather than grade 0.
- unjudged@8 = count of unjudged chunks in top-8 (reported per case; if > 2 for any case in a decisive comparison, the pool is extended and re-judged before the decision stands).
- Per-case evidence table (ranks by arm for every gold chunk) — this powers the named-case rule.
- Arms: FTS5/BM25, BGE-M3 dense, RRF hybrid; identical filters, cutoff, K.

**L2 Reranker [B]:** candidate_set_preserved (boolean per case; violation excludes the case and counts as a failure); Δ nDCG@8 and Δ primary_hit@8 vs the identical candidate set ordered by fusion score; named improved/regressed case lists; latency median and max per case; failure count (timeout → flagged fallback, excluded pairwise, counted).

**L3 Grounding:**
- citation_validity [R] = L0 resolution + visibility (already covered; not double-reported).
- **structured_context_support [R] (amended split, part 1):** every numeric market/sector/OHLCV claim in the output (returns, decomposition components, volume comparisons) must match the Context Builder's computed values; deterministic; violations counted per run.
- **citation_grounding [B] (amended split, part 2):** causes carrying ≥ 1 citation that resolves to a chunk graded ≥ 1 for that case ÷ **all emitted causes that make an event-narrative claim — including market/sector event narratives** ("sector sold off on X" requires cited pre-cutoff evidence for X). Only causes consisting purely of numeric decomposition statements are exempt from this denominator; there is no blanket market/sector exclusion.
- unsupported_material_claim_count [B][J] = claims contradicted-or-unsupported by their cited chunks' full text, on benchmark/audit samples only; fixed rubric + pinned judge model identity in the cache key; ≥ 20% manual audit; disagreement > 10% blocks the metric's conclusions.
- fact_inference_separation [R] — **demoted from quality score to deterministic structure check:** the four required output sections (verified facts / system calculations / model inference / unavailable evidence) are present and non-empty where applicable (boolean).
- unavailable_evidence_stated [R] = boolean; the report names what it could not see when gates degraded.

**L4 Workflow/systems:**
- answer_abstain_confusion [B]: 2x2 expected answer/abstain versus actual answer/abstain matrix over 8 workflow cases (6 answerable + 2 abstain); abstain_reason_match = abstentions whose stated reason class equals the labeled expected class ÷ labeled abstain cases (right answer for the wrong reason is not full credit).
- error_classification_correctness [B]: injected-failure adversarial variants must land in system_error vs insufficient_evidence as labeled.
- path_validity [R]: node sequence is a legal path of the compiled graph; budget_violations [R] = 0 required; retry/repair counts [R]; expansion rows N/A until bounded expansion is implemented and enabled.
- node latency p50/max [R], tokens and cost per node per run [R] — per-component cost is a keep/kill input (a Critic that fixes one case at 40% of run cost is a different decision than one at 5%).
- replay_ok [B]: zero-network re-execution from the result pack + caches reproduces byte-identical deterministic artifacts and verdicts (live-LLM output variance is measured on reruns, never promised away).

**Removed/rejected (question 4):** any composite score; self-reported confidence anywhere; MRR and Precision@K (redundant with nDCG + Recall at this N); source diversity; risk–coverage curves (need far more N); p95 latency (median + max are sufficient at ≤ 12 Core records); LLM-judged "answer relevance"/prose similarity (no reference answers exist). **Metrics that cannot be computed on arbitrary production runs** — everything tagged [B] — are structurally confined to Surface A; the proposal's own L1/L2 tags were already correct, and this review adds the [R]/[B] tag as a mandatory schema field on MetricRecord so the boundary is machine-checked, not conventional.

## F. Per-run runtime assurance design

The L0 + [R]-tagged L3/L4 checks run inside every attribution run (benchmark, developer, or end-user), reading only the run's own artifacts: state, trace events, evidence pack, manifest identities. The deterministic source-support flags (amended 2026-07-21) — `opinion_only_support`, `unknown_origin_support`, `issuer_claim_only_support`, computed from the evidence pack's `source_class` values per the provenance design §8.1 — are [R] checks emitted in the assurance record. Output: one **assurance record** per run (schema: run_id, check name, status pass|fail|degraded|not_applicable, detail, checked_at), persisted beside the trace and exposed through the internal API. Failures are honest states, not hidden: a cutoff violation aborts the report; a degraded context or partial-coverage flag annotates it. No network, no labels, no judge — assurance must work offline on a laptop against any corpus, which is precisely what makes it credible engineering material rather than benchmark decoration.

## G. UI visibility decision (question 2)

**Benchmark metrics never appear in normal user UI — confirmed and binding.** Recall@8, nDCG, reranker deltas, and any aggregate describe a pinned benchmark corpus; shown to an end user beside a live attribution they read as accuracy claims about *this answer*, which Catalyst prohibits. Normal UI shows exactly the proposed trust surface: ranked explanations, supporting/counter-evidence with source links, information cutoff, data freshness and index lag, degraded/partial state, abstention reason — **plus one amendment: the run-integrity line derived from Surface B** (cutoff enforced ✓, all citations resolve ✓, evidence window complete/partial), because these are facts about the current run and directly serve user trust. Developer Mode gets per-stage ranks (BM25/dense/RRF/reranker), Critic accept/reject decisions, gate outcomes, deterministic check details, node traces, latency, model/prompt/manifest identities. Global results ship as versioned JSON MetricRecords + generated Markdown scorecard + a concise README table; a dedicated evaluation UI stays optional and off the 60% path.

## H. Optional feedback design (question 7)

Isolation contract: feedback rows live in their own store keyed by run_id (verdict useful|partly|questionable; per-cause supported|weak|incorrect|unsure; free text), carrying the run's manifest identities so feedback is interpretable later. Quarantine rules: (1) feedback never writes to any BenchmarkCase field, metric pack, or judge cache; (2) feedback may *nominate* a case into a review queue, but promotion to BenchmarkCase requires the full §D protocol with fresh provenance — the feedback text itself never becomes a label; (3) no fine-tuning or preference data is derived from feedback (the project's no-learning rule); (4) UI copy states feedback is advisory; (5) a landmine test asserts the eval package has no read path from the feedback store into metric computation.

## I. Result-pack and replay contracts (question 9)

**MetricRecord:** record_version; case_id (or run_id for [R] records); config_id/arm; metric name + metric_version; surface tag [B]/[R]; value + unit; status scored|skipped|not_applicable|failed|unjudgeable; reason; denominator description; inputs_hash. Per-case/per-run records are persisted before any aggregation; aggregates are recomputed from records at read time (a landmine test recomputes and compares).

**Pack header:** pack_version; created_at; corpus snapshot SHA; index manifest ID; code commit SHA; prompt SHAs; generation model identities; judge model identity + rubric version (when [J] metrics present); benchmark dataset_version; chunk-profile versions; runtime/eval configuration dump. A pack missing any required identity field fails validation and cannot be consumed by the regression gate.

**Replay:** the zero-network regression gate consumes committed packs + judge cache with cache-or-fail semantics; byte identity is required only for deterministic artifacts and cached-verdict replay. Live-model variance is optional Showcase work, never promised as zero by the Core gate.

**Git boundaries:** (1) eval package restructure code; (2) benchmark labels + pool manifests (immutable once referenced; separate from code); (3) result packs + scorecards (separate from the code that produced them, binding a pre-existing code SHA); (4) the package-boundary fix (§J); (5) runtime assurance wiring in agents/app. Never tracked: raw payloads, embeddings, provider probes, feedback stores containing user text.

## J. Package boundaries (questions 8 and the import rule)

**B1 boundary correction:** the graph adapter lives in `catalyst_eval/adapters/`, agents declares no eval dependency, and the adapter consumes plain graph state. An import-direction test keeps the dependency arrow from regressing.

**Framework decision (question 8): build the small in-repo eval package; adopt none of Ragas/DeepEval/TruLens as runtime dependencies.** B1 removes the unused `ragas` optional extra and deletes stale thesis-era tests/scripts instead of preserving an ambient-red or fake legacy suite. Grounds: Catalyst's load-bearing metrics are deterministic and domain-specific (cutoff, gates, visibility, replay identity); the frameworks' center of gravity is judge-metric libraries, exactly what this design minimizes; each brings a heavy dependency chain into a package whose regression gate must run offline; and a small readable eval package is itself contributor-facing portfolio material. Patterns are borrowed per §B with attribution in docs; dependencies are not.

Resulting structure: `catalyst_eval/` → benchmark/ (cases, lineage, validators), retrieval_eval/, grounding_eval/, workflow_eval/, judges/ (cache with model identity in the key), adapters/, packs/, reports/, and gates/. **Runtime assurance ownership is resolved: RunAssuranceRecord and all Surface-B checks live in `catalyst_agents/runtime/assurance/`, produced by the runtime on every run; eval consumes and re-verifies persisted assurance artifacts and never provides the runtime path.** The runtime never imports eval; the assurance record schema is a versioned artifact contract documented alongside the trace schema.

## K. Scope cuts (adversarial trims to the proposal)

- The L3 judge runs on benchmark/audit samples only; additionally, **no [J] metric may call the network in CI**. Regression gates consume cached verdicts only.
- "Fact/inference separation" as a scored quality metric — cut; structure check only (§E).
- Reranker pairwise ordering accuracy — cut (Δ nDCG + named cases carry the decision).
- A dedicated evaluation UI — deferred, as proposed.
- Cross-family second judge — one-time robustness footnote at most; not per-run, not per-pack.
- Any aggregate "Catalyst score," radar chart, or letter grade — prohibited.
- Feedback-driven auto-nomination pipelines — deferred; a manual review queue suffices at 60%.

## L. Implementation-plan amendments

1. Workbench design §6 (Evaluation Lite): bind the §E registry with surface tags and denominators; add the named-case and asymmetric keep/kill rules; add the run-integrity UI line to §5's frontend rules; record the L0 assurance record schema.
2. Provider/chunking design: no changes required; its landmine tests 16 and 21–24 (parity, shared payloads, redaction) are consumed by L0 checks.
3. B4 plan: create Eval Foundation schemas before retrieval pools are persisted; the legacy metric set is formally superseded by §E.
4. B7 plan: gate reads MetricRecord packs; judge-cache keys include judge-model identity; [R]-tag enforcement is part of pack validation.
5. New slice: package-boundary fix (§J) — adapter relocation, dependency removal, import-direction test; lands before the eval restructure.
6. Benchmark labeling slice: §D protocol, 12 base records; depends on the Eval Foundation slice and on all retrieval arms (pool-before-freeze); the Eval Metrics/Gate slice follows label freeze.

## M. Landmine tests

1. A post-cutoff chunk planted in a fixture pack fails L0 on every surface, including a production-shaped run.
2. A cited ID absent from the Judge-visible pack fails visibility_ok even when it resolves in the corpus (memory-fabricated citation).
3. An emitted hypothesis whose gate inputs are absent fails gate_violation_count.
4. A [B]-tagged metric invoked against a non-benchmark run raises; the surface tag is enforced, not advisory.
5. A pack missing any required identity header field is rejected by the regression gate.
6. Aggregates recomputed from per-case records must equal stored aggregates byte-for-byte.
7. Judge verdicts cached under one judge model are cache misses under another (model identity in key).
8. `import catalyst_eval` anywhere under `catalyst_agents` fails the import-direction test; agents' test suite passes with eval uninstalled.
9. The feedback store has no import path into metric computation; a synthetic feedback row cannot alter any MetricRecord.
10. Uniform batch timestamps or annotation dates predating session dates in a BenchmarkCase file fail lineage validation (legacy-set regression).
11. An unjudged chunk in top-8 is counted as unjudged, never as grade 0, in nDCG/Recall computation.
12. Zero-network replay of a committed pack succeeds with networking disabled; any socket attempt fails the gate.
13. A benchmark aggregate rendered into the normal-UI payload schema fails an API contract test (UI visibility rule is machine-enforced).

## N. Final MUST / SHOULD / DEFER

| Tier | Items |
|---|---|
| MUST | L0/[R] runtime assurance + assurance record; package-boundary fix; BenchmarkCase schema + 12-base-record labeling protocol; L1 retrieval comparison with per-case evidence tables; workflow scorecard (6+2); result packs + Markdown scorecard; zero-network replay; named-case + asymmetric keep/kill rules |
| SHOULD | L2 reranker checks (GPU-dependent); L3 [J] grounding on cached samples with ≥ 20% audit; 4 adversarial variants; Developer Mode surface; run-integrity line in normal UI; feedback capture with quarantine; live-variance 3× rerun measurement |
| DEFER | dedicated evaluation UI; cross-family judge; feedback nomination pipeline automation; case-set expansion; any CI-hosted paid judge runs; expansion-utility metrics until bounded expansion lands; NLI/entailment models (re-entry: claim volume ~10× current) |

---

Historical review note: the original review was conducted read-only. This binding version has since received documentation-only consolidation amendments; current integrity evidence belongs to the B1/B2–B7 handoff report rather than this historical note.
