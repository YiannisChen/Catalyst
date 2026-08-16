# Catalyst vNext Technical Design — Frozen V1

**Status:** FROZEN V1 — implementation baseline

**Frozen on:** 2026-08-16

**Revision:** V1.0.3 — final temporal and execution clarifications incorporated

**Code baseline:** 99e584d57f5192418de697b6c2c49eb0ceb273d5

**Scope:** packages/data-core, packages/agents, packages/eval, and the packages/app API/SSE boundary

## 1. Purpose

Catalyst vNext is a bounded financial event-attribution system. It accepts a security identity, a market session, and a user question; retrieves only point-in-time eligible evidence; assesses whether that evidence supports a causal explanation; and returns a cited SUFFICIENT, PARTIAL, or ABSTAIN result.

The project demonstrates production-shaped AI systems work:

- trustworthy financial data and provenance;
- cutoff-safe hybrid retrieval and reranking;
- evidence-bounded agent decisions;
- real streaming user experience;
- reproducible evaluation and runtime identity;
- observable latency, cost, failure, and refusal behavior.

The goal is not to maximize the number of agents, tools, or infrastructure components. The goal is to make one attribution workflow reliable and measurably useful.

## 2. Normative language and change control

MUST, MUST NOT, SHOULD, and MAY are normative.

This document freezes architecture and externally visible contracts. Internal filenames and helper structure may change without an amendment if all frozen contracts remain true.

A change requires a design amendment before implementation if it changes:

- package ownership or dependency direction;
- temporal, ticker, eligibility, or evidence-materiality semantics;
- the attribution state machine or follow-up bound;
- public API or SSE event semantics;
- runtime/evaluation identity rules;
- hard safety or quality gates;
- the declared non-goals.

Repository facts that require inspection during planning are audit items, not permission to redesign the architecture. If a frozen decision is impossible because of verified repository or provider behavior, implementation stops and raises a narrowly scoped amendment.

## 3. Product contract

### 3.1 Input and structured identity

The public run request contains:

    RunRequest
    - ticker: canonical uppercase ticker
    - session_date: exchange-local YYYY-MM-DD session
    - question: bounded attribution question
    - model_selection: validated provider/model reference

At run creation Catalyst resolves and persists:

    TemporalIdentity
    - session_date
    - market_timezone
    - session_open_at
    - session_close_at
    - information_window_start_at
    - cutoff_at

The structured ticker and TemporalIdentity are authoritative. Dates, tickers, brands, and issuer phrases in the question are query hints only and MUST NOT override structured identity.

### 3.2 Output

    AttributionResult
    - run_id
    - status: SUFFICIENT | PARTIAL | ABSTAIN
    - attribution_type: EVIDENCE_BACKED_CAUSAL | NO_MATERIAL_PUBLIC_CATALYST
    - observed_move
    - primary_attribution?
    - secondary_factors[]
    - selected_evidence[]
    - rejected_evidence_summary[]
    - citations[]
    - coverage_gaps[]
    - limitations[]
    - runtime_manifest_id
    - duration_ms
    - cost

Every material factual or causal claim MUST resolve to selected evidence from the same run. The UI MUST be able to show:

    retrieved candidate
    → rerank decision
    → evidence assessment
    → claim support
    → final citation

### 3.3 Status semantics

**SUFFICIENT** means eligible evidence supports a primary explanation, all material claims are cited, and no unresolved material contradiction remains. It requires direct primary evidence or corroborating independent reporting. Commentary-only evidence cannot produce SUFFICIENT.

**PARTIAL** means credible evidence exists but a material gap remains, such as weak magnitude coverage, missing primary confirmation, or an unresolved secondary mechanism. PARTIAL is a valid result, not a system failure.

**ABSTAIN** means Catalyst cannot responsibly make a causal attribution from eligible evidence. Missing local coverage, unsupported market-structure explanations, and unresolved evidence contradictions are valid abstention reasons. A runtime identity, schema, database, index, or other integrity failure is a system failure, not an abstention.

Request and system failures are separate terminal states and MUST NOT be reported as ABSTAIN.

`NO_MATERIAL_PUBLIC_CATALYST` is a bounded negative finding, not proof that no
cause exists. It is permitted only after the configured company-primary and
company-news sanity tasks complete without material support. If point-in-time
market/sector/peer context and local coverage are adequate, the result may be:

    status = PARTIAL
    attribution_type = NO_MATERIAL_PUBLIC_CATALYST
    primary_attribution = no material public catalyst was identified;
                            the move was broadly consistent with observed context

The result MUST disclose unsupported positioning, flow, options, short-interest,
or other market-structure coverage. It can never be `SUFFICIENT`. If local
coverage or structured context is inadequate, the system returns `ABSTAIN`
with explicit coverage gaps instead.

### 3.4 Non-goals

V1 is not:

- a trading, forecasting, or recommendation system;
- an intraday microstructure or institutional event-study engine;
- a general-purpose ReAct or deep-research agent;
- a multi-agent or supervisor-agent system;
- a web-scale crawler or Bloomberg replacement;
- a multi-tenant SaaS platform;
- a microservice, Kafka, Celery, Temporal, Redis, or Kubernetes project.

External web search, market-structure datasets, an LLM research planner, and benchmarks larger than the initial human set are deferred until evaluation shows a specific need.

## 4. Architecture and package boundaries

### 4.1 Python dependency direction

    packages/app ───────► packages/agents ───────► packages/data-core
          │                                              ▲
          └──────────────────────────────────────────────┘

    packages/eval: offline consumer of versioned serialized artifacts;
                   integration runners may use explicit optional adapters.

Rules:

- data-core MUST NOT import agents, eval, or app.
- agents MAY import data-core; it MUST NOT import eval or app.
- production app MAY import agents and data-core; it MUST NOT require eval.
- eval metric/report code consumes documented, versioned artifacts and MUST NOT inject benchmark logic into production paths.
- eval integration scripts MAY invoke production entry points through explicit adapters or optional extras.
- no new catalyst-contracts package is introduced in V1.

This differs from runtime data flow:

    data evidence → agents workflow → app transport/UI
           └──────────────→ eval offline measurement

### 4.2 Ownership

| Package | Owns | Does not own |
|---|---|---|
| data-core | canonical assets, provenance, source taxonomy, temporal identity, eligibility, query policy, retrieval, scores/ranks, data runtime identity | causal conclusions, prompts, final status |
| agents | observation, research tasks, evidence assessment, bounded follow-up, claim plan, attribution status/result, run manifest, trace and runtime assurance | canonical storage, index truth, benchmark truth, HTTP transport |
| eval | golden cases, evidence judgments, metrics, eval manifests, four-arm ablation, regression reports and gates | production routing, refusal policy, runtime side effects |
| app | HTTP request admission, background execution, SSE, cancellation, local credential wiring and Workbench projection | retrieval algorithms, causal decisions, benchmark metrics |

## 5. Data-core design

### 5.1 Canonical evidence model

V1 uses common asset identity plus typed subtype metadata. It does not force all provider records into one giant table.

    CanonicalAsset
    - asset_id
    - asset_type: NEWS | FILING | OFFICIAL_RELEASE | STRUCTURED_CONTEXT
    - issuer_id
    - tickers[]
    - provider
    - publisher?
    - canonical_url?
    - source_class
    - source_published_at?
    - eligible_at
    - ingested_at
    - temporal_precision
    - content_state
    - serving_status
    - title?
    - content_ref?
    - content_hash?
    - dedup_cluster_id?
    - parse_quality
    - subtype_metadata

Filing subtype metadata includes accession number, form type, filing date, accepted timestamp when known, and section key. News subtype metadata includes publisher and canonical URL when known.

Provider, publisher, and source class are distinct. Provider describes ingestion; publisher describes publication; source class describes evidence role.

V1 reuses the repository source classes:

- structured_market_data
- official_government
- issuer_disclosure
- corporate_press_release
- reported_news
- analysis_opinion
- aggregated_unknown

No numeric publisher-authority score is introduced. Agents use this frozen V1
mapping:

| Source class | Evidence role | Permitted use |
|---|---|---|
| `structured_market_data` | `STRUCTURED_CONTEXT` | observation, magnitude, market/sector/peer context; not documentary causal proof |
| `official_government` | `PRIMARY_AUTHORITY` | direct primary support only for a government, regulatory, or macro claim within the source's scope |
| `issuer_disclosure` | `DIRECT_PRIMARY` | direct support for issuer facts within the disclosure's scope |
| `corporate_press_release` | `DIRECT_PRIMARY` | direct support for issuer announcements; independence from the issuer is not implied |
| `reported_news` | `INDEPENDENT_REPORT` | support when the report contains relevant attributable facts and passes independence checks |
| `analysis_opinion` | `COMMENTARY_LEAD` | query hint, follow-up trigger, or weak context; never sufficient alone for a primary causal claim |
| `aggregated_unknown` | `UNKNOWN` | lead-only unless another deterministic rule establishes a stronger typed source |

Evidence role is a ceiling, not an automatic relevance judgment. A direct
primary source outside the claim's scope does not support that claim.

### 5.2 Content state and materiality

Canonical content state is:

    FULL_TEXT | TITLE_ONLY | METADATA_ONLY | EMPTY | FAILED

The current serving corpus may include metadata_only rows. V1 preserves that for existence checks and retrieval leads; it does not silently redefine the existing SEARCHABLE_STATUSES contract.

Material-evidence rules are stricter:

- FULL_TEXT MAY support a final claim after all gates pass.
- TITLE_ONLY MAY be a lead and MAY support only a narrowly scoped existence claim when provenance is authoritative. It MUST NOT independently support a complex causal mechanism.
- METADATA_ONLY MAY support calendar/existence checks and follow-up planning. It MUST NOT be passed to the model as document body or support a material causal claim.
- EMPTY and FAILED MUST NOT be retrieved as evidence.

Retrieval therefore carries both serving eligibility and material evidence utility. Agents MUST NOT infer materiality from a serving status name.

### 5.3 Temporal identity and ticker policy

TemporalIdentity and QueryPolicy are owned by data-core and used unchanged through every retrieval arm.

The temporal center comes from structured session/cutoff identity. A date in free text is recorded as a query-date hint. A conflict is recorded and MUST NOT override the structured value.

Ticker and issuer claims retain provenance:

    symbol | issuer_brand | issuer_phrase

Only an unambiguous foreign ticker symbol permits a hard mismatch. Brands, competitor mentions, issuer phrases, and ambiguous single letters fail open as context. A hard mismatch exits before cutoff calculation, retrieval, or provider calls.

### 5.4 Retrieval contract

Text evidence follows one production path:

    QueryPolicy
    → eligibility/ticker/cutoff filters
    → lexical and dense candidate retrieval
    → reciprocal-rank fusion
    → duplicate collapse
    → bounded reranker
    → RetrievalResultSet

Lexical and dense SHOULD execute concurrently when resources permit. Structured market, peer, macro, and fundamental context uses deterministic SQLite lookup and joins the common observation/evidence contract; it is not forced into LanceDB.

Every retrieval hit carries nullable per-stage scores/ranks, evidence identity and excerpt, ticker/issuer associations, provider/publisher/source class, timestamps, content/materiality state, dedup/parse metadata, retrieval policy, TemporalIdentity, and DataRuntimeIdentity.

Scores MUST be finite. Ranks MUST be positive contiguous integers within the result produced by a participating stage. A missing modality uses null, never a fabricated score.

### 5.5 Lexical fallback

The broad OR path is bounded:

1. Run one strict normalized query with hard result and latency limits.
2. If hits are below the candidate floor, run at most one fallback with at most three normalized OR terms and its own latency limit.
3. On timeout, continue with dense results and record degradation.
4. Recursive fallback or unbounded expansion is forbidden.

Exact limits are pinned runtime configuration in RunManifest, not architecture constants.

### 5.6 Data runtime identity

Every retrieval result binds to:

    DataRuntimeIdentity
    - data_snapshot_id
    - corpus_manifest_id
    - fts_index_version
    - dense_index_version?
    - embedding_model_revision?
    - reranker_revision?
    - query_policy_version

Actual values emitted by runtime components are persisted. Eval and runners MUST NOT recreate them independently from case input.

## 6. Agents design

### 6.1 Fixed bounded state machine

    QUERY_VALIDATION
    → OBSERVATION_BUILD
    → INITIAL_RESEARCH_POLICY
    → RETRIEVE
    → RERANK_NORMALIZE
    → EVIDENCE_ANALYST
        ├─ READY
        ├─ ABSTAIN
        └─ FOLLOW_UP → one typed action → RETRIEVE/RERANK → EVIDENCE_ANALYST
    → CLAIM_PLAN
    → CLAIM_VALIDATOR
    → STREAMING_ANSWER_WRITER
    → POST_STREAM_ASSURANCE
    → FINALIZER

There is no open-ended loop. max_followups = 1 is a frozen bound. There is no independent LLM planner in V1.

### 6.2 Observation and initial research

Observation contains available point-in-time target move, market/sector/peer context, volume context, scheduled macro flags, and local coverage/freshness. Missing context remains explicit.

A `ResearchTask` separates what evidence is needed from when to search:

    ResearchTask
    - task_id
    - evidence_need
    - time_scope
    - lookback_sessions?
    - query_hints[]
    - retrieval_policy_id

    EvidenceNeed =
      COMPANY_PRIMARY | COMPANY_NEWS | SECTOR_NEWS |
      MACRO_EVENT | MACRO_SERIES | FUNDAMENTALS | MARKET_STRUCTURE

    TimeScope =
      SESSION_INFORMATION_WINDOW | PRIOR_SESSION | LOOKBACK_SESSIONS

`PRIOR_SESSION` is therefore a time scope, not an evidence type.

For attribution of session D, `SESSION_INFORMATION_WINDOW` is the closed
point-in-time interval from the previous regular session close through D's
attribution cutoff. It intentionally includes after-close, overnight, and
pre-market information that may explain D's move. It MUST NOT include evidence
whose `eligible_at` is after the current cutoff.

`PRIOR_SESSION` resolves the equivalent information window for the immediately
preceding regular session. `LOOKBACK_SESSIONS(n)` begins at the close n regular
sessions before D and ends at D's cutoff. All boundaries are computed by the
exchange calendar in data-core, not by subtracting calendar days or by the
model. This contract ensures that, for example, Monday 16:05 earnings are
eligible evidence for Tuesday's regular-session attribution.

The deterministic `InitialResearchPolicy` classifies the observation using
pinned runtime thresholds and the following precedence. It emits the listed
tasks, deduplicates identical tasks, and MUST emit no more than three:

| Scenario, in precedence order | Required observation | Initial tasks |
|---|---|---|
| `SCHEDULED_MACRO` | major scheduled macro flag and broad market/sector co-move | `MACRO_EVENT × SESSION_INFORMATION_WINDOW`, `MACRO_SERIES × SESSION_INFORMATION_WINDOW`, `COMPANY_NEWS × SESSION_INFORMATION_WINDOW` sanity check |
| `CONTINUATION` | material prior-session move and current move consistent with continuation, without a stronger scheduled-macro classification | `COMPANY_PRIMARY × PRIOR_SESSION`, `COMPANY_NEWS × PRIOR_SESSION`, `COMPANY_NEWS × SESSION_INFORMATION_WINDOW` sanity check |
| `BROAD_SECTOR` | target direction/magnitude broadly aligned with sector and peers | `SECTOR_NEWS × SESSION_INFORMATION_WINDOW`, `MACRO_EVENT × SESSION_INFORMATION_WINDOW`, `COMPANY_NEWS × SESSION_INFORMATION_WINDOW` sanity check |
| `COMPANY_SPECIFIC` | target move materially diverges from market, sector, or peers | `COMPANY_PRIMARY × SESSION_INFORMATION_WINDOW`, `COMPANY_NEWS × SESSION_INFORMATION_WINDOW` |
| `QUIET_OR_UNCLASSIFIED` | no stronger scenario or observation context is incomplete | at most `COMPANY_NEWS × SESSION_INFORMATION_WINDOW` and `COMPANY_PRIMARY × SESSION_INFORMATION_WINDOW` |

The thresholds defining `material`, `broadly aligned`, `quiet`, and volume
bands are configuration, must be recorded in `RunManifest`, and are calibrated
by eval. They do not change this decision table.

The `EvidenceNeed × TimeScope` mapping is also frozen:

| Evidence need | Retrieval strategy |
|---|---|
| `COMPANY_PRIMARY` | ticker hard filter; requested time scope; hybrid text retrieval; `issuer_disclosure`, `corporate_press_release`, and claim-relevant `official_government`; filing form/section filters when present |
| `COMPANY_NEWS` | ticker hard filter; requested time scope; hybrid text retrieval; `reported_news`; `analysis_opinion` and `aggregated_unknown` may enter only as leads |
| `SECTOR_NEWS` | point-in-time sector/peer scope from Observation; same cutoff; hybrid text retrieval; no implicit external lookup |
| `MACRO_EVENT` | point-in-time calendar/official-source filter plus eligible text retrieval; same cutoff |
| `MACRO_SERIES` | deterministic structured SQLite lookup; no fabricated lexical/dense score |
| `FUNDAMENTALS` | deterministic structured SQLite lookup; normally follow-up only in V1 |
| `MARKET_STRUCTURE` | no execution backend in V1; records a non-recoverable coverage gap and MUST NOT create a retrieval action |

The policy selects a typed strategy; it never exposes arbitrary SQL, tool
selection, source allowlists, ticker, or cutoff control to the model.

Independent initial ResearchTasks execute with bounded parallelism under one
shared stage deadline. They MUST NOT run serially solely because they were
listed in policy order. Concurrency is capped by runtime configuration and the
results merge deterministically by task priority, task ID, and within-task
rank. Per-task degradation remains observable; an identity/integrity failure
still fails the run rather than being hidden by successful sibling tasks.

Market proxies are not an architecture blocker. Implementation audits the local universe and uses only available point-in-time instruments. Missing proxies produce a coverage gap; they are not fetched or fabricated implicitly.

### 6.3 Evidence Analyst

The current Critic evolves in place into an Evidence Analyst. It receives Observation, ResearchTask, complete bounded evidence metadata, and a deterministic `CoverageSummary`—not only truncated text and a generic source type.

Before any analyst model call, code computes:

    CoverageSummary
    - material_evidence_count
    - primary_authority_count
    - direct_primary_count
    - independent_report_count
    - commentary_lead_count
    - unknown_count
    - independence_groups[]
    - duplicate_or_syndicated_count
    - parse_degraded_count
    - metadata_only_count
    - market_co_move
    - sector_co_move
    - peer_co_move
    - scheduled_macro_present
    - retrieval_degradations[]
    - local_coverage_gaps[]

    EvidenceAssessment
    - evidence_decisions[]
    - candidate_claims[]
    - magnitude_fit: STRONG | PLAUSIBLE | WEAK | UNKNOWN
    - conflicts[]
    - missing_evidence[]
    - decision: READY | FOLLOW_UP | ABSTAIN
    - status_ceiling: SUFFICIENT | PARTIAL | ABSTAIN

Each evidence decision is SUPPORT, CONTRADICT, WEAK, LEAD_ONLY, or IRRELEVANT.

Code and model responsibilities are frozen:

| Code decides | Evidence Analyst decides |
|---|---|
| ticker/cutoff/runtime identity validity | what claim a passage supports or contradicts |
| content materiality and source-role ceiling | whether the evidence is semantically relevant |
| dedup/syndication independence groups | the plausible causal mechanism |
| deterministic market/sector/peer comparisons | semantic conflicts and missing evidence |
| maximum permitted attribution status | magnitude fit within the supplied structured context |

Content-level independence is the V1 contract. Evidence is not independent if
it shares a dedup cluster, canonical document/article identity, canonical
content hash, or known syndication lineage. Different URLs or publishers alone
do not establish independence. If lineage is ambiguous, the items do not count
as separate support for a `SUFFICIENT` gate.

After the model returns, code applies status ceilings:

- no eligible material support permits at most `ABSTAIN`;
- commentary/unknown-only support cannot exceed `PARTIAL` and normally produces
  `ABSTAIN` when it does not establish even a bounded factual lead;
- one independent report without direct primary support cannot exceed
  `PARTIAL`;
- direct primary support or at least two independent reports may permit
  `SUFFICIENT`, subject to semantic relevance, magnitude, citation, and
  contradiction gates;
- unresolved material contradiction cannot exceed `PARTIAL`; a contradiction
  that defeats the proposed primary explanation requires `ABSTAIN`;
- runtime identity, schema, or integrity failure produces a system failure and
  cannot be downgraded to an attribution status.

The Evidence Analyst may lower a status or request follow-up. It MUST NOT raise
the final status above the deterministic ceiling. A `READY` model decision is
therefore necessary but never sufficient for `SUFFICIENT`.

### 6.4 Follow-up

Evidence gaps and follow-up actions are first-class versioned artifacts:

    MissingEvidence
    - gap_id
    - evidence_need: EvidenceNeed
    - time_scope: TimeScope
    - expected_information
    - reason_code
    - recoverable: bool

    FollowUpAction
    - action_id
    - gap_id
    - evidence_need: EvidenceNeed
    - time_scope: TimeScope
    - lookback_sessions?
    - query_hints[]
    - research_fingerprint

V1 reason codes are bounded to:

    MISSING_PRIMARY_CONFIRMATION
    MISSING_INDEPENDENT_CORROBORATION
    MISSING_PRIOR_SESSION_CONTEXT
    MISSING_SECTOR_CONTEXT
    MISSING_MACRO_CONTEXT
    MISSING_FUNDAMENTAL_CONTEXT
    CONFLICT_REQUIRES_RESOLUTION
    MARKET_STRUCTURE_UNSUPPORTED
    LOCAL_COVERAGE_GAP

When the first assessment identifies one recoverable gap, it may request one
typed follow-up. A `FollowUpAction` MUST reference a `MissingEvidence` record
from the same assessment with `recoverable = true`; its need and time scope
must match that gap. `MARKET_STRUCTURE_UNSUPPORTED` and capabilities without a
local backend are non-recoverable and MUST NOT create an action.

The model may propose evidence need, time scope, expected information, and
bounded query hints. Code assigns IDs/fingerprint and controls ticker, temporal
identity, source filters, result count, time window, timeout, backend, and
budget. Gap and action artifacts are exposed to trace, eval, and safe UI
projection without hidden reasoning.

Follow-up stops when one has already run, a research fingerprint repeats, no new independent evidence appears, budget expires, or the capability is unavailable. Unsupported market-structure needs become coverage gaps.

The second Evidence Analyst invocation always receives cumulative evidence:

    EvidenceState(t+1) = dedup(EvidenceState(t) ∪ NewEvidence)

Each evidence item records `first_seen_round`, all contributing
`research_task_ids`, and its current accepted/lead/rejected state. Round-two
analysis MUST NOT discard valid round-one evidence or treat the follow-up as an
isolated RAG run. Deduplication and independence grouping are recomputed over
the cumulative state before the second assessment.

### 6.5 Claim plan and streamed answer

The Evidence Analyst produces candidate claims rather than public prose. Code
normalizes those claims and accepted evidence decisions into a ClaimPlan:

    ClaimPlan
    - claim_id
    - claim_type
    - attribution_type
    - display_statement
    - mechanism?
    - support_evidence_ids[]
    - counter_evidence_ids[]
    - status
    - limitations[]

A deterministic validator verifies evidence identity, selection, eligibility, materiality, ticker/cutoff identity, contradiction rules, status gates, and citation ownership before writing begins.

ClaimPlan construction does not make an additional model call. The normal
logical model-call budget is:

| Path | Logical model calls |
|---|---:|
| no follow-up | one Evidence Analyst + one streaming Writer = 2 |
| one follow-up | two Evidence Analyst calls + one streaming Writer = 3 |

The only additional provider attempts permitted are the bounded technical
retries in §6.6. No Hypothesis, Planner, or ClaimPlan model call may be inserted
without a design amendment.

The writer is an expression layer over a validated ClaimPlan. It MAY paraphrase and connect validated statements, but MUST NOT retrieve or add material facts. Runtime assurance can guarantee structural constraints—citation resolution, evidence membership, required sections, output completion, status, and plan/citation identity. Pure semantic equivalence cannot be perfectly guaranteed by deterministic code; unsupported semantic drift is measured offline and is not advertised as a perfect runtime guarantee.

Text emitted before assurance is labelled PROVISIONAL_RENDERING. If assurance fails, the answer is invalidated and the run fails closed. The system MUST NOT retry merely to obtain a preferred status or narrative.

### 6.6 Retry policy

A model call MAY retry once only for transport error, timeout, or invalid structured schema, using the same evidence and instructions. Semantic dissatisfaction, PARTIAL, or ABSTAIN is not a retry reason.

### 6.7 Run identity, trace, and assurance

Agents owns RunManifest, composing DataRuntimeIdentity with code revision, workflow/policy version, analyst and writer prompt hashes, provider/model/revision, token and timeout budgets, and runtime configuration.

Every run persists versioned artifacts for observation, retrieval/rerank, evidence assessment, optional follow-up, claim plan, answer, and assurance. Trace events contain lifecycle metadata and artifact references; large payloads live in artifacts. Secrets and hidden chain-of-thought are never persisted.

Production runtime owns RunAssuranceRecord. Eval independently revalidates persisted evidence; it does not own production assurance.

## 7. Eval design

### 7.1 Evaluation layers

V1 separates:

1. Offline retrieval eval, which measures finding/ranking evidence without an answer.
2. Four-arm ablation, using the repository-defined fts5, dense, hybrid, and reranked arms from one pinned production runner.
3. End-to-end attribution eval, which measures selected evidence, causal claims, citations, status/refusal, latency, and cost.

Four-arm is controlled ablation and union-pool generation, not four production runtimes.

### 7.2 Human truth and judges

The first benchmark contains 12 manually reviewed cases. Existing T4 cases may be reused only after explicit human re-annotation under the V1 schema. Cases cover company-specific, sector/macro, continuation, distractor, quiet, and abstention behavior.

    GoldenCase
    - case_id
    - ticker/session_date/cutoff/question
    - oracle_status
    - acceptable_cause_labels[]
    - evidence_judgments[]
    - expected_primary_evidence[]
    - expected_refusal_reason?
    - expected_attribution_type?
    - expected_research_behavior:
        acceptable_initial_tasks[]
        expected_gap_reason_codes[]
        acceptable_followup_actions[]
        followup_recoverable: bool
        followup_required: bool
    - notes

Human labels are primary truth for relevance, cause, and answerability. An LLM judge MAY score language quality and provide regression hints; it MUST NOT define retrieval, cause, or answerability ground truth.

Research-behavior labels describe acceptable task sets rather than one brittle
exact query string. Human annotation decides whether a gap is recoverable and
whether follow-up is required; the implementation cannot infer those labels
from its own output.

### 7.3 Metrics

Retrieval reports Recall@K, MRR, nDCG@K, primary-source hit rate, duplicate-adjusted precision, independent evidence recall, source diversity, reranker contribution, violations, latency, and degradation.

Attribution reports evidence precision, citation correctness, causal relevance, material coverage, unsupported claims, the status confusion matrix, refusal correctness, conflict handling, latency, tokens, and cost.

Agent behavior reports:

- follow-up trigger precision and recall;
- gap reason-code identification accuracy;
- follow-up action match against the acceptable need/time-scope set;
- stop correctness after ready, non-recoverable, repeated, or exhausted states;
- useful-follow-up rate and unnecessary-follow-up rate;
- attribution delta between no-follow-up and bounded-follow-up policies.

Each metric reports its eligible denominator. In particular, follow-up recall is
measured only on human-labelled recoverable/required cases, while unnecessary
follow-up rate is measured on cases labelled not required.

Reports MUST show integer case counts with percentages. Twelve cases do not justify spurious decimal precision.

### 7.4 Pinned evidence and invalidation

Official eval binds inputs and outputs to RunManifest and EvalManifest. Actual runtime values are extracted from production artifacts. Missing, wrong, or conflicting identity fails closed and produces no success token.

Data/corpus/index/query-policy changes invalidate retrieval evidence. Model, prompt, workflow, or budget changes invalidate attribution output. Old evidence remains historical but is labelled STALE_FOR_CURRENT_RUNTIME.

## 8. API and true SSE streaming

### 8.1 Run creation and execution

POST /api/live-runs returns after durable admission:

    {
      "run_id": "...",
      "status": "ACCEPTED",
      "stream_url": "/api/live-runs/{run_id}/stream"
    }

The app uses a managed bounded executor, not an unbounded daemon thread per request. V1 remains a single-process local Workbench. Concurrent run capacity is pinned between 2 and 4 after load testing. SQLite queue claiming MUST be atomic.

### 8.2 Event stream

GET /api/live-runs/{run_id}/stream returns text/event-stream.

Normative lifecycle events are:

    run.accepted
    stage.started
    evidence.retrieved
    evidence.reranked
    evidence.assessed
    followup.started          # optional
    answer.started
    answer.delta
    answer.completed
    assurance.completed
    run.completed | run.failed | run.cancelled

Every lifecycle event has a versioned envelope:

    id: {run_id}:{sequence}
    event: {event_type}
    data:
      schema_version
      run_id
      sequence
      emitted_at
      stage
      payload
      artifact_refs[]

Sequence is strictly increasing. Events are persisted before emission. Provider tokens are buffered for approximately 30–80 ms and emitted as persisted answer.delta chunks; persistence is per chunk, not per token. Delta content MUST come from the provider streaming API and MUST NOT be a completed answer sliced after generation.

Last-Event-ID replays later events and then tails live events. Delivery is at least once; clients deduplicate by sequence. A 10-second heartbeat is a non-persisted, non-sequenced transport comment.

### 8.3 Evidence and answer visibility

evidence.retrieved exposes safe candidate summaries as soon as retrieval finishes: evidence ID, headline, publisher, publication time, source class, task ID, and candidate count.

evidence.reranked exposes selected IDs, rank changes, duplicate drops, reranker usage/degradation, and latency. Missing quality fields are not fabricated.

evidence.assessed exposes accepted, lead-only, and rejected IDs; bounded reason codes; typed MissingEvidence records; decision; and status ceiling. It does not expose hidden reasoning. The existing internal `critic` node may emit this public semantic event; internal node names are not part of the SSE contract.

followup.started exposes `action_id`, `gap_id`, evidence need, time scope, bounded
reason code, and research fingerprint. It does not expose hidden analysis or
model-generated tool arguments outside the validated FollowUpAction contract.

answer.started binds the stream to a validated ClaimPlan and citations. answer.completed records usage and citations. assurance.completed records validity, violations, and final status.

### 8.4 Cancellation and timeout

POST /api/live-runs/{run_id}/cancel requests cooperative cancellation at retrieval, model-call, and streaming boundaries. Providers that support cancellation are aborted. Results from a non-cancellable in-flight call are discarded and MUST NOT complete the run.

The run-level timeout is pinned in RunManifest; the initial target is 60 seconds with narrower stage budgets. Timeout produces run.failed with a stable reason code.

SSE is frozen instead of WebSocket because the flow is server-to-client; create and cancel remain ordinary HTTP.

## 9. Reliability, security, and observability

### 9.1 Storage and execution

- SQLite is canonical for metadata, run state, ordered events, artifacts, and structured context. Writes are short transactions; WAL is used where compatible.
- LanceDB owns only dense vector serving. It is never canonical truth.
- Model providers own inference only. They do not decide identity, evidence authority, or final status.
- A bounded executor and semaphore provide backpressure.
- Idempotency-Key prevents duplicate active runs for the same normalized request.

### 9.2 Provider capability

The production writer adapter MUST expose real streaming. A provider/model is admitted only after a capability probe verifies streaming, timeout, token accounting, and error normalization. A non-streaming provider may be used by offline tests but cannot satisfy the V1 Workbench writer contract.

### 9.3 Security

The Workbench binds loopback by default. Non-loopback binding requires an explicit unsafe override. Host and Origin are allowlisted; CORS is closed by default. Credentials are accepted in JSON, retained in memory only, never echoed, and redacted from logs, traces, errors, configs, reports, and artifacts.

### 9.4 Required observability

Each run records data/index/query-policy/prompt/model/code identity; stage and total latency; retrieval counts, fallback/degradation, and reranker contribution; selected/rejected/conflicting/missing evidence; token usage, cost, retry reason; and cancellation, timeout, refusal, and assurance outcome.

Normal run artifacts use configurable retention, initially 30 days. Versioned golden/eval artifacts are retained permanently.

### 9.5 UX latency targets

These are engineering targets, not public SLA claims:

| Milestone | Target |
|---|---:|
| POST to first event | p95 < 500 ms |
| first evidence visible | p50 < 5 s, p95 < 10 s |
| first answer token without follow-up | p50 < 12 s, p95 < 20 s |
| complete without follow-up | p50 < 20 s, p95 < 35 s |
| complete with follow-up | p95 < 45 s |

## 10. V1 release gates

Hard safety gates:

    ticker violations = 0
    post-cutoff material evidence = 0
    citation resolution = 100%
    runtime identity binding = 100%
    metadata-only material causal evidence = 0
    frontend-fabricated evidence fields = 0
    secret leakage = 0

Initial data gates:

    dedup identity coverage >= 95% of searchable serving evidence
    publisher and source_class presence >= 95% of searchable news
    benchmark-relevant SEC parse success >= 90%

Initial 12-case retrieval gates:

    Recall@8 >= 0.75
    primary-source hit >= 0.80 where primary evidence exists
    duplicate-adjusted Precision@8 >= 0.60
    ticker/cutoff violations = 0

Initial attribution gates:

    citation correctness = 100%
    unsupported primary causal claims = 0
    unsupported material claims <= 5%
    false SUFFICIENT <= 1 of 12
    explicit ABSTAIN cases producing SUFFICIENT = 0
    NO_MATERIAL_PUBLIC_CATALYST producing SUFFICIENT = 0
    NO_MATERIAL_PUBLIC_CATALYST without completed sanity tasks = 0

Adaptive follow-up is retained only if it finds new useful independent evidence in at least half of recoverable-gap cases, zero-new-evidence rate is at most 25%, false attribution does not increase, and median total latency grows by no more than 30% over no-follow-up.

These are Frozen V1 release gates. They may be recalibrated only by an explicit amendment supported by benchmark evidence; they are not silently weakened to make a run pass.

## 11. Implementation order

### P0 — establish truth

1. Audit current repo against this design as KEEP, WIRE, MODIFY, ADD, or REMOVE.
2. Materialize publisher/source/content/dedup metadata through serving and establish the canonical evidence API.
3. Create and human-review the 12-case benchmark and evidence judgments.
4. Re-run the committed baseline and publish retrieval, attribution, latency, and cost results.

### P1 — balanced vNext

1. Wire point-in-time observation context into runtime.
2. Enforce bounded retrieval/fallback and complete evidence metadata.
3. Evolve Critic into Evidence Analyst.
4. Add one typed follow-up and its stop conditions.
5. Add ClaimPlan, pre-write validation, and post-stream assurance.
6. Add managed background execution, persisted SSE replay, cancellation, and true provider streaming.
7. Project canonical evidence and citations into the Workbench without frontend-derived quality claims.
8. Re-run V1 eval on a pinned GPU/runtime and compare it to baseline.

### P2 — evidence-driven only

LLM planning, external web search, market-structure data, and a larger benchmark enter planning only after V1 metrics identify a concrete failure mode they can address.

## 12. Definition of Done

Catalyst vNext V1 is complete only when:

- all hard safety and release gates pass on pinned evidence;
- a user immediately receives a run ID and replayable SSE;
- candidates and rerank decisions appear before the answer;
- answer deltas come from a real provider stream;
- every material claim resolves to selected same-run evidence;
- failure, cancellation, timeout, abstention, and invalidation are visible and testable;
- calendar-aware tests prove prior-close/after-close/pre-market evidence enters the current session information window while post-cutoff evidence does not;
- follow-up tests prove round two receives the deduplicated union of round-one and new evidence with round/task provenance intact;
- initial-task tests prove bounded parallel execution, deterministic merge order, shared deadline enforcement, and visible per-task degradation;
- at least one benchmark case demonstrates a useful, different, bounded follow-up without answer-shopping;
- each run explains what data/model/code it used, what it retrieved, what it selected or rejected, why it stopped, how long it took, and what it cost;
- canonical suites, replay tests, SSE reconnect tests, secret scans, and pinned GPU/eval gates pass;
- the final report includes negative results and limitations.

## 13. Resolved candidate-design audit items

The candidate design's former open questions are resolved for V1:

1. Four-arm policies are fts5, dense, hybrid, and reranked.
2. Market/sector proxies use only audited point-in-time local coverage; absence becomes a coverage gap.
3. Source classes reuse the seven repository values in §5.1; agents owns their evidence-role mapping.
4. The production writer must pass a real-streaming capability probe; provider choice is configuration.
5. Cancellation is cooperative; non-cancellable results are discarded after cancellation.
6. Benchmark composition is frozen by category and human approval; exact case IDs are a versioned eval artifact, not architecture.

These require implementation audit and tests, but none remains an open architecture decision.

## 14. Overengineering guardrail

Implementation MUST NOT add multi-agent orchestration, a supervisor, free ReAct, GraphRAG, memory/self-improvement, an LLM planner, web search, microservices, a distributed queue, WebSocket, generic SQL agents, numeric publisher authority scores, or automated answer-shopping under this V1 design.

The allowed architecture is deliberately small:

    trustworthy data
    → explainable hybrid retrieval
    → bounded evidence assessment
    → at most one evidence-driven follow-up
    → validated claim plan
    → true streamed rendering
    → assurance and human-grounded evaluation

That complete, measurable chain—not architectural breadth—is the V1 technical claim.
