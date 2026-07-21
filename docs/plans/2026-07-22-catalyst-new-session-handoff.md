# Catalyst New-Session Handoff

- Date: 2026-07-22
- Repository: `/Users/yiannischen/Desktop/Catalyst`
- Branch: `ws4b/article-level-data`
- HEAD at handoff: `4d4e3a938ac6ee4720f13586c5b281217d75308d`
- Immediate status: **B2 has not started**
- Next executable package: **B2 — Data Update and Provenance**

This file is the starting point for the next session. Read it before older plans or reports. Do not reconstruct project status from the full chat transcript unless a fact below needs verification.

## 1. Responsibility Model

### User — Architect and Final Approver

The user owns final product scope, architecture sign-off, provider spending, live operations, database migrations, Git commits, pushes, and any decision that changes a locked contract.

### Opus 4.8 — Senior SDE and System Architect

Opus 4.8 owns high-level planning and difficult design decisions:

- product and system architecture;
- package boundaries and dependency direction;
- industrial-agent standards and non-toy exit criteria;
- design trade-offs and YAGNI decisions;
- adversarial review of plans and cross-document consistency;
- decisions that materially change B2–B7 contracts.

Opus should not be used for routine repository inspection, straightforward test execution, Git housekeeping, or mechanical plan edits. Escalate to Opus only when a real architectural decision remains after code-level verification.

### Codex — Orchestrator, Reviewer, and Repository Operations

Codex owns the integration and verification work:

- inspect the repository and verify all reported green claims independently;
- review dscodex reports against real code, databases, and artifacts;
- run canonical tests in the main `.venv`;
- perform landmine checks and identify tautological tests;
- make small, verified plan corrections and narrowly scoped hygiene fixes;
- manage Git status, staging proposals, commit boundaries, and safety checks;
- write prompts for Opus and dscodex;
- keep the user informed about current state, blockers, and decisions.

Codex must not blindly accept dscodex or Opus reports. It must reproduce important claims itself.

### dscodex — Writing Plans and Execution Worker

dscodex owns detailed implementation work:

- use `superpowers:writing-plans` when a new execution plan is required;
- use Goal mode plus `superpowers:executing-plans` for an approved plan;
- implement with strict TDD;
- run focused and canonical tests;
- produce exact evidence reports;
- pause after each package for Codex review.

dscodex must not make architecture decisions, stage files, commit, push, call live providers, inspect secrets, mutate canonical databases, or silently expand scope.

## 2. Locked Project Positioning

Catalyst is a deliberately scoped, non-toy open-source AI systems project. It is not a commercial SaaS, Bloomberg replacement, universal agent framework, research paper, or autonomous financial adviser.

The portfolio claim is:

> An evidence-bounded attribution workbench demonstrating reproducible provider ingestion, temporal retrieval correctness, RAG/reranker measurement, constrained agent workflow, persisted assurance, and zero-network evaluation replay.

The project is intended to provide:

- a credible first agent/RAG systems project;
- evidence for Fall 2027 master's applications in AI systems and agentic workflows;
- backend, reliability, retrieval, evaluation, and infrastructure interview material;
- a trustworthy repository that supports later contributions to established open-source projects;
- a project that meets Professor Mac's non-toy standard through explicit contracts, reproducibility, meaningful tests, failure handling, and honest limitations.

The target is a strong, finished 60/100 open-source system. Do not add features merely to appear industrial.

## 3. Locked Architecture

The package direction is:

```text
data-core
   ↑
agents
   ↑
app

eval consumes versioned artifacts.
Production packages never import eval.
```

The execution order is:

```text
B1 repository cleanup and design freeze — current mixed working tree
B2 data update and provenance — NOT STARTED
B3 corpus and chunking
B4 cutoff-safe lexical retrieval and eval foundation
B5 attribution runtime and per-run assurance
B6 dense retrieval, RRF and reranker
B7 local API, evaluation replay, fixtures and backend release
```

Frontend work starts only after the backend and API exit gates pass.

Do not reopen these decisions during B2:

- two LLM calls in the attribution workflow;
- deterministic Context Builder before model judgment;
- cutoff enforcement before retrieval scoring;
- `catalyst_data/retrieval/` as the canonical retrieval implementation home;
- agents do not open corpus SQLite directly;
- per-run assurance belongs in agents runtime;
- eval remains an artifact consumer;
- reranker is implemented and measured but may be disabled if it adds no named-case value;
- local typed API, not public SaaS infrastructure;
- migration ownership B2=v8, B3=v9, B4=v10.

## 4. Canonical Documents

Read in this order:

1. `docs/plans/2026-07-22-catalyst-new-session-handoff.md`
2. `docs/plans/2026-07-21-b2-b7-technical-contracts.md`
3. `docs/plans/2026-07-21-catalyst-roadmap.md`
4. `docs/plans/2026-07-22-b2-b7-pre-execution-ratification.md`
5. `docs/plans/2026-07-22-b2-data-update-provenance.md`
6. Later, only when their package starts:
   - `docs/plans/2026-07-22-b3-corpus-chunking.md`
   - `docs/plans/2026-07-22-b4-cutoff-safe-lexical-retrieval.md`
   - `docs/plans/2026-07-22-b5-attribution-runtime-assurance.md`
   - `docs/plans/2026-07-22-b6-dense-reranker.md`
   - `docs/plans/2026-07-22-b7-api-evaluation-release.md`

Supporting canonical designs:

- `docs/plans/2026-07-19-catalyst-open-source-workbench-design.md`
- `docs/plans/2026-07-19-catalyst-final-package-architecture.md`
- `docs/plans/2026-07-19-catalyst-evaluation-architecture-review.md`
- `docs/plans/2026-07-19-catalyst-provider-provenance-and-chunking-design.md`

Older deleted plans and thesis-era reports are not authoritative.

## 5. Current Verified Baseline

The final pre-B2 verification used the main `.venv` and produced:

```text
packages/data-core: 758 passed, 1 skipped, 1 xfailed
packages/agents:    237 passed
packages/eval:       93 passed
packages/app:       128 passed
```

The formerly flaky app test `test_retry_semantics_and_lineage` was reproduced failing once in ten package runs. Its test setup was repaired so the parent run is created without launching a background worker. The focused test then passed 30 consecutive runs, and the app package passed canonically.

Protected database identities:

```text
data/catalyst_dev_ws4b.db
92731fb7c5c3e989b4fdcbefb9d1d2060974082a6c60f1bbecad6bc42ee846b0

data/catalyst_eval_frozen_v2.db
0d97a7ec61b6ec8fb5f9263b0b37b0efc9755812739d7afead7f720c3567e8dd
```

The current frozen SHA is the protected working artifact for B2–B7. Do not reopen the historical `0dfc81...` incident during B2. Never re-pin either database without explicit user authorization.

The Git index was empty at handoff. No B2 implementation files have been created or modified by dscodex.

## 6. Final Pre-B2 Amendments Already Applied

Before this handoff, Codex made the following narrow corrections:

- `packages/app/tests/test_failure_paths.py`
  - removed the retry-test background-worker race;
- `docs/plans/2026-07-22-b2-data-update-provenance.md`
  - declared every transport and count fixture used by later tests;
  - required fixture count oracles to be independent of the production planner;
  - replaced undefined `sha256()` examples with `hashlib.sha256(...).hexdigest()`;
  - explicitly prohibited autonomous execution of the live provider-canary path;
- `docs/plans/2026-07-22-b2-b7-pre-execution-ratification.md`
  - records the final pre-execution state and verification;
- `docs/plans/2026-07-21-catalyst-roadmap.md`
  - corrected Context Builder from “existing” to a new B5 component.

These changes are unstaged.

## 7. Git and Worktree State

The current branch contains a large mixed, unstaged B1 working tree. It includes intended documentation cleanup, package-boundary work, earlier W1-A/W1-B/W1-C implementation, canonical design documents, and unrelated local provider artifacts.

Do not create a new B2 worktree from current HEAD. The committed HEAD does not contain all unstaged B1 prerequisites required by B2.

Do not reset, stash, clean, checkout, rebase, rename the branch, stage, commit, or push during B2 execution.

Never stage:

- `data/provider_discovery/`
- `data/provider_probe/`
- `data/catalyst_eval_frozen_v2.db.damaged-0d97-backup`
- `packages/data-core/scripts/provider_discovery.py`
- `scripts/discover_models.py`
- `scripts/probe_news_sources.py`
- `scripts/run_polygon_live.sh`
- `.env` or any secret-bearing file
- canonical database files

After B2 passes Codex review, Codex will prepare an exact staging proposal. The user must authorize commits. Commit messages use Conventional Commits and must not mention assistants or generated content.

## 8. Immediate Next Action — Start dscodex B2 Goal

B2 has not started. Start it with:

```text
/goal Execute and independently verify Catalyst B2 only from docs/plans/2026-07-22-b2-data-update-provenance.md using the main .venv and strict TDD. Do not stage, commit, push, inspect secrets, call live providers, mutate canonical databases, or start B3. Complete Tasks 0-11, but for Task 11 implement and test only the authorization guard with fake transport; never execute the authorized live-canary path. Stop and pause after producing the complete B2 evidence report.
```

dscodex must read the B2 plan and technical contract before editing. It must use `superpowers:executing-plans`, `superpowers:test-driven-development`, `superpowers:systematic-debugging` for unexpected failures, and `superpowers:verification-before-completion` before reporting success.

## 9. B2 Mandatory Before-State

dscodex must record:

```bash
git branch --show-current
git rev-parse HEAD
git status --porcelain=v1
git diff --cached --name-status
git worktree list
shasum -a 256 data/catalyst_dev_ws4b.db
shasum -a 256 data/catalyst_eval_frozen_v2.db
sqlite3 'file:data/catalyst_dev_ws4b.db?mode=ro' 'PRAGMA user_version;'
```

It must save the pre-B2 status and relevant package diffs under `/tmp` so the final report distinguishes B2 changes from the existing mixed tree.

## 10. B2 Execution Boundaries

dscodex may implement only the B2 plan:

1. isolated DB and HTTP fixtures;
2. migration v8;
3. plan-hash completeness and `PlanDriftError`;
4. request fingerprint and redaction;
5. request-attempt ledger;
6. append-only raw-response store;
7. normalized provenance;
8. Polygon pagination;
9. durable cancellation, lease and resume;
10. two-stage OHLCV-first execution and evidence re-plan;
11. connector instrumentation;
12. provider-canary authorization guard tested only with fake transport.

Important invariants:

- migration v8 is tested only on temporary DBs and disposable copies;
- canonical Dev DB migration is not part of B2;
- one HTTP response/page creates one append-only raw row;
- same request ID and same SHA is idempotent;
- same request ID and different SHA is an integrity error;
- normalized entities preserve many-to-many raw provenance;
- plan drift fails before the first write or network call;
- exactly one asynchronous `execute_update` entry point exists;
- OHLCV commits before evidence is re-planned;
- partial pages never report full success;
- cancellation is checked at cell, page, fallback, and commit boundaries;
- secrets are redacted before logging or persistence;
- transaction failure leaves no partial raw/entity/provenance/checkpoint state.

## 11. Mandatory Stop Conditions

dscodex must stop and pause immediately if:

- either protected DB SHA changes;
- a canonical DB migration is attempted;
- a socket or live provider call occurs;
- a secret appears in output or artifacts;
- a package outside the B2 allowlist must change;
- migration ownership conflicts with v9 or v10;
- an existing assertion must be weakened, skipped, or xfailed;
- data-core imports agents, eval, or app;
- Git index becomes non-empty;
- any canonical package suite regresses;
- a step requires an unratified architecture decision;
- B3 work becomes necessary.

Do not work around a stop condition.

## 12. B2 Completion and Review Protocol

dscodex must produce an evidence report containing:

- exact files created and modified;
- red/green evidence per task;
- migration v8 schema and ownership;
- focused and canonical test counts with exit codes;
- request/raw/provenance invariants;
- pagination, partial-result, rollback, drift, cancellation, lease, and resume proofs;
- secret-redaction evidence using runtime-random sentinels;
- confirmation that no authorized live canary ran;
- before/after DB SHAs;
- Git index state;
- remaining risks;
- confirmation that B3 did not start.

After the report, dscodex pauses Goal mode. Codex then independently reruns the landmines and canonical suites. Only after Codex review may the user authorize a Git boundary or the start of B3.

## 13. Communication Discipline in the New Session

- Separate verified facts from worker claims.
- Do not call a suite green without running it in the main `.venv`.
- Do not treat an isolated rerun as proof that a full-suite failure is harmless.
- Do not accept empty, tautological, fixture-self-derived, or broad-exception tests.
- Do not reopen settled architecture during routine implementation.
- Escalate real design conflicts to Opus 4.8 with a focused prompt containing verified evidence and explicit choices.
- Keep the user-facing status concise: current package, verified result, blocker, next owner, next action.
