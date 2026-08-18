# Catalyst vNext Technical Design — Frozen V1.1

**Status:** FROZEN V1.1 — implementation baseline

**Frozen on:** 2026-08-16

**Revision:** V1.1 — final normative implementation baseline

**Code baseline:** 99e584d57f5192418de697b6c2c49eb0ceb273d5

**Scope:** packages/data-core, packages/agents, packages/eval, and the packages/app API/SSE boundary

**Revision note:** V1.1 refines the V1.0.3 contracts after implementation
review, external agent/harness research, and evaluation planning. Final
normative cleanup clarifies model/code ownership, causal-hypothesis ontology,
token-pressure packing, round-specific conflict semantics, and staged adaptive
promotion. The bounded architecture and its correctness spine are unchanged:
no new LLM node, no second corrective round, and no weakening of temporal,
materiality, independence, identity, refusal, or citation guarantees.

## 1. Purpose

Catalyst vNext is a bounded financial event-attribution system. It accepts a security identity, a market session, and a user question; retrieves only point-in-time eligible evidence; assesses whether that evidence supports a causal explanation; and returns a cited SUFFICIENT, PARTIAL, or ABSTAIN result.

The project demonstrates production-shaped AI systems work:

- trustworthy financial data and provenance;
- cutoff-safe hybrid retrieval and reranking;
- evidence-bounded agent decisions;
- real streaming user experience;
- reproducible evaluation and runtime identity;
- observable latency, cost, failure, and refusal behavior.

The goal is not to maximize the number of agents, tools, or infrastructure components. The goal is to make one attribution workflow reliable and measurably useful. No feature without a measurement plan enters the critical path.

## 2. Normative language and change control

MUST, MUST NOT, SHOULD, and MAY are normative.

This document freezes architecture and externally visible contracts. Internal filenames and helper structure may change without an amendment if all frozen contracts remain true.

A change requires a design amendment before implementation if it changes:

- package ownership or dependency direction;
- temporal, ticker, eligibility, or evidence-materiality semantics;
- the attribution state machine or corrective-round/action bounds;
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

`AttributionStatus = SUFFICIENT | PARTIAL | ABSTAIN` and
`AttributionType = EVIDENCE_BACKED_CAUSAL | NO_MATERIAL_PUBLIC_CATALYST` are
orthogonal contracts. Attribution type is never a status, and research-control
state is neither one.

**SUFFICIENT** means eligible evidence supports a primary explanation, all material claims are cited, and no unresolved material contradiction remains. It requires direct primary evidence or corroborating independent reporting. Commentary-only evidence cannot produce SUFFICIENT.

**PARTIAL** means credible evidence exists but a material gap remains, such as weak magnitude coverage, missing primary confirmation, or an unresolved secondary mechanism. PARTIAL is a valid result, not a system failure.

**ABSTAIN** means Catalyst cannot responsibly make a causal attribution from eligible evidence. Missing local coverage, unsupported market-structure explanations, and unresolved evidence contradictions are valid abstention reasons. A runtime identity, schema, database, index, or other integrity failure is a system failure, not an abstention.

Request and system failures are separate terminal states and MUST NOT be reported as ABSTAIN.

Stable system-failure reasons include at least `MODEL_TIMEOUT`,
`MODEL_SCHEMA_FAILURE` after the permitted retry, `IDENTITY_FAILURE`,
`DATA_RUNTIME_MISMATCH`, and `ASSURANCE_FAILED`. They are excluded from
epistemic abstention/refusal metrics.

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

V1.1 is not:

- a trading, forecasting, or recommendation system;
- an intraday microstructure or institutional event-study engine;
- a general-purpose ReAct or deep-research agent;
- a multi-agent or supervisor-agent system;
- a web-scale crawler or Bloomberg replacement;
- a multi-tenant SaaS platform;
- a microservice, Kafka, Celery, Temporal, Redis, or Kubernetes project.

External web search, market-structure datasets, and an LLM ResearchPlanner are
deferred until promotion gates show a specific need. Larger validation and
holdout benchmarks are staged evaluation requirements before strong external
claims, not new runtime capabilities.

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
- no new catalyst-contracts package is introduced in V1.1.

This differs from runtime data flow:

    data evidence → agents workflow → app transport/UI
           └──────────────→ eval offline measurement

### 4.2 Ownership

| Package | Owns | Does not own |
|---|---|---|
| data-core | canonical assets, provenance, source taxonomy, temporal identity, eligibility, query policy, retrieval, scores/ranks, point-in-time structured facts, data runtime identity | Observation/MoveProfile interpretation, causal conclusions, prompts, final status |
| agents | Observation/MoveProfile, ContextPack policy, research tasks, evidence assessment/hypotheses, bounded corrective research, claim plan, attribution status/result, run manifest, trace and runtime assurance | canonical storage, index truth, benchmark truth, HTTP transport |
| eval | golden cases, evidence judgments, metrics, eval manifests, four-arm ablation, regression reports and gates | production routing, refusal policy, runtime side effects |
| app | HTTP request admission, background execution, SSE, cancellation, local credential wiring and Workbench projection | retrieval algorithms, causal decisions, benchmark metrics |

## 5. Data-core design

### 5.1 Canonical evidence model

V1.1 uses common asset identity plus typed subtype metadata. It does not force all provider records into one giant table.

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

V1.1 reuses the repository source classes:

- structured_market_data
- official_government
- issuer_disclosure
- corporate_press_release
- reported_news
- analysis_opinion
- aggregated_unknown

No numeric publisher-authority score is introduced. Agents use this frozen V1.1
mapping:

| Source class | Evidence role | Permitted use |
|---|---|---|
| `structured_market_data` | `STRUCTURED_CONTEXT` | observation, magnitude, market/sector/peer context; not documentary causal proof |
| `official_government` | `PRIMARY_AUTHORITY` | direct primary support only for a government, regulatory, or macro claim within the source's scope |
| `issuer_disclosure` | `DIRECT_PRIMARY` | direct support for issuer facts within the disclosure's scope |
| `corporate_press_release` | `DIRECT_PRIMARY` | direct support for issuer announcements; independence from the issuer is not implied |
| `reported_news` | `INDEPENDENT_REPORT` | support when the report contains relevant attributable facts and passes independence checks |
| `analysis_opinion` | `COMMENTARY_LEAD` | query hint, corrective-research trigger, or weak context; never sufficient alone for a primary causal claim |
| `aggregated_unknown` | `UNKNOWN` | lead-only unless another deterministic rule establishes a stronger typed source |

Evidence role is a ceiling, not an automatic relevance judgment. A direct
primary source outside the claim's scope does not support that claim.

### 5.2 Content state and materiality

Canonical content state is:

    FULL_TEXT | TITLE_ONLY | METADATA_ONLY | EMPTY | FAILED

The current serving corpus may include metadata_only rows. V1.1 preserves that for existence checks and retrieval leads; it does not silently redefine the existing SEARCHABLE_STATUSES contract.

Material-evidence rules are stricter:

- FULL_TEXT MAY support a final claim after all gates pass.
- TITLE_ONLY MAY be a lead and MAY support only a narrowly scoped existence claim when provenance is authoritative. It MUST NOT independently support a complex causal mechanism.
- METADATA_ONLY MAY support calendar/existence checks and corrective-research planning. It MUST NOT be passed to the model as document body or support a material causal claim.
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

    RUN_ADMISSION
    → QUERY_VALIDATION
    → OBSERVATION_BUILD
    → INITIAL_RESEARCH_POLICY
    → INITIAL_RESEARCH_EXECUTION
    → EVIDENCE_STATE
    → COVERAGE_SUMMARY
    → CONTEXT_PACK_BUILD
    → EVIDENCE_ANALYST
        ├─ READY
        ├─ ABSTAIN
        └─ FOLLOW_UP
             → one CorrectiveResearchBatch
             → cumulative EVIDENCE_STATE
             → recomputed COVERAGE_SUMMARY
             → CONTEXT_PACK_BUILD round 2
             → EVIDENCE_ANALYST round 2
    → CLAIM_PLAN_BUILD
    → CLAIM_VALIDATION
    → STREAMING_ANSWER_WRITER
    → POST_STREAM_ASSURANCE
    → FINALIZER

There is no open-ended loop. `max_corrective_rounds = 1` is a frozen bound.
The production batch defaults to one corrective action; a two-action batch is
an eval-only experiment defined in §6.4. There is no independent LLM Planner,
Hypothesis Agent, Claim Agent, Supervisor, or extra final Judge.

The logical model-call budget remains:

| Runtime path | Logical model calls |
|---|---:|
| normal | one Evidence Analyst + one streaming Writer = 2 |
| one corrective round | two Evidence Analyst calls + one streaming Writer = 3 |

Parallel actions within one corrective batch do not add an Analyst call.
Additional provider attempts are allowed only by the bounded technical retry
contract in §6.6.

### 6.2 Observation and initial research

Observation is agents-owned. Data-core supplies only point-in-time facts;
agents interprets those facts as a null-safe `MoveProfile`:

    MoveProfile
    - target_return?
    - prior_session_return?
    - gap_return?
    - market_return?
    - sector_return?
    - peer_summary?
    - market_adjusted_return?
    - sector_adjusted_return?
    - volume_abnormality?
    - scheduled_macro_flags[]
    - market_comove?
    - sector_comove?
    - peer_comove?
    - coverage_flags[]
    - degraded_fields[]

No field is inferred when the underlying point-in-time fact is unavailable.
Unknown and degraded fields stay explicit and participate in coverage/status
ceilings. Data-core owns prices, timestamps, calendars, and retrieved facts;
agents owns relative-move, co-movement, and scenario interpretation.

Before P1A implementation, `peer_summary`, `volume_abnormality`,
`market_comove`, `sector_comove`, and `peer_comove` MUST receive one canonical,
versioned typed shape each. The architecture does not freeze their formulas,
but one field cannot mean a boolean in one module, correlation in another, and
residual move in a third. Typed structs MAY retain both an underlying metric
and a bounded categorical band such as `NORMAL | ELEVATED | EXTREME | UNKNOWN`.

The deterministic InitialResearchPolicy may use only MoveProfile and other
already-built Observation fields. In particular, `CONTINUATION` MUST NOT depend
on news or evidence that has not yet been researched.

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
| `FUNDAMENTALS` | deterministic structured SQLite lookup; normally corrective-research only in V1.1 |
| `MARKET_STRUCTURE` | no execution backend in V1.1; records a non-recoverable coverage gap and MUST NOT create a retrieval action |

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

#### 6.3.1 EvidenceAnalystContextPack

Canonical artifacts are not passed directly to the model. A deterministic
`ContextPackBuilder` derives a versioned, model-facing view:

    canonical Observation + EvidenceState + CoverageSummary + research history
    → ContextPackBuilder
    → EvidenceAnalystContextPack
    → Evidence Analyst

    EvidenceAnalystContextPack
    - schema_version
    - packing_policy_version
    - round
    - token_budget
    - hard_constraints
    - observation
    - coverage_summary
    - research_history
    - direct_primary_evidence[]
    - primary_authority_evidence[]
    - independent_reports[]
    - lead_only_evidence[]
    - deterministic_conflict_signals[]
    - coverage_gaps[]
    - included_evidence_ids[]
    - excluded_evidence_ids[]
    - truncation_metadata
    - delta_evidence_ids[]
    - prior_counter_evidence_ids[]?       # round two only
    - prior_semantic_conflicts[]?         # round two only
    - previously_supported_hypothesis_ids[]? # round two only
    - unresolved_gap_ids[]?               # round two only

The pack is a derived view, never canonical truth. Changing packing policy does
not mutate Observation, EvidenceState, source roles, independence groups,
CoverageSummary, or retrieval artifacts.

`ContextPackBuilder` owns deterministic ordering, token allocation, excerpt
selection, inclusion/exclusion, truncation, duplicate compression, delta
highlighting, and provenance preservation. Its ordering policy is:

1. identity and temporal eligibility hard gates;
2. materiality hard gates;
3. semantic relevance and retrieval/rerank utility;
4. evidence-role authority ceiling;
5. independence-group novelty;
6. temporal relevance;
7. token efficiency.

Authority is a ceiling, not semantic relevance; source role is never the first
ranking feature before eligibility, materiality, and relevance.

For round one, relevance means deterministic query/task fit plus retrieval and
reranker utility already available before the Analyst call; the builder MUST
NOT call another model to rank context. Round-one
`deterministic_conflict_signals` may contain only code-computable temporal
inconsistencies, structured-data mismatches, dedup/syndication relationships,
provider/source metadata disagreement, and known data-quality conflicts. The
builder MUST NOT precompute semantic hypothesis-level counter-evidence or
claim-level contradictions. The four `prior_*` semantic fields are absent or
empty in round one.

Under token pressure, the builder MUST preserve the identity and safety
metadata of every unique material primary item and every material
counter-evidence item already identified by a prior Analyst assessment. At
minimum, the inventory retains `evidence_id`, source class/evidence role,
`eligible_at`, materiality/content state, independence group, document/section
identity where relevant, and the exclusion/truncation reason when text payload
is omitted. Every excluded evidence ID remains visible in the pack inventory.

Text bodies and excerpts remain strictly bounded by the model token budget and
deterministic packing policy. After the hard identity/safety inventory is
reserved, payload allocation prioritizes highest-utility material evidence,
useful independent support, material counter-evidence where a prior assessment
exists, relevant remaining evidence, and finally lead-only evidence. This
contract does not require the full body or an excerpt of every inventoried
item.

For long filings, the builder always retains document identity, section
identity where known, eligibility, evidence role, and materiality metadata,
while only bounded relevant sections/excerpts enter model context; it does not
dump entire filings. For syndicated reporting, it includes one useful
representative body and carries cluster/member/support-count metadata
separately. `TITLE_ONLY` and `METADATA_ONLY` retain §5.2 ceilings and are never
upgraded because of source authority.

Round two is rebuilt from canonical cumulative state after deduplication,
independence, deterministic conflicts, CoverageSummary, and coverage gaps are
recomputed. It may use the prior `AnalystDecision` and normalized
`EvidenceAssessment` to organize `prior_counter_evidence_ids`,
`prior_semantic_conflicts`, previously supported hypotheses, and unresolved
gaps alongside the new-evidence delta. Those semantic records are prior model
judgments, not deterministic facts, and they never replace canonical evidence.
The pack MUST NOT use a model-generated round-one summary as the sole
representation of prior evidence; that shortcut would create lossy state
propagation and self-confirmation risk.

#### 6.3.2 Bounded hypothesis competition

The same Evidence Analyst call evaluates evidence and emits at most three
serious competing hypotheses. There is no Hypothesis Agent or additional model
call.

    MAX_SERIOUS_HYPOTHESES = 3

    CandidateHypothesis
    - hypothesis_id
    - cause_type
    - statement
    - mechanism?
    - supporting_evidence_ids[]
    - contradicting_evidence_ids[]
    - magnitude_fit: STRONG | PLAUSIBLE | WEAK | UNKNOWN
    - proposed_role: PRIMARY | SECONDARY | CONTEXT | REJECTED
    - unresolved_gap_ids[]

`CauseType` contains only causal explanation categories:

    COMPANY_SPECIFIC_CATALYST
    CONTINUATION
    SECTOR_MOVE
    MACRO_EVENT
    FUNDAMENTAL_REPRICING
    REPORTING_OR_ANALYST_CONTINUATION

`NO_MATERIAL_PUBLIC_CATALYST` is an `AttributionType`, not a causal
hypothesis. The Analyst may recommend `PARTIAL` with that attribution type
after the existing sanity-task and coverage gates pass; it does not create a
CandidateHypothesis whose cause is the absence of a catalyst.

`MARKET_STRUCTURE_UNSUPPORTED` is a gap/capability reason, not a causal
hypothesis. It is represented as non-recoverable `MissingEvidence` with
`evidence_need = MARKET_STRUCTURE` and may appear in `coverage_gaps` or
limitations. Catalyst does not invent a squeeze, positioning, flow, or similar
mechanism merely because documentary evidence is insufficient.

The Analyst does not emit every type on every run. MoveProfile supplies bounded
scenario context: a large company residual makes a company-specific hypothesis
more plausible; broad sector/peer co-movement makes a sector explanation more
plausible; scheduled macro plus broad movement makes a macro explanation more
plausible. Eligible evidence then determines support and contradiction.

No numeric probability, fake calibrated confidence, or hidden chain-of-thought
is permitted; artifacts contain structured conclusions and bounded reason
codes/rationales only. Stale or ineligible evidence cannot support a
hypothesis; commentary cannot independently establish a primary causal
explanation; unsupported market structure becomes a coverage gap; co-movement
may weaken a pure company-specific explanation; and primary evidence confirms
event facts but does not automatically prove magnitude causality.

The only structured object emitted directly by the Evidence Analyst model is:

    AnalystDecision
    - evidence_decisions[]
    - candidate_hypotheses[]
    - conflicts[]
    - proposed_missing_evidence[]:
        proposal_ref
        evidence_need
        time_scope
        expected_information
        reason_code
    - research_decision: READY | FOLLOW_UP | ABSTAIN
    - recommended_status: SUFFICIENT | PARTIAL | ABSTAIN
    - proposed_attribution_type:
        EVIDENCE_BACKED_CAUSAL | NO_MATERIAL_PUBLIC_CATALYST
    - proposed_corrective_intents[]?:
        proposal_ref
        query_hints[]

`proposal_ref` links bounded corrective intent to one proposed missing-evidence
record. It is not a runtime gap ID or permission to execute. The model does not
declare recoverability or capability availability.

Code validates and normalizes the raw model object before any workflow action:

    AnalystDecision
    → deterministic validation / normalization
    → EvidenceAssessment

    EvidenceAssessment
    - analyst_decision
    - validated_missing_evidence[]
    - status_ceiling: SUFFICIENT | PARTIAL | ABSTAIN
    - corrective_batch?
    - normalization_violations[]

Each evidence decision is SUPPORT, CONTRADICT, WEAK, LEAD_ONLY, or IRRELEVANT.

`research_decision` controls workflow only. `recommended_status` and
`proposed_attribution_type` describe the epistemic result only. They are never
used as technical failure states. `CauseType`, `AttributionType`,
`AttributionStatus`, `ResearchDecision`, and gap reason codes are distinct
ontologies and MUST NOT be substituted for one another.

The Evidence Analyst model MUST NOT emit `status_ceiling`. Code computes the
ceiling from eligible material support, source-role ceilings, deterministic
independence groups, semantic contradictions, and coverage. Runtime identity,
schema, or integrity invalidity fails the run before result normalization; it
does not become a lower attribution ceiling. The model may recommend below the
computed ceiling, but cannot set, raise, or override it or convert a system
failure into `ABSTAIN`.

The model MUST NOT emit a finished executable `CorrectiveResearchBatch`. It may
only propose gap-linked evidence need, time scope, expected information, and
bounded query hints. Code validates the referenced proposal, determines
recoverability and backend capability, assigns gap/action IDs and research
fingerprints, sanitizes query hints, enforces ticker/cutoff/source constraints,
action count/result limits/budgets, assigns the shared deadline, and constructs
the optional `corrective_batch` persisted in `EvidenceAssessment`.
Forbidden raw fields are structured-schema violations subject to the single
technical retry in §6.6; they are never silently trusted. Semantically invalid
gap/action proposals are excluded by normalization and recorded in
`normalization_violations`; a `FOLLOW_UP` recommendation without a valid
normalized batch cannot execute research.

Code and model responsibilities are frozen:

| Code decides | Evidence Analyst decides |
|---|---|
| ticker/cutoff/runtime identity validity | what claim a passage supports or contradicts |
| content materiality and source-role ceiling | whether the evidence is semantically relevant |
| dedup/syndication independence groups | the plausible causal mechanism |
| deterministic market/sector/peer comparisons | semantic conflicts and missing evidence |
| maximum permitted attribution status | magnitude fit within the supplied structured context |
| gap recoverability, backend capability, IDs, limits, deadline, and budget | bounded gap and corrective-intent proposals |

Content-level independence is the V1.1 contract. Evidence is not independent if
it shares a dedup cluster, canonical document/article identity, canonical
content hash, or known syndication lineage. Different URLs or publishers alone
do not establish independence. If lineage is ambiguous, the items do not count
as separate support for a `SUFFICIENT` gate.

Code exclusively owns independence-group assignment. The Analyst may judge
whether passages semantically corroborate or contradict a hypothesis; it MUST
NOT create a new independence group, split a deterministic group, or count
ambiguous lineage as independent.

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

The Evidence Analyst may recommend a status below the ceiling or request
corrective research. It MUST NOT raise the final status above the deterministic
ceiling. A `READY` model decision is therefore necessary but never sufficient
for `SUFFICIENT`.

### 6.4 Corrective research

Initial `ResearchTask`, discovered `MissingEvidence`, and adaptive
`CorrectiveResearchAction` are distinct first-class versioned artifacts:

    MissingEvidence
    - gap_id
    - evidence_need: EvidenceNeed
    - time_scope: TimeScope
    - expected_information
    - reason_code
    - recoverable: bool

    CorrectiveResearchAction
    - action_id
    - gap_id
    - evidence_need: EvidenceNeed
    - time_scope: TimeScope
    - lookback_sessions?
    - query_hints[]
    - research_fingerprint

    CorrectiveResearchBatch
    - batch_id
    - actions[]
    - shared_deadline
    - total_result_budget

V1.1 reason codes are bounded to:

    MISSING_PRIMARY_CONFIRMATION
    MISSING_INDEPENDENT_CORROBORATION
    MISSING_PRIOR_SESSION_CONTEXT
    MISSING_SECTOR_CONTEXT
    MISSING_MACRO_CONTEXT
    MISSING_FUNDAMENTAL_CONTEXT
    CONFLICT_REQUIRES_RESOLUTION
    MARKET_STRUCTURE_UNSUPPORTED
    LOCAL_COVERAGE_GAP

When the first `AnalystDecision` identifies proposed gaps and bounded corrective
intent, code may normalize at most one corrective batch into
`EvidenceAssessment`. Every `CorrectiveResearchAction` MUST reference a
distinct validated `MissingEvidence` record from that assessment with
`recoverable = true`; its need and time scope must match that gap. One gap ID
maps to at most one action. Multiple query variants for one gap in one batch
are forbidden.

`MARKET_STRUCTURE_UNSUPPORTED` is normalized as:

    MissingEvidence
    - evidence_need: MARKET_STRUCTURE
    - reason_code: MARKET_STRUCTURE_UNSUPPORTED
    - recoverable: false

It and any capability without a local backend MUST NOT create a retrieval
action. Code, not the model, assigns gap/action IDs and fingerprints and
controls recoverability, ticker, TemporalIdentity, source filters, result
count, time window, timeout, backend, deadline, and budget. Gap, action, and
batch artifacts are exposed to trace, eval, and safe UI projection without
hidden reasoning.

Production freezes:

    max_corrective_rounds = 1
    max_actions_per_batch = 1

An eval-only arm MAY set `max_actions_per_batch = 2`, but only when the same
assessment contains two distinct human-valid recoverable gap IDs. Actions run
in bounded parallel under the batch deadline and total result budget. The
two-action arm is not a production default and does not create another Analyst
call or corrective round.

Corrective research stops when one batch has run, a research fingerprint
repeats, no new independent evidence appears, budget expires, or the capability
is unavailable. Unsupported market-structure needs become coverage gaps.

The two-action arm may be promoted only after A4 runs on Stage 2 or later, its
predeclared human-labelled multi-gap subset meets the EvalManifest minimum
eligible denominator, and the pinned ablation demonstrates net improvement in
useful independent-evidence recovery, attribution correctness, status
correctness, and false-attribution rate after accounting for zero-new-evidence
rate, latency, retrieval cost, and token cost. A neutral or negative result
keeps production at one action.

The second Evidence Analyst invocation always receives cumulative evidence:

    EvidenceState(t+1) = dedup(EvidenceState(t) ∪ NewEvidence)

Each evidence item records `first_seen_round`, all contributing
`research_task_ids`, and its current accepted/lead/rejected state. Round-two
analysis MUST NOT discard valid round-one evidence or treat corrective research
as an isolated RAG run. Deduplication and independence grouping are recomputed
over the cumulative state before deterministic conflicts, CoverageSummary,
coverage gaps, and packing priority are recomputed and the round-two ContextPack
is rebuilt. Prior semantic conflicts and counter-evidence come from the prior
`AnalystDecision`/`EvidenceAssessment` and serve only as organization aids over
that canonical cumulative state.

### 6.5 Claim plan and streamed answer

The Evidence Analyst produces CandidateHypothesis records rather than public
prose. Code normalizes accepted hypotheses and evidence decisions into a
minimal ClaimPlan:

    ClaimPlan
    - status
    - attribution_type
    - claims[]:
        claim_id
        role: PRIMARY | SECONDARY | CONTEXT | LIMITATION
        statement
        mechanism?
        support_evidence_ids[]
        counter_evidence_ids[]
        limitations[]

The plan is intentionally not a reasoning graph. It exists only to constrain
the Writer, bind claims to evidence, support Workbench navigation, enable eval,
and make failures debuggable. It contains no numeric confidence.

The pipeline is:

    EvidenceAssessment
    → deterministic ClaimPlan normalization
    → ClaimPlan
    → ClaimValidator
    → ValidatedClaimPlan
    → Writer

`ValidatedClaimPlan` is the maximum set of public claims and roles the Writer
may express. Validation verifies evidence existence, same-run identity,
eligibility, materiality, independence, source-role ceiling, contradiction,
status gates, and citation ownership.

ClaimValidator has two failure classes:

**Attribution-support failure** includes insufficient material support,
insufficient independence, unresolved contradiction, a source-role ceiling too
low for the proposed role, or inadequate citation support. Code drops or
downgrades the claim, lowers its role/status, or produces `ABSTAIN`. This is not
automatically a system failure.

**Integrity/system failure** includes a missing evidence ID, cross-run evidence,
runtime identity mismatch, impossible canonical state, corrupted artifact, or
unrecoverable schema invariant. The run fails; it MUST NOT become `ABSTAIN`.

ClaimPlan construction does not make an additional model call. The normal
logical model-call budget is:

| Path | Logical model calls |
|---|---:|
| no corrective round | one Evidence Analyst + one streaming Writer = 2 |
| one corrective round | two Evidence Analyst calls + one streaming Writer = 3 |

The only additional provider attempts permitted are the bounded technical
retries in §6.6. No Hypothesis, Planner, ClaimPlan, or final-Judge model call may be inserted
without a design amendment.

    WriterInput
    - final_status
    - attribution_type
    - observed_move
    - validated_claim_plan
    - narrowly_bound_supporting_snippets
    - citation_map
    - required_limitations

The Writer is an expression layer over ValidatedClaimPlan. It MAY paraphrase,
organize, connect validated statements, and make the report readable. It MUST
NOT retrieve, research, invent a fact or mechanism, promote a rejected
hypothesis, change claim roles/status, increase certainty, or use unbound
evidence. True provider token streaming remains mandatory.

Post-stream assurance remains structural. It verifies citation resolution and
same-run ownership, required sections/limitations, status alignment with
ValidatedClaimPlan, stream completion, and plan/citation identity. It is not a
fake semantic theorem prover; unsupported semantic drift remains primarily an
offline eval problem. Assurance MUST NOT silently replace or rewrite a streamed
answer.

Text emitted before assurance is labelled PROVISIONAL_RENDERING. If assurance fails, the answer is invalidated and the run fails closed. The system MUST NOT retry merely to obtain a preferred status or narrative.

### 6.6 Retry policy

A model call MAY retry once only for transport error, timeout, or invalid structured schema, using the same evidence and instructions. Semantic dissatisfaction, PARTIAL, or ABSTAIN is not a retry reason.

### 6.7 Run identity, trace, and assurance

Agents owns RunManifest, composing DataRuntimeIdentity with code revision,
workflow/policy version, analyst and writer prompt hashes,
provider/model/revision, token and timeout budgets, and runtime configuration.
V1.1 also binds:

    context_pack_schema_version
    packing_policy_version
    context_token_budget
    hypothesis_schema_version
    max_corrective_rounds
    max_actions_per_batch

Every run persists versioned artifacts for Observation/MoveProfile,
retrieval/rerank, EvidenceState, CoverageSummary, each ContextPack,
raw `AnalystDecision`/CandidateHypothesis, normalized EvidenceAssessment,
optional corrective batch, ClaimPlan, ValidatedClaimPlan, answer, and assurance.
Trace events contain lifecycle metadata and artifact references; large payloads
live in artifacts. Secrets and hidden chain-of-thought are never persisted.

Production runtime owns RunAssuranceRecord. Eval independently revalidates persisted evidence; it does not own production assurance.

## 7. Eval design

### 7.1 Evaluation layers

V1.1 separates:

1. Offline retrieval eval, which measures finding/ranking evidence without an answer.
2. Four-arm ablation, using the repository-defined fts5, dense, hybrid, and reranked arms from one pinned production runner.
3. End-to-end attribution eval, which measures selected evidence, causal claims, citations, status/refusal, latency, and cost.

Four-arm is controlled ablation and union-pool generation, not four production runtimes.

### 7.2 Human truth and judges

The Stage-1 development benchmark contains 12 manually reviewed cases. Existing T4 cases may be reused only after explicit human re-annotation under the V1.1 schema. Cases cover company-specific, sector/macro, continuation, distractor, quiet, and abstention behavior.

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
        acceptable_corrective_actions[]
        corrective_recoverable: bool
        corrective_required: bool
    - notes

Human labels are primary truth for relevance, cause, and answerability. An LLM judge MAY score language quality and provide regression hints; it MUST NOT define retrieval, cause, or answerability ground truth.

Research-behavior labels describe acceptable task sets rather than one brittle
exact query string. Human annotation decides whether a gap is recoverable and
whether corrective research is required; the implementation cannot infer those
labels from its own output.

### 7.3 Metrics

Retrieval reports Recall@K, MRR, nDCG@K, primary-source hit rate, duplicate-adjusted precision, independent evidence recall, source diversity, reranker contribution, violations, latency, and degradation.

Attribution reports evidence precision, citation correctness, causal relevance, material coverage, unsupported claims, the status confusion matrix, refusal correctness, conflict handling, latency, tokens, and cost.

Agent behavior reports:

- corrective trigger precision and recall;
- gap reason-code identification accuracy;
- corrective action match against the acceptable need/time-scope set;
- stop correctness after ready, non-recoverable, repeated, or exhausted states;
- useful-corrective rate and unnecessary-corrective rate;
- attribution delta between no-corrective and bounded-corrective policies.

Each metric reports its eligible denominator. In particular, corrective recall is
measured only on human-labelled recoverable/required cases, while unnecessary
corrective rate is measured on cases labelled not required.

For A3, the eligible subset is human-labelled recoverable corrective cases. For
A4, it is human-labelled multi-gap recoverable cases. Before either comparison
runs, `EvalManifest` MUST bind the eligible-subset definition and a
`minimum_eligible_denominator`. That minimum is a versioned eval decision, not
an architecture constant, and MUST be declared before results are observed.
Runs below it report diagnostics but cannot satisfy an architecture-retention
or promotion gate.

Reports MUST show integer case counts with percentages. Twelve cases do not justify spurious decimal precision.

### 7.4 Pinned evidence and invalidation

Official eval binds inputs and outputs to RunManifest and EvalManifest. Actual runtime values are extracted from production artifacts. Missing, wrong, or conflicting identity fails closed and produces no success token.

Data/corpus/index/query-policy changes invalidate retrieval evidence. Model, prompt, workflow, or budget changes invalidate attribution output. Old evidence remains historical but is labelled STALE_FOR_CURRENT_RUNTIME.

### 7.5 Staged benchmark program

**Stage 1 — implementation/development probe** uses 12 manually reviewed
cases for contract correctness, fast regression detection, and architecture
iteration. It may show whether corrective research executes safely, follows its
contracts, avoids obvious regressions, and can recover useful evidence at all.
Stage-1 adaptive metrics are development signals only: they cannot establish a
general adaptive-research efficacy claim or promote the two-action arm.

**Stage 2 — architecture validation** targets approximately 40–60 stratified,
human-reviewed cases. Strata include company earnings/guidance, issuer events,
continuation, reporting/analyst continuation, macro, sector-wide movement,
company-versus-sector ambiguity, stale/opinion distractors, conflicting
evidence, quiet/no-catalyst, unsupported market structure, and local coverage
gaps. A3/A4 retention or promotion conclusions require their predeclared
eligible subsets to meet the `minimum_eligible_denominator` bound in the
EvalManifest.

**Stage 3 — holdout** targets approximately 15–20 untouched cases. External
quantitative claims about attribution or adaptive-research improvement require
this holdout. Exact case IDs and splits are versioned eval artifacts, not
architecture constants. Human labels remain primary truth; LLM judges remain
secondary diagnostics.

### 7.6 Minimal ablation program

V1.1 freezes five high-value experiments rather than a combinatorial matrix:

| ID | Comparison | Primary measurements |
|---|---|---|
| A1 Context Engineering | legacy/raw packing vs ContextPackBuilder | causal correctness, unsupported material claims, false attribution, tokens, latency |
| A2 Hypothesis Competition | legacy candidate-claim behavior vs at most three CandidateHypothesis records | primary-cause correctness, company-vs-sector errors, conflict handling, premature READY/false SUFFICIENT |
| A3 Adaptive Follow-up | no corrective round vs one action, on the predeclared human-labelled recoverable subset | recoverable-gap evidence recovery, attribution delta, status correctness, false attribution, zero-new-evidence rate, latency/cost |
| A4 Corrective Batch | one action vs at most two parallel actions, on the predeclared human-labelled multi-gap recoverable subset | useful independent evidence, attribution/status correctness, false attribution, zero-new-evidence rate, latency and cost |
| A5 Observation | limited relative context vs MoveProfile-aware policy/context | company-vs-sector attribution, macro attribution, routing correctness |

Temporal cutoff, materiality, deterministic source independence, runtime
identity, and citation integrity are correctness invariants. Eval MAY quantify
their value in offline research, but they are not removable production features
even if a small benchmark appears more accurate without them.

Stage 1 may retain or reject an implementation as operationally
working/non-working, but it does not prove architecture efficacy. A strong claim
that one-action adaptive retrieval improves attribution requires A3 on Stage 2
or later with the predeclared denominator satisfied. If proper validation shows
no quality benefit, Catalyst records the negative result and reconsiders or
removes production adaptive behavior only through an explicit design
amendment; it is not silently retained for appearing agentic.

Likewise, production remains `max_actions_per_batch = 1` until A4 runs on Stage
2 or later, satisfies its predeclared multi-gap denominator, and passes the
promotion criteria. A neutral or negative A4 result keeps the production bound
at one action.

## 8. API and true SSE streaming

### 8.1 Run creation and execution

POST /api/live-runs returns after durable admission:

    {
      "run_id": "...",
      "status": "ACCEPTED",
      "stream_url": "/api/live-runs/{run_id}/stream"
    }

The app uses a managed bounded executor, not an unbounded daemon thread per request. V1.1 remains a single-process local Workbench. Concurrent run capacity is pinned between 2 and 4 after load testing. SQLite queue claiming MUST be atomic.

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

followup.started exposes `batch_id`, validated action IDs/gap IDs, evidence
needs, time scopes, bounded reason codes, and research fingerprints. It does not
expose hidden analysis or model-generated tool arguments outside the validated
CorrectiveResearchBatch contract.

answer.started binds the stream to ValidatedClaimPlan and its citations. answer.completed records usage and citations. assurance.completed records validity, violations, and final status.

The public event taxonomy remains intentionally small. New internal stages such
as `OBSERVATION_BUILD`, `CONTEXT_PACK_BUILD`, and `CLAIM_VALIDATION` use
`stage.started` plus artifact references and structured payloads rather than
new bespoke protocol event names.

### 8.4 Workbench projection

The Workbench projects canonical backend state: session/MoveProfile,
initial ResearchTasks, candidate evidence, rerank changes, accepted/lead/rejected
evidence, coverage gaps, corrective batch/actions, hypothesis/claim validation,
streamed answer, and final limitations. Final claim-to-evidence navigation uses
the exact IDs in ValidatedClaimPlan.

The frontend MUST NOT invent quality, support strength, causal role, or primary
driver semantics. It may format and filter backend artifacts; it may not create
a parallel interpretation layer.

### 8.5 Cancellation and timeout

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

The production writer adapter MUST expose real streaming. A provider/model is admitted only after a capability probe verifies streaming, timeout, token accounting, and error normalization. A non-streaming provider may be used by offline tests but cannot satisfy the V1.1 Workbench writer contract.

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
| first answer token without corrective research | p50 < 12 s, p95 < 20 s |
| complete without corrective research | p50 < 20 s, p95 < 35 s |
| complete with one corrective round | p95 < 45 s |

## 10. V1.1 release gates

Hard safety gates:

    ticker violations = 0
    post-cutoff material evidence = 0
    citation resolution = 100%
    runtime identity binding = 100%
    metadata-only material causal evidence = 0
    frontend-fabricated evidence fields = 0
    secret leakage = 0
    Evidence Analyst ContextPack identity binding = 100%
    final PRIMARY/SECONDARY claim lineage binding = 100%

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

Stage-1 adaptive results are implementation/development signals only. They may
show that the bounded corrective path works, is useless, or is broken, but they
cannot establish architecture efficacy or promote a larger action budget.

An A3 architecture-retention claim requires Stage 2 or later and its
human-labelled recoverable subset MUST meet the predeclared
`minimum_eligible_denominator` in EvalManifest. Only after that denominator is
met does the provisional efficacy gate apply: new useful independent evidence
in at least half of eligible cases, zero-new-evidence rate at most 25%, no
increase in false attribution, and median total latency no more than 30% above
the no-corrective policy. Failure to show quality benefit is reported as a
negative result and triggers explicit amendment review of production adaptive
behavior; the loop is not silently retained because it appears agentic.

Every Evidence Analyst call MUST persist and bind
`context_pack_schema_version`, `packing_policy_version`, token budget,
included/excluded evidence IDs, required identity/safety inventory, and
truncation metadata. Its raw AnalystDecision and code-normalized
EvidenceAssessment MUST bind to that same ContextPack. Every final PRIMARY or
SECONDARY causal claim MUST trace from an accepted CandidateHypothesis (or its
normalized accepted claim), through ClaimPlan and ValidatedClaimPlan, to
selected same-run evidence.

`max_actions_per_batch = 2` cannot become a production default without A4 on
Stage 2 or later, a predeclared multi-gap eligible subset and
`minimum_eligible_denominator`, and an explicit promotion report covering
useful independent-evidence recovery, attribution/status correctness, false
attribution, zero-new-evidence rate, latency, retrieval cost, and token cost.
Merely supporting the schema, passing unit tests, or showing one or two useful
Stage-1 cases is insufficient. A neutral or negative result keeps production at
one action.

These are Frozen V1.1 release gates. They may be recalibrated only by an explicit amendment supported by benchmark evidence; they are not silently weakened to make a run pass.

## 11. Implementation order

### P0 — Truth Layer

1. Audit the repo against V1.1 as `KEEP`, `WIRE`, `MODIFY`, `ADD`, or `REMOVE`.
2. Materialize publisher/source/content/dedup metadata through serving; preserve
   SEC parse/materiality, TemporalIdentity, and the canonical evidence API.
3. Create and human-review the 12-case Stage-1 benchmark and evidence/research
   judgments.
4. Re-run the committed baseline and publish retrieval, attribution, latency,
   token, and cost results.

### P1A — Observation and Context Engineering

Align `MoveProfile`, `CoverageSummary`, `ContextPackBuilder`,
`EvidenceAnalystContextPack`, and packing-policy runtime identity. Establish A1
and A5 baselines before changing adaptive research.

### P1B — Evidence Analyst Refinement

Implement bounded CandidateHypothesis competition, refined MissingEvidence,
and research-decision/status/type separation inside the existing Analyst call.
No new LLM node or call is permitted. Run A2.

### P1C — Claim Boundary and Assurance

Implement minimal ClaimPlan, ValidatedClaimPlan, ClaimValidator support-vs-
integrity failure semantics, narrow WriterInput, and structural assurance.

### P1D — Adaptive Research

First implement CorrectiveResearchBatch with production
`max_actions_per_batch = 1` and use Stage 1 to verify that the path is safe and
can work. Compare no corrective research against one action in A3 on the
predeclared Stage-2-or-later eligible subset. Only then add the eval-only
two-action A4 arm for labelled multi-gap cases. Do not promote it automatically.

### P1E — Workbench and Streaming

Project real MoveProfile, tasks, retrieved/reranked evidence, assessment,
coverage gaps, corrective actions, claim/evidence links, and true answer
streaming. Add managed background execution, SSE replay, cancellation, and
bounded backpressure without frontend-generated evidence semantics.

### P2 — Validation Expansion

Move from 12 development cases to 40–60 stratified validation cases plus 15–20
untouched holdout cases. Predeclare A3/A4 eligible-subset definitions and
minimum denominators before observing outcomes; use untouched holdout evidence
before making strong quantitative portfolio claims.

### P3 — Evidence-Driven Extensions Only

Planner, external search, market-structure data, or any additional corrective
round enters planning only through §13 promotion gates.

## 12. Definition of Done

Catalyst vNext V1.1 is complete only when:

- all hard safety and release gates pass on pinned evidence;
- a user immediately receives a run ID and replayable SSE;
- candidates and rerank decisions appear before the answer;
- answer deltas come from a real provider stream;
- every material claim resolves to selected same-run evidence;
- every Analyst call is reproducible from a persisted ContextPack identity and included/excluded evidence inventory;
- every token-constrained ContextPack retains the required identity/safety metadata for omitted material primary and prior counter-evidence payloads;
- round-one packs contain no precomputed semantic counter-evidence, while round-two semantic conflict aids remain bound to the prior Analyst artifact and canonical cumulative evidence;
- raw AnalystDecision artifacts contain neither `status_ceiling` nor an executable CorrectiveResearchBatch, and normalized EvidenceAssessment records code-owned ceilings, gaps, violations, and any batch;
- every run emits at most three serious CandidateHypothesis records and every public causal claim traverses ValidatedClaimPlan;
- CandidateHypothesis uses only causal CauseType values; `NO_MATERIAL_PUBLIC_CATALYST` remains an AttributionType and `MARKET_STRUCTURE_UNSUPPORTED` remains a gap reason;
- normal/corrective paths stay within the two/three logical-model-call budgets, excluding bounded technical retry attempts;
- failure, cancellation, timeout, abstention, and invalidation are visible and testable;
- calendar-aware tests prove prior-close/after-close/pre-market evidence enters the current session information window while post-cutoff evidence does not;
- corrective-round tests prove round two receives the deduplicated union of round-one and new evidence with round/task provenance intact;
- initial-task tests prove bounded parallel execution, deterministic merge order, shared deadline enforcement, and visible per-task degradation;
- at least one Stage-1 benchmark case demonstrates useful, different, bounded corrective research without answer-shopping as an implementation signal, not an efficacy claim;
- each run explains what data/model/code it used, what it retrieved, what it selected or rejected, why it stopped, how long it took, and what it cost;
- canonical suites, replay tests, SSE reconnect tests, secret scans, and pinned GPU/eval gates pass;
- Stage-1 adaptive metrics remain development signals, and A3/A4 architecture conclusions require Stage 2 or later with their predeclared eligible denominators satisfied;
- the two-action corrective arm remains eval-only unless an explicit Stage-2-or-later A4 promotion report passes;
- strong external quantitative claims are withheld until Stage-2 validation and Stage-3 holdout results exist;
- the final report includes negative results and limitations.

## 13. Deferred-capability promotion gates

Deferred capabilities enter a future design amendment only through measurable
residual failure:

- **LLM ResearchPlanner:** reconsider only if deterministic InitialResearchPolicy
  retains a persistent human-labelled required-dimension miss rate after
  MoveProfile, ContextPack, and corrective research are optimized.
- **Conditional external search:** reconsider only if `LOCAL_COVERAGE_GAP` is a
  meaningful residual failure on validation/holdout and conditional recovery
  improves attribution with acceptable reproducibility, latency, and cost.
  Local-first remains the default.
- **Market-structure provider:** reconsider only if
  `MARKET_STRUCTURE_UNSUPPORTED` accounts for a meaningful share of otherwise
  recoverable failures.
- **Second corrective round:** reconsider only if the one-round system still
  leaves a material human-labelled recoverable-gap rate after the parallel
  two-action experiment.

Multi-agent orchestration, free ReAct, GraphRAG, memory, MCP, and distributed
orchestration have no V1.1 promotion path because no measured requirement
currently depends on them.

## 14. Repository implementation deltas

The V1.1 architecture is frozen, but the current repository requires later
implementation work:

1. **MODIFY/WIRE — MoveProfile:** `ContextBuilderArtifact` already supplies
   target/benchmark/sector/peer and volume facts. It should evolve into or feed
   agents-owned MoveProfile rather than be duplicated; gap return,
   scheduled-macro flags, explicit co-movement, and null/degraded semantics are
   incomplete. `peer_summary`, `volume_abnormality`, and the three co-movement
   fields need canonical versioned types before P1A implementation.
2. **MODIFY/ADD — Analyst boundary:** current `CriticResponse` grades chunks and
   current `CriticDecision` exposes sufficiency, fixed next-action strings,
   numeric magnitude coverage, and a rationale. The prompt truncates each chunk
   to roughly 600 characters. It lacks ContextPackBuilder,
   CandidateHypothesis, typed MissingEvidence, raw AnalystDecision, normalized
   EvidenceAssessment, and explicit status/type/ceiling ownership.
3. **MODIFY — corrective wrapper:** current graph correction is the fixed
   `expand_macro` transition with expansion counters. It must evolve into one
   generic code-normalized CorrectiveResearchBatch wrapper; no parallel
   competing workflow should be created.
4. **ADD/WIRE — claim boundary:** current state carries hypothesis lists, Judge
   causes, and Validator output, but no explicit
   ClaimPlan/ValidatedClaimPlan/WriterInput boundary. Existing useful gates
   should be reused while the public claim contract is made explicit.
5. **MODIFY — reasoning artifacts:** current trace/state comments and artifacts
   include a broad `critic_reasoning` field. V1.1 permits bounded reason
   codes/rationales, not persisted hidden chain-of-thought; implementation must
   audit and migrate this field safely.
6. **MODIFY — runtime transport:** the app currently exposes event polling and
   starts a daemon thread per admitted run. Persisted SSE tail/replay and a
   managed bounded executor remain implementation deltas.
7. **KEEP/WIRE — retrieval ablation:** existing four-arm definitions are
   `fts5`, `dense`, `hybrid`, and `reranked` and must be reused. Existing
   post-import evidence contracts remain fail-closed.
8. **ADD — staged eval:** the current T4 pack has 10 cases; the 12-case human
   Stage-1 benchmark, 40–60 validation set, 15–20 holdout, and EvalManifest
   fields for predeclared A3/A4 eligible subsets and minimum denominators do not
   yet exist as V1.1-labelled artifacts.
9. **EXPERIMENT — two-action batch:** the two-action implementation is added
   only as the A4 eval arm after the one-action path exists. It is not wired into
   the production default without the Stage-2-or-later promotion report.

These are implementation-audit inputs, not open architecture decisions. The
later audit classifies files as `KEEP`, `WIRE`, `MODIFY`, `ADD`, or `REMOVE` and
must prefer evolving current components over parallel abstractions.

## 15. Overengineering guardrail

V1.1 MUST NOT add an LLM initial Planner, more than one corrective round, a
two-action production default without benchmark promotion, new LLM nodes,
multi-agent orchestration, a Supervisor, free ReAct, always-on web search,
GraphRAG, long-term memory, MCP, generic SQL/tool agents, microservices, or
distributed queues solely for architectural sophistication.

The allowed architecture is deliberately small:

    trustworthy point-in-time data
    → MoveProfile-aware bounded research
    → explainable hybrid retrieval
    → deterministic ContextPack
    → bounded hypothesis competition
    → at most one corrective round
    → ValidatedClaimPlan
    → true streamed rendering
    → structural assurance and human-grounded evaluation

That measurable chain—not architectural breadth—is the V1.1 technical claim.

## 16. V1.0.3 → V1.1 change summary

| Component | V1.0.3 | V1.1 | Change Type | Reason | Measurement |
|---|---|---|---|---|---|
| core runtime spine | one bounded follow-up | explicit EvidenceState/Coverage/ContextPack stages; one corrective round | CLARIFY | make model-facing state reproducible | runtime/artifact contract tests |
| MoveProfile | prose Observation fields | formal agents-owned null-safe schema | ADD | consistent scenario context | A5 routing/attribution |
| ContextPackBuilder | implicit prompt formatting | deterministic versioned builder | ADD | control model-visible evidence | A1 quality/tokens/latency |
| EvidenceAnalystContextPack | direct bounded context implied | first-class derived view with identity/safety inventory distinct from bounded text payload | ADD | replay and safe token-pressure packing | identity/inventory/truncation gate |
| round-specific conflicts | conflict/counter evidence implicit | round one deterministic signals only; round two may carry prior semantic conflicts over canonical cumulative state | CLARIFY | preserve deterministic/model boundary | round-one/round-two pack tests |
| round-two context | cumulative evidence | recompute canonical state and rebuild pack; delta highlighting only | CLARIFY | avoid lossy self-summary | cumulative-state tests |
| CandidateHypothesis | candidate claims | at most three competing hypotheses using causal CauseType values only | MODIFY | reduce premature causal closure and ontology mixing | A2 plus schema negatives |
| hypothesis competition | implicit | bounded within Analyst call | ADD | compare plausible causes without new node | A2 |
| AnalystDecision vs EvidenceAssessment | model/code ownership implicit | raw model decision followed by code-normalized assessment | CLARIFY | keep ceilings and executable actions deterministic | structured-output/normalization tests |
| CorrectiveResearchBatch | one FollowUpAction | batch wrapper, one round | MODIFY | experimental seam without extra autonomy | A3/A4 |
| max_actions_per_batch | one action | production 1; experimental 2 | EXPERIMENT | test multi-gap recovery | A4 promotion gate |
| status/type semantics | separate | unchanged; research decision also separated | KEEP | prevent control/result conflation | schema/status tests |
| source independence | deterministic fail-closed | unchanged and explicitly code-only | KEEP | prevent false corroboration | independence negatives |
| materiality | hard content-state ceilings | unchanged | KEEP | prevent unusable primary evidence | materiality gates |
| ClaimPlan | flat implied claim record | minimal claims list with roles | MODIFY | Writer/UI/eval boundary | lineage tests |
| ValidatedClaimPlan | validated plan implied | explicit maximum public claim set | ADD | enforce Writer boundary | claim/evidence gate |
| ClaimValidator failures | validator checks | support downgrade vs integrity failure | CLARIFY | preserve ABSTAIN metrics | failure taxonomy tests |
| Writer | expression layer | narrow WriterInput over ValidatedClaimPlan | CLARIFY | prevent new facts/roles/status | streaming/lineage tests |
| Assurance | structural | unchanged and explicitly non-semantic | KEEP | honest runtime guarantee | assurance tests |
| SSE | lean persisted events | unchanged; rich artifacts/stage payloads | KEEP | protocol stability | replay/order tests |
| benchmark staging | 12-case initial benchmark | 12 dev, 40–60 validation, 15–20 holdout | ADD | credible quantitative claims | split/lineage validation |
| ablation program | retrieval/follow-up gates | A1–A5 with Stage-1 development signals and predeclared Stage-2 eligible denominators for A3/A4 | ADD | require statistically meaningful evidence before autonomy claims | pinned manifests/ablation reports |
| deferred capabilities | broad P2 deferral | measurable promotion gates | CLARIFY | evidence-driven evolution | validation/holdout residuals |
