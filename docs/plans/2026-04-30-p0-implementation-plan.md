# Catalyst P0 Implementation Plan (Defense-Window Sprint)

**Plan revision:** v2 (post-strict-review, 2026-04-30). Supersedes v1 of the same date.

**v1 → v2 changes (each addresses one finding from the strict review):**
- B-1: `git status -uall` removed from §5 (CLAUDE.md prohibits `-uall`).
- B-2: T-04 budget branch removed (was P2 scope creep per strategy spec W-04).
- B-3: T-05 sequencing fixed — baseline pipeline built against `catalyst_dev.db`; `catalyst_eval_frozen.db` is only referenced from T-10 onward.
- B-4: `check_p0_gate.py` creation lifted into T-11 as Subtask 11.6 (was orphaned).
- B-5: Day 8 → Day 13 source-freeze invariant added as §6.bis; T-15 cross-reference uses the SHA captured at T-10 with the invariant guaranteeing no `packages/` drift in between.
- B-6: every "(or equivalent)" / "(or create)" placeholder replaced with one concrete path.
- B-7: T-13 rerun outputs land at `data/eval_reports/rerun_<timestamp>_mcj_full.{md,json}` and `data/traces/rerun_<run_id>.json`; frozen paths are read-only.
- B-8: new §0.bis Commit-Authorization Protocol — task-boundary commits explicitly enumerated; verification commands using `git log` are gated by user authorization at those boundaries.
- B-9: T-09 ingestion references concrete scripts under `packages/data-core/scripts/` instead of "targeted ingestion".
- B-10: T-01 contradiction resolved — strict removal of "legacy stock-attribution framing" with no history-block exception.
- N-2: T-03 baseline-test-count recording added as Subtask 3.0.
- N-3: T-03 time-window failure disposition specified — same as evidence-id failure (regenerate once → downgrade `PARTIAL`).
- N-4: §1 file-touch map adds `packages/eval/tests/test_direct_llm_baseline.py`.
- N-5: T-06 fixture-data subtask added (Subtask 6.0).
- N-7: T-08 explicit `### <ClassName>` heading format mandated; verification matches it.
- N-9: T-11 deck verification removed format-locked grep; replaced with explicit content-presence check via filename of slide files.
- N-10: `notebooks/` directory creation moved into T-02 as Subtask 2.5.
- I-3: `geo_corpus_tier` field declared as a sidecar JSON column in the preflight artifact (no DB schema mutation needed).

> **For agentic workers:** REQUIRED SUB-SKILL — `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans`. Steps use checkbox (`- [ ]`) syntax for tracking. **Per project CLAUDE.md, never run `git commit` or `git push` without explicit user authorization. `git add` (stage) is the maximum default action.**

## §0.bis Commit-Authorization Protocol (mandatory reading before any task)

CLAUDE.md mandates stage-only by default. Several verification commands in this plan use `git log` and `git tag`, which require committed state. To reconcile:

- At the **end of each task** (T-01 through T-16), the executing agent stages all changes with `git add` and **prompts the user explicitly** for commit authorization. The user, if satisfied, runs (or authorizes the agent to run) `git commit -m "<task tag>: <summary>"`.
- Tasks that gate downstream verification on `git log` are: T-10 (golden set freeze commit needed for §6.bis immutability check), T-15 (defense-freeze tag).
- Until the user authorizes the T-10 commit, §6 / §6.bis / §7 immutability and freeze checks are **not runnable**. The plan executor must surface this as a blocker, not silently skip.
- Tasks T-01 through T-09 may proceed staged-only with no immediate verification penalty; commits at end-of-day are recommended for clean history but not load-bearing.

**Goal:** Deliver the P0 closed loop for thesis defense — narrative consistency, control-plane hardening (validator, four-state output, deterministic DecisionRouter, Layer 1+2 retrieval, trace persistence, failure taxonomy with regression tests), real eval (`direct_llm` vs `mcj_full` on a 10-case 5/2/3 set), and pre-recorded defense artifacts — within 14 calendar days.

**Architecture:** Extend the existing midterm Miner-Critic-Judge graph with a Validator node, a deterministic DecisionRouter, and Critic decision-contract output. Implement Layer 1 + Layer 2 retrieval; stub Layer 3. Persist run-level traces to local SQLite. Score runs against a frozen golden set with `evidence_validity`, `schema_validity`, `trace_completeness`, `should_refuse_hit_rate` as gating metrics; Tier-A reproducibility pinned via `model_id`, DB SHA-256, and git SHA recorded in the frozen eval report header.

**Tech Stack:** Python 3.11+, existing monorepo (`packages/data-core`, `packages/eval`, `packages/agents`), SQLite (WAL), LanceDB, LangGraph (already in `graph.py`), `uv`/pip editable installs, pytest.

**Sources:**
- `docs/superpowers/specs/2026-04-27-catalyst-fullversion-strategy.md` (r3) — strategy, gates, waivers, task contracts
- `docs/full-version-execution-spec.md` — technical execution baseline (narrowed by §11 waivers in strategy spec)

**Hard scope rules** (enforced by §6.1 Day 10 Gate, §8 Engineering Hard Rules in strategy spec):
- P0 only. P1 (rag_only, expanded regression tests, LangSmith, eval-package extraction) and P2 (Layer 3, LLM-graded sufficiency, budget circuit breaker, model routing) are explicitly forbidden inside this plan's execution scope.
- Sample protocol (10 cases, 5/2/3 distribution) is **immutable after Day 8 freeze**. Reducing the case count to make a gate pass is a protocol violation.
- Day 13 EOD = code freeze. Day 13–14 commits must touch only the freeze whitelist (see §7).

---

## 0. Reference Project Patterns (≤5, design-level only)

These patterns are **borrowed in shape**, not imported as code. No new dependencies, no new subsystems.

| # | Source pattern | Where it lives in the source | Catalyst landing | Risk + downgrade |
|---|---|---|---|---|
| R-1 | **deepeval / ragas — gate-based eval runner**: each test case carries declarative pass/fail thresholds; runner aggregates per-metric pass/fail and exits non-zero on any failed gate. | deepeval `evaluate()` API; ragas `Score` aggregation. | `packages/eval/catalyst_eval/harness/` — extend the existing harness with a gate-enforcement layer that reads gates from a config object and emits the gate report. **No deepeval/ragas import.** | Risk: borrowing more surface than needed. Downgrade: implement the smallest gate-aggregator (≤80 lines); revert to printing summary if gate enforcement breaks reproducibility. |
| R-2 | **deepeval — golden set as JSONL with `input` + `expected` + metadata fields**, including a `should_refuse` flag where applicable. | deepeval `LLMTestCase` schema. | `packages/eval/golden_set/v1_2.jsonl` — extend each row's schema with `expected_status` (`SUFFICIENT/PARTIAL/INSUFFICIENT`) and `should_refuse` boolean. | Risk: schema bloat. Downgrade: keep extension to two new fields only; defer richer metadata to P2. |
| R-3 | **gpt-researcher — phase enum drives the agent loop**, with each phase producing a state delta consumed by the next. | gpt-researcher main agent loop's phase machine. | `packages/agents/catalyst_agents/state.py` — add `Phase` enum (parser, retrieval_l1, retrieval_l2, miner, critic, router, judge, validator, finalizer); `graph.py` references the enum on every node transition. | Risk: re-architecting graph.py. Downgrade: enum is **labels only**; existing LangGraph wiring stays, enum drives traceability not control flow. |
| R-4 | **opengpts / LangGraph — strict per-node input/output state contracts**, enforced by typed state dataclasses. | LangGraph state schemas in opengpts node definitions. | `packages/agents/catalyst_agents/nodes/` — Validator and DecisionRouter declare typed input/output dataclasses; existing nodes get type hints added (no behavior change). | Risk: type churn breaks midterm tests. Downgrade: add type hints behind `if TYPE_CHECKING:` to keep runtime contract loose if type system causes friction. |
| R-5 | **PokieTicker — per-stage cost/latency ledger + structured report tables**: every pipeline stage logs `{stage, latency_ms, cost_usd, tokens}` to a single ledger; report aggregates into markdown + JSON. | PokieTicker pipeline reporting layer. | Extend existing `packages/agents/catalyst_agents/cost_tracker.py` to record per-node entries; new writer in `packages/eval/catalyst_eval/reports/` emits markdown+JSON tables. | Risk: pulling in PokieTicker UI/dashboard. Downgrade: text reports only; no visualization, no live dashboard. |

**Hard rule:** If any pattern adoption requires creating a new package, a new top-level directory, or a new external dependency, abandon the borrow and ship without it. P0 cannot afford pattern-driven scope drift.

---

## 1. File-Touch Map (where work lands)

This is the canonical list of files modified or created in P0. Anything not on this list is out of scope.

### Documentation (T-01)
- Modify: `README.md`
- Modify: `docs/current_situation.md`
- Modify: `packages/data-core/README.md`, `packages/eval/README.md`, `packages/agents/README.md`
- Add header to (recursively): every `*.md` under `docs/` not in the four-anchor allowlist

### Database / paths (T-02)
- Create: `data/catalyst_dev.db` (touch via existing data-core init), `data/catalyst_eval_frozen.db` (stub), `data/catalyst_demo.db` (stub)
- Create: `data/eval_reports/.gitkeep`, `data/traces/.gitkeep`, `docs/testing/.gitkeep` (if directory empty)
- Modify: data-core config to read `CATALYST_DB_PATH` env var, defaulting to `data/catalyst_dev.db`

### Control-plane (T-03 → T-04 → T-06)
- Modify: `packages/agents/catalyst_agents/state.py` — add `Phase` enum (R-3); add `OutputStatus` enum (`SUFFICIENT/PARTIAL/INSUFFICIENT/SYSTEM_ERROR`); add `CriticDecision` dataclass with `sufficiency`, `next_action`, `magnitude_coverage`, `reasoning`
- Create: `packages/agents/catalyst_agents/nodes/validator.py`
- Create: `packages/agents/catalyst_agents/nodes/decision_router.py`
- Modify: `packages/agents/catalyst_agents/nodes/critic.py` — emit `CriticDecision` schema
- Modify: `packages/agents/catalyst_agents/graph.py` — wire `Parser → RetrievalPolicy → Miner → Critic → DecisionRouter → Judge | Refuse → Validator → Finalizer` (note: no `Expand` edge as a terminal — DecisionRouter routes `expand_macro` back into the retrieval node)
- Create: `packages/agents/catalyst_agents/retrieval/policy.py` — Layer 1 + Layer 2 dispatcher; Layer 3 stub raising `NotImplementedError`
- Create or Modify: `packages/agents/catalyst_agents/retrieval/__init__.py` — export policy
- Create: `packages/agents/tests/fixtures/retrieval_fixture.py` (per T-06 Subtask 6.0; only if no comparable midterm fixture exists for reuse)
- Create: `packages/agents/tests/test_validator.py`, `test_decision_router.py`, `test_retrieval_policy.py`

### Baseline (T-05)
- Create: `packages/eval/catalyst_eval/harness/baselines/direct_llm.py`
- Create: `packages/eval/catalyst_eval/harness/baselines/__init__.py`
- Create: `packages/eval/tests/test_direct_llm_baseline.py`

### Trace (T-07)
- Create: `packages/agents/catalyst_agents/trace/schema.py` — SQLite schema definition
- Create: `packages/agents/catalyst_agents/trace/writer.py`
- Create: `packages/agents/catalyst_agents/trace/exporter.py` — JSON exporter to `data/traces/<run_id>.json`
- Modify: `packages/agents/catalyst_agents/graph.py` — instrument every node transition

### Failure taxonomy + regressions (T-08)
- Create: `docs/testing/failure-taxonomy.md`
- Create: `packages/agents/tests/test_failure_taxonomy.py`

### Preflight (T-09)
- Create: `packages/eval/scripts/preflight.py`

### Eval gates + reports (T-10, T-11)
- Modify or Create: `packages/eval/catalyst_eval/harness/runner.py` — gate enforcement (R-1). Modify if it exists from midterm; create at this exact path otherwise.
- Modify: `packages/eval/golden_set/v1_2.jsonl` — extend schema (`expected_status`, `should_refuse`); add 3 should-refuse cases (R-2)
- Create: `packages/eval/catalyst_eval/reports/markdown_writer.py`, `packages/eval/catalyst_eval/reports/json_writer.py` (R-5)
- Create: `packages/eval/scripts/check_p0_gate.py` — Day 10 Gate executable (per T-11 Subtask 11.6)
- Create: `data/eval_reports/<frozen_timestamp>_direct_llm.{md,json}`, `<frozen_timestamp>_mcj_full.{md,json}`
- Create: `data/eval_reports/preflight_<timestamp>.json`
- Create: `data/eval_reports/<frozen_timestamp>_comparison.{md,json}`

### Day 10 Gate + Audit + Demo (T-12, T-13, T-14)
- Create: `docs/testing/audit_<date>.md` — Day 10 Gate decision record + Day 11–12 audit log
- Create: `data/eval_reports/rerun_<timestamp>_mcj_full.{md,json}` — T-13 reproducibility rerun (separate from frozen artifact)
- Create: `data/traces/rerun_<run_id>.json` — T-13 rerun traces
- Create: `notebooks/demo.ipynb` (cleared outputs) + `notebooks/demo_executed.ipynb` (executed)

### Defense materials (T-15, T-16)
- Create: `docs/defense/deck.md` (or equivalent — see Open Decision OD-1)
- Git tag: `defense-freeze-YYYY-MM-DD` (annotated)

---

## 2. P0 Traceability Matrix (T-01 → T-16)

Each task: subtasks, files, done definition, dependencies.

### T-01 Doc Sweep (Day 1)

**Depends on:** None. Parallelizable with T-02.
**Estimated:** 6 hours.

- [ ] **Subtask 1.1:** Rewrite first 30 lines of `README.md` to match strategy spec §1.1–1.3 (headline + active claims + non-claims). Remove every occurrence of "legacy stock-attribution framing".
- [ ] **Subtask 1.2:** Rewrite `docs/current_situation.md` positioning paragraph (§1.1 in that doc), the "已实现与边界" section (must reference four-state output, validator, trace as defense-phase additions), and the "下一步" section (must point at strategy spec).
- [ ] **Subtask 1.3:** Rewrite `packages/{data-core,eval,agents}/README.md` lead paragraphs to align with new positioning. Keep technical setup blocks unchanged.
- [ ] **Subtask 1.4:** Add supporting-reference header to every non-anchor doc:
  ```
  > Supporting reference. Canonical source: <relative-path-to-anchor>
  ```
  Anchors: README.md, docs/superpowers/specs/2026-04-27-catalyst-fullversion-strategy.md, docs/full-version-execution-spec.md, docs/current_situation.md.
- [ ] **Subtask 1.5:** Stage all changes; do not commit.

**Done definition (Exit):** README front-matter aligned; `current_situation.md` rewritten; supporting-reference headers present on all non-anchor docs.

**Verification:**
```bash
# 1. Old positioning is gone — strict removal, no exceptions.
grep -ri "legacy stock-attribution framing" README.md docs/ packages/*/README.md
# Expected: zero hits, period. Strategy spec history blocks must use a different paraphrase
# (e.g., "the original attribution framing") rather than the verbatim deprecated phrase.

# 2. Recursive header check on docs/.
find docs -type f -name "*.md" \
  ! -path "docs/superpowers/specs/2026-04-27-catalyst-fullversion-strategy.md" \
  ! -path "docs/full-version-execution-spec.md" \
  ! -path "docs/current_situation.md" \
  ! -path "docs/superpowers/specs/2026-04-02-catalyst-system-design.md" \
  ! -path "docs/superpowers/specs/2026-04-07-catalyst-midterm-freeze-boundary.md" \
  | xargs grep -L "Supporting reference"
# Expected: empty output.

# 3. Package READMEs reflect the new positioning. Match any of the three new active claims
#    (evidence validity / refusal / replayability / failure governance) so the verification
#    is not format-locked to one phrase.
for f in packages/*/README.md; do
  grep -qE "(evidence-bounded|evidence validity|refusal|replayability|failure governance)" "$f" || echo "MISSING: $f"
done
# Expected: no MISSING lines.
```

### T-02 DB Triple Split + Artifact Paths (Day 2 AM)

**Depends on:** None. Parallelizable with T-01.
**Estimated:** 3 hours.

- [ ] **Subtask 2.1:** Inspect existing `packages/data-core/catalyst_data/` to identify the current DB-path resolution (config module, environment variable, or hardcoded path). Record findings in this plan as a one-line note before proceeding. **Also inspect `data/` for existing DB files** (e.g. `data/dev_assets.db` is known to exist). The implementation in 2.2 must extend the existing mechanism, not introduce a parallel one.
- [ ] **Subtask 2.2:** Create three SQLite files via existing data-core init: `data/catalyst_dev.db`, `data/catalyst_eval_frozen.db`, `data/catalyst_demo.db`. Eval and demo may be empty stubs (schema-only). **If a pre-existing `data/dev_assets.db` (or other midterm dev DB) is already in active use, do not delete it; either symlink `data/catalyst_dev.db -> dev_assets.db` or copy the schema, depending on what 2.1 found. The choice is recorded in the inspection note from 2.1.**
- [ ] **Subtask 2.3:** Add env var `CATALYST_DB_PATH` (default `data/catalyst_dev.db`) wired into the resolution mechanism identified in 2.1. **No new config system.** If the existing layer does not expose a `db_path()` callable, expose one as the minimum addition; otherwise extend the existing API.
- [ ] **Subtask 2.4:** Create directories `data/eval_reports/`, `data/traces/`; ensure `docs/testing/` and `notebooks/` exist (create with `.gitkeep` if absent). Add `.gitkeep` to any newly created directory.
- [ ] **Subtask 2.5:** Stage all changes; prompt user for commit authorization at task close per §0.bis.

**Done definition:** Three DB files exist on disk; env var override works; directories present including `notebooks/`.

**Verification:**
```bash
ls data/catalyst_dev.db data/catalyst_eval_frozen.db data/catalyst_demo.db
ls data/eval_reports/ data/traces/ docs/testing/ notebooks/

# Resolve the path API as discovered in 2.1; the assertion below assumes a `db_path()`
# callable was exposed (the simplest extension). If 2.1 found a different shape,
# substitute the equivalent invocation discovered.
CATALYST_DB_PATH=/tmp/test_catalyst.db python -c "
from catalyst_data.config import db_path
result = str(db_path())
assert result == '/tmp/test_catalyst.db', f'env override failed: got {result!r}'"
# Expected: no AssertionError (silent success).
```

### T-03 Validator + 4-State Output + Critic Schema (Day 2 PM → Day 4 AM)

**Depends on:** T-02.
**Estimated:** 12 hours.

- [ ] **Subtask 3.0:** Record midterm test baseline. Run `cd packages/agents && /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest tests/ -q --tb=no 2>&1 | tail -5 > /tmp/agents_baseline.txt`. Capture passing-test count for use as the regression bar in Subtask 3.7.
- [ ] **Subtask 3.1:** In `state.py`, add `OutputStatus(Enum)` with values `SUFFICIENT/PARTIAL/INSUFFICIENT/SYSTEM_ERROR`. Add `Phase(Enum)` (R-3). Add `CriticDecision` dataclass: `sufficiency: Literal["sufficient","partial","insufficient"]`, `next_action: Literal["proceed","expand_macro","expand_related","refuse"]`, `magnitude_coverage: float`, `reasoning: str`.
- [ ] **Subtask 3.2:** Modify `packages/agents/catalyst_agents/nodes/critic.py` to emit `CriticDecision`. Internal sufficiency rule (per spec §3.3 + Open Decision OD-2):
  - `sufficient` if `evidence_count >= K_sufficient` AND `magnitude_coverage >= M_threshold`
  - `partial` if `K_partial <= evidence_count < K_sufficient` OR `magnitude_coverage < M_threshold`
  - `insufficient` otherwise
  - Constants `K_sufficient`, `K_partial`, `M_threshold` declared at module scope; calibrated in T-10.
- [ ] **Subtask 3.3:** Create `packages/agents/catalyst_agents/nodes/validator.py` with four checks per execution-spec §8: (a) every cited `evidence_id` exists in the run's evidence set; (b) the evidence's `published_at` (or comparable date field returned by the existing data-core query layer) falls within the case's allowed time window; (c) output schema matches the contract; (d) magnitude sanity — if claim is "complete" but `magnitude_coverage < threshold`, downgrade `status` to `PARTIAL`.
- [ ] **Subtask 3.4:** Failure dispositions on validator rejection (one regenerate attempt; on second failure):
  - Evidence-id failure (a) — status downgrades to `PARTIAL` (cannot recover citations without new retrieval).
  - Time-window failure (b) — status downgrades to `PARTIAL` (same disposition as evidence-id failure, recorded with `reason=time_window_violation` in trace).
  - Schema failure (c) — status downgrades to `PARTIAL`.
  - Magnitude failure (d) — status downgrades to `PARTIAL` (no regenerate; magnitude is structural).
  - Model timeout / parse error (any) — status becomes `SYSTEM_ERROR`.
- [ ] **Subtask 3.5:** Wire Validator node into `graph.py` between Judge and Finalizer.
- [ ] **Subtask 3.6:** Write unit tests in `packages/agents/tests/test_validator.py`:
  - Test: validator rejects output with non-existent `evidence_id`, triggers regenerate, second failure → `PARTIAL`.
  - Test: validator rejects evidence dated outside the case's time window with `reason=time_window_violation`.
  - Test: validator forces `PARTIAL` when `magnitude_coverage < 0.5` while claim is "complete".
  - Test: validator passes valid output unchanged.
- [ ] **Subtask 3.7:** Regression check: `cd packages/agents && /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest tests/ -q --tb=no` — passing-test count must be ≥ the baseline from Subtask 3.0.

**Done definition:** Output schema includes 4-state enum; validator runs after Judge; regenerate-on-fail policy active; Critic emits the decision contract.

**Verification:**
```bash
# 1. Validator and Critic schemas present.
python -c "from catalyst_agents.state import OutputStatus, CriticDecision; assert {'SUFFICIENT','PARTIAL','INSUFFICIENT','SYSTEM_ERROR'} == {s.name for s in OutputStatus}"

# 2. Validator unit tests pass.
cd packages/agents && /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest tests/test_validator.py -v
# Expected: at least 3 tests pass.

# 3. No regression in existing tests.
cd packages/agents && /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest tests/ -q --tb=short
# Expected: same pass count as Day 1 baseline (record baseline before T-03).
```

### T-04 DecisionRouter (Day 4 PM)

**Depends on:** T-03.
**Estimated:** 4 hours.

- [ ] **Subtask 4.1:** Create `packages/agents/catalyst_agents/nodes/decision_router.py`. Pure-function dispatcher (no LLM). Input: `CriticDecision`, current expansion count, error flag. Output: one of `judge | expand_macro | refuse | system_error`. (Note: `expand_related` is **not** an emitted edge in P0 — see 4.2.)
- [ ] **Subtask 4.2:** Decision rules (P0 narrowing of execution-spec §6 per strategy spec W-02 and W-04):
  - `system_error` flag from upstream → emit `system_error`.
  - `next_action == "proceed"` → emit `judge`.
  - `next_action == "expand_macro"` AND expansion budget remains (`expansions_used < max_expansions`, where `max_expansions = 2`) → emit `expand_macro`.
  - `next_action == "expand_macro"` AND expansion budget exhausted → emit `refuse` with `reason=expansions_exhausted`.
  - `next_action == "expand_related"` → emit `refuse` with `reason=layer3_not_implemented` (per strategy spec §3.2, W-02). The router never emits an `expand_related` edge in P0.
  - `next_action == "refuse"` → emit `refuse` with `reason=critic_refused`.
  - **Budget tracking is record-only in P0** (per strategy spec W-04). The router does not read or react to cost/latency/token totals; it does not emit a budget-overflow branch. Cost reporting happens passively via the trace writer (T-07) and aggregated reports (T-11).
- [ ] **Subtask 4.3:** Wire into `graph.py` between Critic and the Judge/Expand/Refuse fan-out.
- [ ] **Subtask 4.4:** Write unit tests in `packages/agents/tests/test_decision_router.py` covering:
  - `next_action="proceed"` → `judge`.
  - `next_action="expand_macro"` with budget → `expand_macro`.
  - `next_action="expand_macro"` exhausted → `refuse` with `reason=expansions_exhausted`.
  - `next_action="expand_related"` → `refuse` with `reason=layer3_not_implemented`.
  - `next_action="refuse"` → `refuse` with `reason=critic_refused`.
  - `system_error` flag → `system_error`.

**Done definition:** Router is deterministic; every `CriticDecision` maps to exactly one outgoing edge; Layer 3 short-circuit visible in trace; **no budget-overflow branch exists** (W-04 compliance).

**Verification:**
```bash
cd packages/agents && /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest tests/test_decision_router.py -v
# Expected: ≥6 tests pass.

# W-04 compliance grep — router source must not reference budget enforcement.
grep -E "(budget_overflow|partial_budget_flag|cost_cap|latency_cap)" \
  packages/agents/catalyst_agents/nodes/decision_router.py
# Expected: zero hits.
```

### T-05 direct_llm Baseline (Day 5 AM)

**Depends on:** T-03 (output schema must be stable).
**Estimated:** 4 hours.

- [ ] **Subtask 5.1:** Create `packages/eval/catalyst_eval/harness/baselines/direct_llm.py` and `packages/eval/catalyst_eval/harness/baselines/__init__.py`. Function `run_direct_llm(case: dict, model_id: str, db_path: Path) -> dict` returning the **same output schema** as `mcj_full` (so reports are diffable). The `db_path` parameter resolves which corpus the evidence-id check runs against — caller's responsibility, not hardcoded.
- [ ] **Subtask 5.2:** Pipeline shape: load case → single LLM call with the question + ticker + date window in the prompt → parse to schema → run validator's evidence-id existence check against the DB at `db_path`. **In Day 5 development, callers pass `data/catalyst_dev.db`. After T-10 freeze on Day 8, callers pass `data/catalyst_eval_frozen.db`. The baseline does not assume or hardcode either filename.**
- [ ] **Subtask 5.3:** Record `model_id`, `cost_usd`, `latency_ms`, `tokens_in`, `tokens_out` per case (R-5).
- [ ] **Subtask 5.4:** Create `packages/eval/tests/test_direct_llm_baseline.py`. Smoke test: invoke `run_direct_llm` on 1 case fixture against `data/catalyst_dev.db`; assert output JSON validates against schema; assert `model_id`, `cost_usd`, `latency_ms`, `tokens_in`, `tokens_out` populated.

**Done definition:** Baseline runnable on a single case against any provided DB path; produces schema-conformant output with cost/latency/tokens; no hardcoded reference to the frozen DB.

**Verification:**
```bash
cd packages/eval && /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest tests/test_direct_llm_baseline.py -v
# Expected: smoke-test passes for 1 case.

# W-04 / B-3 compliance grep — baseline source must not hardcode the frozen DB.
grep -E "catalyst_eval_frozen\.db" packages/eval/catalyst_eval/harness/baselines/direct_llm.py
# Expected: zero hits (DB path is a parameter, not a constant).
```

### T-06 Retrieval Policy (Layer 1 + Layer 2; Layer 3 stub) (Day 5 PM)

**Depends on:** T-04.
**Estimated:** 6 hours.

- [ ] **Subtask 6.0:** Identify or create the test-fixture corpus. The retrieval tests in 6.5 require an in-memory or temp-file SQLite + LanceDB pair seeded with: (a) ≥3 ticker-tagged news rows for one test ticker, (b) ≥3 macro-tagged rows. Fixture lives at `packages/agents/tests/fixtures/retrieval_fixture.py` and is referenced by the tests via pytest fixture. If a comparable fixture already exists in midterm tests, reuse it; otherwise create the minimum new fixture file.
- [ ] **Subtask 6.1:** Create `packages/agents/catalyst_agents/retrieval/policy.py`. Function `retrieve(query, layer: Layer, metadata: RetrievalMetadata) -> list[Evidence]`.
  - `Layer.DIRECT` (Layer 1): ticker filter + date window against existing LanceDB hybrid retrieval.
  - `Layer.MACRO` (Layer 2): drop ticker filter; query macro/policy/geo sources.
  - `Layer.RELATED` (Layer 3): function body raises `NotImplementedError("layer3_not_implemented")`.
- [ ] **Subtask 6.2:** Add `RetrievalMetadata` dataclass: `layers_attempted`, `expansion_reasons`, `stop_reason`, `hit_counts_per_layer`, `geo_corpus_tier` (1 or 2 per W-09).
- [ ] **Subtask 6.3:** Wire policy into the Miner / RetrievalPolicy node in `graph.py` so DecisionRouter's `expand_macro` invokes Layer 2.
- [ ] **Subtask 6.4:** Enforce bounds: `max_layers=2` effective in P0 (Layer 3 short-circuit per T-04); `max_expansions=2`.
- [ ] **Subtask 6.5:** Unit tests in `packages/agents/tests/test_retrieval_policy.py`:
  - Layer 1 returns ticker-filtered hits.
  - Layer 2 returns broader macro hits when invoked.
  - `Layer.RELATED` raises `NotImplementedError`.
  - Bounded expansion stops after 2 expansions.

**Done definition:** Layer 1 and Layer 2 produce real hits against the test DB; Layer 3 short-circuits; metadata records every attempt.

**Verification:**
```bash
cd packages/agents && /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest tests/test_retrieval_policy.py -v
# Expected: ≥4 tests pass.
```

### T-07 Trace Persistence (Day 6)

**Depends on:** T-04, T-06.
**Estimated:** 6 hours.

- [ ] **Subtask 7.1:** Create `trace/schema.py` with SQLite DDL for `agent_runs` (run-level) and `trace_events` (node-level). Required fields per execution-spec §10: `trace_id`, `run_id`, `node`, `started_at`, `ended_at`, `latency_ms`, `model_id`, `input_tokens`, `output_tokens`, `cost_usd`, `decision`, `error_type`, `error_message`, `status_before`, `status_after`.
- [ ] **Subtask 7.2:** Create `trace/writer.py`. Single `TraceWriter` class with context-manager API (`with TraceWriter(run_id=...) as t: t.event(...)`). Writes to the DB pointed at by `CATALYST_DB_PATH`.
- [ ] **Subtask 7.3:** Instrument every node transition in `graph.py` with `t.event(...)`. Validator failures, regenerate attempts, DecisionRouter decisions, Layer-3 short-circuits all produce events.
- [ ] **Subtask 7.4:** Create `trace/exporter.py`. CLI `python -m catalyst_agents.trace.exporter --run-id <run> --out data/traces/<run_id>.json` emits a single JSON file with all events for the run.
- [ ] **Subtask 7.5:** Unit tests in `packages/agents/tests/test_trace.py`:
  - One run produces ≥6 events (one per node).
  - Exporter JSON round-trips through `json.loads` and contains all required fields.
  - Query by `error_type` returns expected rows.

**Done definition:** Every run leaves a trace record; exporter produces a self-contained JSON; required fields all populated.

**Verification:**
```bash
cd packages/agents && /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest tests/test_trace.py -v
# Expected: ≥3 tests pass.

# Smoke: run a case, then query the latest run_id from the trace DB and export.
LATEST_RUN=$(/Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -c "
import sqlite3, os
db = os.environ.get('CATALYST_DB_PATH','data/catalyst_dev.db')
conn = sqlite3.connect(db)
row = conn.execute('SELECT run_id FROM agent_runs ORDER BY started_at DESC LIMIT 1').fetchone()
print(row[0] if row else '')")
test -n "$LATEST_RUN" || { echo "FAIL: no agent_runs row"; exit 1; }
python -m catalyst_agents.trace.exporter --run-id "$LATEST_RUN" --out /tmp/trace.json
python -c "
import json
d = json.load(open('/tmp/trace.json'))
assert 'trace_id' in d, 'missing trace_id'
assert len(d.get('events',[])) >= 6, f'too few events: {len(d.get(\"events\",[]))}'"
```

### T-08 Failure Taxonomy + 2 Regression Tests (Day 7)

**Depends on:** T-03, T-07.
**Estimated:** 5 hours.

- [ ] **Subtask 8.1:** Create `docs/testing/failure-taxonomy.md` documenting 5 classes from execution-spec §9: `retrieval_failure`, `model_failure`, `consistency_failure`, `budget_failure`, `provider_failure`. **Each class is a Markdown H3 heading of the exact form `### <class_name>`** (lowercase, with underscores, matching the strings used in trace `error_type` fields). Each section's body lists: definition, detection signal, deterministic action, regression-test reference.
- [ ] **Subtask 8.2:** Create `packages/agents/tests/test_failure_taxonomy.py`. Add a header docstring tag per test: `# class: <failure_class>`.
- [ ] **Subtask 8.3:** Test 1 (`# class: consistency_failure`): inject a Critic output with non-existent `evidence_id`; assert validator triggers regenerate; assert second failure downgrades to `PARTIAL`; assert trace records both attempts.
- [ ] **Subtask 8.4:** Test 2 (`# class: model_failure`): inject a model timeout via mock; assert DecisionRouter returns `system_error`; assert final status is `SYSTEM_ERROR`; assert trace records the error_type.
- [ ] **Subtask 8.5:** Stage taxonomy doc + tests; do not commit.

**Done definition:** Taxonomy doc complete with 5 classes; 2 regression tests pass with class tags visible.

**Verification:**
```bash
# 1. Taxonomy doc has all 5 class entries with the exact required headings.
for c in retrieval_failure model_failure consistency_failure budget_failure provider_failure; do
  grep -qE "^### ${c}\b" docs/testing/failure-taxonomy.md || { echo "MISSING H3: ### $c"; exit 1; }
done
echo "All 5 class headings present."

# 2. Tests pass.
cd packages/agents && /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest tests/test_failure_taxonomy.py -v
# Expected: ≥2 tests pass.

# 3. Class tags present.
grep -c "# class:" packages/agents/tests/test_failure_taxonomy.py
# Expected: ≥2.
```

### T-09 Pre-Eval Data Preflight (Day 8 AM)

**Depends on:** T-02 (frozen DB target exists). Independent of control-plane tasks.
**Estimated:** 4 hours.

- [ ] **Subtask 9.1:** Create `packages/eval/scripts/preflight.py`. Reads from a candidate frozen DB path (passed as `--db` CLI arg); checks data class counts per strategy spec §8.5.
- [ ] **Subtask 9.2:** Class checks:
  - Macro / monetary: ≥30 docs in case date windows (queries clean_assets table where source matches FRED ingestion patterns).
  - Direct evidence: ≥5 docs per case ticker (queries clean_assets where ticker matches case ticker list).
  - Geopolitical Tier 1 (GDELT): ≥50 docs in case date windows.
  - If Tier 1 fails: fallback Tier 2 (Polygon news tagged macro/policy/tariff) ≥20 docs.
- [ ] **Subtask 9.3:** Output: `data/eval_reports/preflight_<YYYYMMDD_HHMMSS>.json` with these fields:
  - `pass`: bool
  - `db_sha256`: SHA-256 of the candidate DB at check time (for §4.1 Tier-A pinning)
  - `row_counts`: dict of `{table_name: count}` for `raw_assets`, `clean_assets`
  - `class_counts`: dict per class with thresholds + actuals
  - `geo_corpus_tier`: 1 or 2 (sidecar field, **not a DB schema mutation** — lives only in this JSON and propagates into eval-report headers; no T-02 schema change required)
  - `timestamp`: UTC ISO8601
- [ ] **Subtask 9.4:** Exit code 0 on pass (Tier 1 OR Tier 2 satisfied AND macro AND direct all pass); exit code 1 on any class fail.
- [ ] **Subtask 9.5:** Day 8 morning: run preflight against `catalyst_eval_frozen.db` candidate. If exit 1, identify which class(es) failed and run the corresponding ingestion scripts:
  - Macro fail → `python packages/data-core/scripts/ingest_fred.py --series FEDFUNDS,CPIAUCSL,UNRATE --start <case-window-start> --end <case-window-end> --db data/catalyst_eval_frozen.db`
  - Direct evidence fail (per ticker) → `python packages/data-core/scripts/ingest_polygon_news.py --tickers <missing-tickers-comma-sep> --start <window-start> --end <window-end> --db data/catalyst_eval_frozen.db`
  - GDELT (Tier 1) fail → `python packages/data-core/scripts/ingest_gdelt.py --start <window-start> --end <window-end> --db data/catalyst_eval_frozen.db` if such a script exists; otherwise accept Tier 2 fallback rather than authoring new ingestion in the defense window (Tier 2 is the explicit waiver path per W-09).
  - **Constraint:** if any of the referenced ingest scripts does not exist in `packages/data-core/scripts/`, the failure is recorded and the corresponding class either uses Tier 2 fallback (geo) or fails the preflight (macro / direct). No new ingestion scripts are written inside the P0 sprint — that is P1 / P2 territory.
  - Re-run preflight until exit 0 OR until Day 8 EOD; if still failing at EOD, this is a data prerequisite failure that blocks T-10 freeze and triggers the Day 10 Gate red path early.
- [ ] **Subtask 9.6:** Final preflight JSON (the one with `pass=true`) is the canonical artifact; its `db_sha256` and `row_counts` propagate to the T-10 frozen-run header (§4.1 Tier-A pinning).

**Done definition:** Preflight exit 0 against frozen DB candidate; report committed; `geo_corpus_tier` chosen; DB fingerprint recorded.

**Verification:**
```bash
python packages/eval/scripts/preflight.py --db data/catalyst_eval_frozen.db
echo "Exit: $?"
# Expected: 0.

ls data/eval_reports/preflight_*.json | tail -1 | xargs cat | python -c "import json,sys; d=json.load(sys.stdin); assert d['pass']==True and d['geo_corpus_tier'] in (1,2) and 'db_sha256' in d"
```

### T-10 Eval Freeze + First Full Run + Threshold Calibration (Day 8 PM)

**Depends on:** T-05, T-06, T-07, T-09.
**Estimated:** 6 hours.

- [ ] **Subtask 10.1:** Finalize 10-case set in `packages/eval/golden_set/v1_2.jsonl`: 5 sufficient + 2 partial + 3 should-refuse (per Open Decision OD-3). Add `expected_status` and `should_refuse` fields per row (R-2).
- [ ] **Subtask 10.2:** **Mark eval DB frozen.** Compute final SHA-256, record in metadata table or sidecar file. Add a comment block at top of golden_set file noting "Frozen Day 8 — protocol immutable per strategy spec §6.1".
- [ ] **Subtask 10.3:** Calibrate Critic thresholds (`K_sufficient`, `K_partial`, `M_threshold`). Initial values from OD-2; tune by inspecting Critic output on the 10-case set; lock final values in `state.py` constants.
- [ ] **Subtask 10.4:** Run `direct_llm` on all 10 cases. Run `mcj_full` on all 10 cases. Both runs write outputs + traces.
- [ ] **Subtask 10.5:** Capture freeze inputs in run header (Tier-A pinning per §4.1): `model_id` per role, `provider_version` (when available), `db_sha256`, table row counts, random seed, code git SHA.
- [ ] **Subtask 10.6:** Stage golden_set changes + run outputs + trace files.

**Done definition:** Two runs complete; threshold calibration finalized in code; frozen DB SHA recorded; Tier-A pinning fields populated in run headers.

**Verification:**
```bash
# 1. Two runs present.
ls data/eval_reports/ | grep -E "(direct_llm|mcj_full).*\.json$" | wc -l
# Expected: ≥2.

# 2. Trace files present — one per (config × case) execution, per strategy spec §10
#    ("Single run-level trace_id" = one trace_id per case execution).
TRACE_COUNT=$(ls data/traces/*.json 2>/dev/null | grep -v rerun_ | wc -l | tr -d ' ')
test "$TRACE_COUNT" -ge 20 || { echo "FAIL: only $TRACE_COUNT trace files (expect ≥20 = 2 configs × 10 cases)"; exit 1; }

# 3. Tier-A pinning fields present.
python -c "
import json,glob
for f in glob.glob('data/eval_reports/*_direct_llm.json') + glob.glob('data/eval_reports/*_mcj_full.json'):
    d = json.load(open(f))
    h = d.get('header', {})
    for k in ('model_id_per_role','db_sha256','code_git_sha','random_seed'):
        assert k in h, f'{f} missing {k}'
print('OK')"
```

### T-11 Eval Report + Deck Draft (Day 9)

**Depends on:** T-10.
**Estimated:** 8 hours.

- [ ] **Subtask 11.1:** Implement gate enforcement inside `packages/eval/catalyst_eval/harness/runner.py`. If the file already exists from midterm, extend it; if absent, create it at this exact path. The gate-enforcement function reads run JSONs and computes:
  - `evidence_validity` per case + aggregate.
  - `schema_validity` (1.00 or fail).
  - `trace_completeness` (count traces vs expected).
  - `should_refuse_hit_rate` over the 3 should-refuse cases.
  - `attribution_f1`, `category_accuracy`, `grounding_rate`, `temporal_precision` (existing midterm metrics).
  - Cost/latency/tokens per config.
- [ ] **Subtask 11.2:** Generate `data/eval_reports/<frozen_timestamp>_comparison.md` (R-5). Tables: gate values per config, quality metrics per config, cost/latency table.
- [ ] **Subtask 11.3:** Generate `data/eval_reports/<frozen_timestamp>_comparison.json` with the same data, machine-readable. The `header` block contains all Tier-A pinning fields (per §4.1) plus `case_distribution` (`{sufficient: 5, partial: 2, should_refuse: 3}`) for downstream immutability checks.
- [ ] **Subtask 11.4:** Draft the deck file at the path chosen by OD-1 (default: `docs/defense/deck.md`) covering the 8 slides + appendix from strategy spec §5. Each slide is a top-level Markdown H2 of the exact form `## Slide N: <Title>` so a downstream verification can count slides without ambiguity.
- [ ] **Subtask 11.5:** Stage all artifacts; prompt user for commit authorization at task close.
- [ ] **Subtask 11.6:** Create `packages/eval/scripts/check_p0_gate.py` — the executable used by T-12 Day 10 Gate. Inputs: a comparison-report JSON path. Behavior: parses the report's `gates` block, asserts each threshold per strategy spec §4 (evidence_validity ≥ 0.95; schema_validity = 1.00; trace_completeness = 1.00; should_refuse_hit_rate ≥ 2/3; cost_latency_reported across all configs). Exit 0 on all pass; exit 1 on any gate fail; exit 2 on script error (missing file, malformed JSON). Prints a human-readable summary table of each gate's value vs threshold.

**Done definition:** Comparison report + deck draft committed paths; gate values computed and visible; `check_p0_gate.py` exists and is executable on the comparison report.

**Verification:**
```bash
# 1. Report files exist.
ls data/eval_reports/*_comparison.md data/eval_reports/*_comparison.json

# 2. Gate values present in JSON.
python -c "
import json,glob
f = sorted(glob.glob('data/eval_reports/*_comparison.json'))[-1]
d = json.load(open(f))
for g in ('evidence_validity','schema_validity','trace_completeness','should_refuse_hit_rate'):
    assert g in d['gates'], g
print('Gate keys OK:', list(d['gates'].keys()))"

# 3. Deck draft has at least 8 explicitly numbered slide headings.
DECK=$(ls docs/defense/deck.md 2>/dev/null || ls docs/defense/* | head -1)
test -f "$DECK" || { echo "FAIL: deck file missing"; exit 1; }
SLIDES=$(grep -cE "^## Slide [0-9]+:" "$DECK")
test "$SLIDES" -ge 8 || { echo "FAIL: only $SLIDES numbered slides"; exit 1; }
echo "Deck has $SLIDES numbered slides."

# 4. Gate script exists and is executable on the comparison report.
test -f packages/eval/scripts/check_p0_gate.py || { echo "FAIL: gate script missing"; exit 1; }
python packages/eval/scripts/check_p0_gate.py "$(ls -t data/eval_reports/*_comparison.json | head -1)"
echo "Gate script exit: $?"
# Expected: prints gate summary; exit 0 (GREEN), 1 (RED), or 2 (script error).
```

### T-12 Day 10 Gate (Day 10)

**Depends on:** T-11.
**Estimated:** 2 hours (decision + record).

See §5 Day 10 Gate Decision Checklist below for the full procedure.

- [ ] **Subtask 12.1:** Run `packages/eval/scripts/check_p0_gate.py` against the latest comparison JSON. Script exits non-zero if any gate fails.
- [ ] **Subtask 12.2:** Record decision in `docs/testing/audit_<date>.md` header: GREEN (proceed P1) / RED-RECOVERABLE (freeze P1, retry Day 11) / RED-DISCLOSE (defense reports failure honestly).
- [ ] **Subtask 12.3:** If GREEN: optionally schedule G5 (eval extraction) if buffer permits. Otherwise stop P1 work entirely.
- [ ] **Subtask 12.4:** **Sample-protocol immutability invariant check:** confirm no edits to `golden_set/v1_2.jsonl` since T-10 freeze; confirm `db_sha256` in this run matches T-10 freeze value.

**Done definition:** Decision recorded; immutability invariant confirmed; subsequent task list updated based on decision.

**Verification:**
```bash
# Run gate script.
python packages/eval/scripts/check_p0_gate.py data/eval_reports/$(ls -t data/eval_reports/*_comparison.json | head -1 | xargs basename)
# Expected: exit 0 if GREEN; exit 1 if RED.

# Confirm sample-protocol immutability.
git log -- packages/eval/golden_set/v1_2.jsonl --since="$(date -v-3d +%Y-%m-%d)" --pretty=oneline | wc -l
# Expected: 1 (the T-10 freeze commit/stage). More than 1 = protocol violation.

# Confirm DB fingerprint match.
python -c "
import hashlib,json,glob
report = json.load(open(sorted(glob.glob('data/eval_reports/*_comparison.json'))[-1]))
freeze_sha = report['header']['db_sha256']
current_sha = hashlib.sha256(open('data/catalyst_eval_frozen.db','rb').read()).hexdigest()
assert freeze_sha == current_sha, f'DB drifted: frozen={freeze_sha[:8]} current={current_sha[:8]}'
print('Fingerprint match OK')"
```

### T-13 Verification Audit (Day 11 → Day 12 AM)

**Depends on:** T-12.
**Estimated:** 12 hours over 1.5 days.

- [ ] **Subtask 13.1:** Open `docs/testing/audit_<date>.md`. For each task T-01 through T-11, re-run the verification commands listed above. Record outcome (pass/fail/notes) per task.
- [ ] **Subtask 13.2:** Re-verify Tier-A reproducibility. Rerun `mcj_full` on the 10-case set with output **redirected to a separate path**: `data/eval_reports/rerun_<YYYYMMDD_HHMMSS>_mcj_full.{md,json}`, with traces under `data/traces/rerun_<run_id>.json`. **The frozen artifact paths (`data/eval_reports/<frozen_timestamp>_*` and `data/traces/<frozen_run_id>.json`) are read-only during this step** — any tool overwriting them is a violation. Diff the rerun's per-case `status`, `evidence_ids`, `next_action`, `magnitude_coverage`, and aggregate quality metrics against the frozen run. Tier-B fields (cost/latency/tokens) variance is logged but not gated.
- [ ] **Subtask 13.3:** **No new features.** Bug fixes that preserve schema are allowed. Any schema change is a freeze violation per strategy spec §8.1. Schema is "frozen" in the sense of §6.bis below.
- [ ] **Subtask 13.4:** Exit criterion: every T-01 through T-11 row in the audit log marked PASS, or marked FAIL-DISCLOSED with a deck callout.

**Done definition:** Audit log committed; every task verified; rerun reproducibility shows Tier-A exact equality; frozen artifacts unmodified.

**Verification:**
```bash
# 1. Audit log has at least one row per audited task.
AUDIT=$(ls docs/testing/audit_*.md | sort | tail -1)
test -f "$AUDIT" || { echo "FAIL: no audit log"; exit 1; }
ROWS=$(grep -c "^| T-" "$AUDIT")
test "$ROWS" -ge 11 || { echo "FAIL: only $ROWS audit rows"; exit 1; }

# 2. Rerun artifacts exist at the rerun-prefixed paths.
ls data/eval_reports/rerun_*_mcj_full.json | head -1 || { echo "FAIL: no rerun report"; exit 1; }

# 3. Frozen artifacts are byte-identical to their state at T-10 freeze.
python -c "
import hashlib, json, glob
report = json.load(open(sorted(glob.glob('data/eval_reports/*_comparison.json'))[-1]))
freeze_sha = report['header']['db_sha256']
current_sha = hashlib.sha256(open('data/catalyst_eval_frozen.db','rb').read()).hexdigest()
assert freeze_sha == current_sha, f'FROZEN DB MUTATED: {freeze_sha[:8]} -> {current_sha[:8]}'"

# 4. Tier-A diff.
FROZEN=$(ls data/eval_reports/*_mcj_full.json | grep -v rerun_ | sort | tail -1)
RERUN=$(ls data/eval_reports/rerun_*_mcj_full.json | sort | tail -1)
diff <(python -c "import json;d=json.load(open('$FROZEN'));print('\n'.join(sorted(f\"{c['case_id']}|{c['status']}|{sorted(c['evidence_ids'])}|{c['next_action']}|{round(c['magnitude_coverage'],2)}\" for c in d['cases'])))") \
     <(python -c "import json;d=json.load(open('$RERUN'));print('\n'.join(sorted(f\"{c['case_id']}|{c['status']}|{sorted(c['evidence_ids'])}|{c['next_action']}|{round(c['magnitude_coverage'],2)}\" for c in d['cases'])))")
# Expected: empty diff.
```

### T-14 Tech Dry-Run (Day 12 PM)

**Depends on:** T-13.
**Estimated:** 4 hours.

- [ ] **Subtask 14.1:** Author `notebooks/demo.ipynb` reading from `catalyst_demo.db`. Cells, in order: (1) overview header citing strategy spec headline; (2) load 1 should-refuse case + 1 sufficient case from frozen golden set; (3) for each case, run **both** `direct_llm` and `mcj_full` configs side-by-side reading from frozen artifacts (no live API); (4) display retrieval metadata, Critic decision, Validator output, final status; (5) display the cost/latency comparison table from the frozen comparison report; (6) closing cell showing the gate-summary table from the comparison JSON. **No live LLM calls are made by the notebook** — it only reads frozen JSON artifacts.
- [ ] **Subtask 14.2:** Populate `catalyst_demo.db` from frozen eval artifacts (do NOT mutate `catalyst_eval_frozen.db`).
- [ ] **Subtask 14.3:** Execute notebook end-to-end; save as `notebooks/demo_executed.ipynb` with outputs.
- [ ] **Subtask 14.4:** Defense-day procedure rehearsal: open notebook, walk slides, point at frozen artifacts. Confirm no live API calls required.

**Done definition:** Executed notebook committed; offline-runnable; references only frozen artifacts.

**Verification:**
```bash
# Notebook executed.
jupyter nbconvert --to notebook --execute notebooks/demo.ipynb --output /tmp/demo_check.ipynb
# Expected: no errors.

# No live API in cell content.
grep -E "(api\.openai|api\.anthropic|claude\.ai)" notebooks/demo.ipynb
# Expected: empty (offline-only).

# Demo DB references in notebook are catalyst_demo.db.
grep -c "catalyst_demo.db" notebooks/demo.ipynb
# Expected: ≥1.
grep -c "catalyst_eval_frozen.db" notebooks/demo.ipynb
# Expected: 0 (demo never touches frozen).
```

### T-15 Code Freeze + Deck Polish (Day 13)

**Depends on:** T-14.
**Estimated:** 6 hours.

- [ ] **Subtask 15.1:** Finalize deck content. Replace any explicit `<placeholder>` markers in the deck draft with real screenshots/values pulled from frozen artifacts. (T-11's deck draft inserts `<placeholder>` tokens for screenshots so this step has something concrete to scan for.)
- [ ] **Subtask 15.2:** **Run the §6.bis source-freeze invariant check** before tagging — if `packages/`, `data/eval_reports/<frozen>*`, `data/traces/<frozen>*`, or any DB file has been modified since the T-10 commit, abort and investigate. The invariant must hold to make the tag meaningful.
- [ ] **Subtask 15.3:** Stage all final deck changes.
- [ ] **Subtask 15.4:** **Request user authorization** to create the freeze tag (per CLAUDE.md, Claude does not commit/tag without explicit user OK).
- [ ] **Subtask 15.5 (user-authorized only):** Create annotated tag at HEAD: `git tag -a defense-freeze-YYYY-MM-DD -m "P0 defense freeze"`. The tag commit's SHA must equal the T-10 commit's SHA (because §6.bis guarantees no source changes between T-10 and now). If the tag commit SHA differs from the eval-frozen `code_git_sha`, that is a §6.bis violation — abort.

**Done definition:** Annotated tag exists at the same commit recorded in the eval frozen run header; §6.bis invariant holds; subsequent commits restricted to the freeze whitelist (§7).

**Verification:**
```bash
# 1. Tag exists and is annotated.
TAG=$(git tag --list 'defense-freeze-*' | sort | tail -1)
test -n "$TAG" || { echo "FAIL: no freeze tag"; exit 1; }
TYPE=$(git for-each-ref "refs/tags/$TAG" --format='%(objecttype)')
test "$TYPE" = "tag" || { echo "FAIL: $TAG is $TYPE, not annotated"; exit 1; }

# 2. Tag SHA equals the eval-frozen code SHA (no source drift since T-10).
python -c "
import json, subprocess, glob, os
report = json.load(open(sorted(glob.glob('data/eval_reports/*_comparison.json'))[-1]))
eval_sha = report['header']['code_git_sha']
tag = '${TAG}'
tag_sha = subprocess.check_output(['git','rev-parse', f'{tag}^{{commit}}']).decode().strip()
assert eval_sha == tag_sha, f'§6.bis violation: eval={eval_sha[:8]} tag={tag_sha[:8]}'
print(f'SHA match OK: {eval_sha[:8]}')"
```

### T-16 Defense Rehearsal (Day 14)

**Depends on:** T-15.
**Estimated:** 4 hours.

- [ ] **Subtask 16.1:** Full talk rehearsal at target length (per OD-4).
- [ ] **Subtask 16.2:** Run notebook live as a sanity check (no edits — read-only walkthrough).
- [ ] **Subtask 16.3:** Buffer for last-minute deck text fixes (no code).

**Done definition:** Two end-to-end rehearsals complete; timing within target.

**Verification:** Self-attestation; no machine check.

---

## 3. Day-Level Schedule + Dependency Graph

### Calendar (Day 1 = first execution day, e.g. 2026-04-30)

| Day | AM | PM |
|---|---|---|
| 1 | T-01 doc sweep (full day) | T-01 continues |
| 2 | T-02 DB triple split | T-03 validator + 4-state + Critic schema (start) |
| 3 | T-03 continues | T-03 continues |
| 4 | T-03 finish | T-04 DecisionRouter |
| 5 | T-05 direct_llm baseline | T-06 retrieval Layer 1+2 |
| 6 | T-07 trace persistence (full day) | T-07 continues |
| 7 | T-08 failure taxonomy + 2 regression tests | T-08 continues |
| 8 | **T-09 preflight + ingest if needed** | T-10 eval freeze + first run + calibration |
| 9 | T-11 report + deck draft (full day) | T-11 continues |
| 10 | **T-12 Day 10 Gate (decision before noon)** | If GREEN: P1 work begins (G5 only). If RED: T-13 starts early. |
| 11 | T-13 audit (full day) | T-13 continues |
| 12 | T-13 audit AM | T-14 tech dry-run |
| 13 | T-15 code freeze + deck polish (full day) | T-15 continues |
| 14 | T-16 defense rehearsal | Buffer |

### Dependency Graph (text form)

```
T-01 ──┐
       ├─► (Day 1-2 parallel)
T-02 ──┘
        │
        ▼
       T-03 ──► T-04 ──► T-06 ──┐
                                ├─► T-07 ──► T-08
                T-05 (parallel) ┘
                                            │
                                            ▼
                                T-09 ──► T-10 ──► T-11 ──► T-12 ──► T-13 ──► T-14 ──► T-15 ──► T-16
                              (Day 8 AM)  (Day 8 PM)
```

### Critical Path

**T-01 / T-02 → T-03 → T-04 → T-06 → T-07 → T-08 → T-10 → T-11 → T-12 → T-13 → T-14 → T-15 → T-16**

The hottest stretch is **Day 2 PM through Day 5 PM** (T-03 → T-04 → T-06): three sequential control-plane tasks. Any slip here cascades into the eval window. **T-05 (baseline) is the only parallelizable lane in that stretch** — exploit it.

Slack:
- T-01 / T-02 finish by Day 2 EOD with ~3h buffer.
- T-09 (preflight) can run in parallel with late T-08 work because it only needs T-02 data layer.
- Day 11 PM is unallocated — natural buffer or T-13 spillover.

---

## 4. Risk Register (trigger → action → decision-maker)

| # | Risk | Trigger | Action | Decision-maker |
|---|---|---|---|---|
| R-A | T-03 validator regresses midterm tests | `pytest packages/agents/tests/` passing-test count drops below the Subtask 3.0 baseline (committed or staged state, both checked) | Pause T-04. Day 4 EOD deadline to fix. If unfixable, ship validator without auto-regenerate (single-pass only); record as runtime-spec deviation. | Yiannis |
| R-B | direct_llm baseline cost overrun (>$50 for 10 cases) | T-05 single-case smoke shows projected total >$50 | Switch model tier (Sonnet → Haiku); record OD-6 update. | Yiannis |
| R-C | Layer 2 macro corpus too thin | T-09 preflight fails Tier 1 | Activate W-09 Tier 2 fallback; record `geo_corpus_tier=2` in DB metadata + run header. | Yiannis (executive); GPT (review optional) |
| R-D | should-refuse cases ambiguous | T-10 first run shows boundary disagreement on a should-refuse case | Pause; pick a clearer alternative case **before** T-10 freeze. After Day 8 freeze, no case substitution allowed. | Yiannis ahead of Day 8 |
| R-E | Day 10 Gate fails | T-12 `check_p0_gate.py` exits non-zero | Day 10 PM: freeze all P1 work; Day 11 EOD reassess. If still red, defense ships with explicit failure disclosure (deck P7 reports failed gate honestly). | Yiannis |
| R-F | Day 13 freeze violation | T-15 verification finds non-whitelist commits between Day 13 EOD and Day 14 | Revert violating commit; record violation in audit log. | Yiannis |
| R-G | Tier-A reproducibility divergence on rerun | T-13 diff between frozen run and rerun shows non-empty | Inspect pinning — likely `model_id` drift or DB fingerprint mismatch. Re-pin and rerun; if still divergent, isolate the offending field and document as known nondeterminism in the report. | Yiannis |
| R-H | Time slip on critical path (T-03 → T-06) | Any task end > planned + 0.5 day | Apply Day 10 Gate decision tree early: cut P1 first; never cut sample protocol. | Yiannis |
| R-I | Pattern adoption (R-1 to R-5) creates scope drift | Any reference-pattern subtask grows beyond its file budget | Abandon the borrow; ship the task without the pattern. P0 closure overrides pattern fidelity. | Yiannis |
| R-J | LangGraph version incompatibility surfaces during graph rewiring | T-04 / T-06 fails with import or state-routing errors | Pin LangGraph to the midterm-known-good version; do not upgrade in the defense window. | Yiannis |

---

## 5. Day 10 Gate Decision Checklist (Executable)

Run sequentially on Day 10 AM. Stop and apply remediation on the first failure.

```bash
# 0. Workspace state. (CLAUDE.md prohibits `-uall`.)
git status  # Expected: only T-11 staged artifacts uncommitted.

# 1. Latest comparison report exists.
LATEST=$(ls -t data/eval_reports/*_comparison.json 2>/dev/null | head -1)
test -n "$LATEST" || { echo "FAIL: no comparison report"; exit 1; }
echo "Using report: $LATEST"

# 2. Sample-protocol immutability.
python -c "
import json, hashlib
report = json.load(open('$LATEST'))
expected_distribution = {'sufficient': 5, 'partial': 2, 'should_refuse': 3}
actual = report['header']['case_distribution']
assert actual == expected_distribution, f'PROTOCOL DRIFT: {actual}'
print('Distribution OK:', actual)"

# 3. DB fingerprint match.
python -c "
import json, hashlib
report = json.load(open('$LATEST'))
expected_sha = report['header']['db_sha256']
actual_sha = hashlib.sha256(open('data/catalyst_eval_frozen.db','rb').read()).hexdigest()
assert expected_sha == actual_sha, f'DB DRIFT: report={expected_sha[:8]} actual={actual_sha[:8]}'
print('DB fingerprint OK')"

# 4. Gate enforcement script.
python packages/eval/scripts/check_p0_gate.py "$LATEST"
GATE_EXIT=$?

# Decision tree.
if [ $GATE_EXIT -eq 0 ]; then
  echo "DECISION: GREEN — proceed with G5 if buffer; otherwise hold P1."
elif [ $GATE_EXIT -eq 1 ]; then
  echo "DECISION: RED-RECOVERABLE — freeze P1 immediately; revisit Day 11 EOD."
else
  echo "DECISION: ERROR — script malfunction; manual review required."
fi
```

The script `check_p0_gate.py` (created in T-11) must enforce these thresholds (per strategy spec §4):

| Gate | Threshold |
|---|---|
| `evidence_validity` | ≥ 0.95 |
| `schema_validity` | = 1.00 |
| `trace_completeness` | = 1.00 |
| `should_refuse_hit_rate` | ≥ 2/3 |
| `cost_latency_reported` | All configs report `cost_usd`, `latency_ms_p50`, `latency_ms_p95`, `tokens_in`, `tokens_out` |

Decision recording (per Day 10 PM):

- [ ] Append to `docs/testing/audit_<date>.md` a "Day 10 Gate" section with: timestamp, gate values, decision (GREEN/RED-RECOVERABLE/RED-DISCLOSE), follow-up actions.
- [ ] If RED-DISCLOSE: update deck P7 with the explicit failed gate before Day 13 freeze.

---

## 6. Sample-Protocol Immutability Invariant

After T-10 (Day 8 PM), the following are **frozen** and any modification is a P0 protocol violation:

- `packages/eval/golden_set/v1_2.jsonl` — case content, distribution, expected_status, should_refuse flags.
- `data/catalyst_eval_frozen.db` — schema and contents (SHA-256 fingerprint enforces).
- The freeze run's input pinning fields: `model_id` per role, random seed, code git SHA at T-10 commit.

**Invariant check** (run on Day 10, Day 11, Day 12, Day 13):

```bash
# 0. Process precondition: T-10 commit must have been authorized by the user (per §0.bis).
#    If not, the immutability checks below cannot run and the operator must surface this
#    as a blocker.
T10_COMMIT=$(git log --pretty=format:"%H %s" --grep="T-10" -1 | awk '{print $1}')
test -n "$T10_COMMIT" || { echo "BLOCKER: T-10 commit not found; user must authorize T-10 commit before §6 immutability can run"; exit 2; }

# 1. Golden set unchanged in any committed form since T-10.
DRIFT_COMMITS=$(git log "$T10_COMMIT"..HEAD -- packages/eval/golden_set/v1_2.jsonl --pretty=oneline | wc -l)
test "$DRIFT_COMMITS" -eq 0 || { echo "VIOLATION: golden set modified after T-10"; exit 1; }

# 1b. Golden set has no uncommitted modifications either.
git diff --quiet -- packages/eval/golden_set/v1_2.jsonl && \
git diff --cached --quiet -- packages/eval/golden_set/v1_2.jsonl \
  || { echo "VIOLATION: uncommitted changes to golden set"; exit 1; }

# 2. Frozen DB SHA unchanged.
python -c "
import json, hashlib, glob
report = json.load(open(sorted(glob.glob('data/eval_reports/*_comparison.json'))[-1]))
freeze_sha = report['header']['db_sha256']
current_sha = hashlib.sha256(open('data/catalyst_eval_frozen.db','rb').read()).hexdigest()
assert freeze_sha == current_sha, 'VIOLATION: frozen DB content changed'
print('Frozen DB SHA OK')"
```

A failure of any check forbids further P0 progress until the cause is identified and reverted.

---

## 6.bis Source-Freeze Invariant (Day 8 → Day 13)

The strategy spec §4.1 Tier-A pinning requires that the `code_git_sha` captured at T-10 (Day 8 PM, eval freeze) be the SHA tagged at T-15 (Day 13). Between those two checkpoints, the following paths are **frozen at the source level**:

- `packages/**` — no commits, no staged source changes.
- `data/eval_reports/<frozen_timestamp>_*` — read-only.
- `data/traces/<frozen_run_id>.json` — read-only.
- `data/catalyst_eval_frozen.db` — read-only (SHA-256 invariant per §6).
- `packages/eval/golden_set/v1_2.jsonl` — frozen per §6.

Permitted edits between T-10 commit and T-15 tag:

- `data/eval_reports/comparison_*` and `data/eval_reports/audit_*` — these are derived artifacts produced by T-11 and T-13.
- `data/eval_reports/rerun_*` — produced by T-13.
- `docs/**` — including the deck draft and audit log.
- `notebooks/demo.ipynb`, `notebooks/demo_executed.ipynb` — produced by T-14.

**Invariant check** (run on Day 9, Day 10, Day 11, Day 12, Day 13 before tagging):

```bash
# Identify the T-10 commit. This requires the user to have authorized the T-10 commit
# at task close (per §0.bis). If the T-10 commit does not yet exist, this check is not
# runnable and must be flagged as a process blocker rather than skipped.
T10_COMMIT=$(git log --pretty=format:"%H %s" --grep="T-10" -1 | awk '{print $1}')
test -n "$T10_COMMIT" || { echo "BLOCKER: T-10 commit not found; user must authorize T-10 commit before §6.bis can run"; exit 2; }

# Source-freeze violators since T-10.
VIOLATIONS=$(git log "$T10_COMMIT"..HEAD --name-only --pretty=format: 2>/dev/null \
  | grep -v '^$' \
  | grep -E '^(packages/|data/catalyst_eval_frozen\.db|data/catalyst_dev\.db|data/catalyst_demo\.db|packages/eval/golden_set/v1_2\.jsonl)$')
if [ -n "$VIOLATIONS" ]; then
  echo "§6.bis SOURCE-FREEZE VIOLATION since T-10:"
  echo "$VIOLATIONS"
  exit 1
fi
echo "§6.bis source-freeze invariant holds since T-10."
```

If this check fails, the eval-frozen `code_git_sha` no longer represents the current code, and Tier-A reproducibility (§4.1) is broken. Remediation requires either:

1. Reverting the violating commits (preferred), or
2. Re-running T-10 with the new code and re-establishing the freeze (very expensive: re-pins, re-validates, restarts §6 immutability clock).

---

## 7. Freeze Protocol (Day 13 → Day 14 Whitelist)

After T-15 creates the freeze tag, only the following file paths may receive new commits:

```
^docs/.*\.md$
^docs/defense/.*$
^README\.md$    # only if a typo/clarity fix; no scope changes
```

**Forbidden** between freeze tag and defense:

- Any path under `^packages/`
- Any path under `^data/eval_reports/`, `^data/traces/`, `^data/.*\.db$`
- `^notebooks/` (the executed notebook is part of the frozen artifact set)
- `^pyproject.toml`, `^uv.lock`, dependency files

**Violation check** (run on Day 14 morning, before rehearsal):

```bash
TAG=$(git tag --list 'defense-freeze-*' | sort | tail -1)
test -n "$TAG" || { echo "FAIL: no freeze tag"; exit 1; }
VIOLATIONS=$(git log $TAG..HEAD --name-only --pretty=format: 2>/dev/null \
  | grep -v '^$' \
  | grep -vE '^(docs/.*\.md|docs/defense/|README\.md)$')
if [ -n "$VIOLATIONS" ]; then
  echo "FREEZE VIOLATION:"
  echo "$VIOLATIONS"
  exit 1
fi
echo "Freeze whitelist OK."
```

---

## 8. Open Decisions (≤6, require user ruling before execution starts)

| # | Decision | Default if not ruled | Blocking? |
|---|---|---|---|
| OD-1 | Defense deck format and committed path. Markdown (`docs/defense/deck.md`) is plan-default; could be Marp / Slidev / Reveal source. | `docs/defense/deck.md` (Markdown). | Blocks T-11 deck draft start. |
| OD-2 | Critic threshold initial values: `K_sufficient`, `K_partial`, `M_threshold`. Spec §12.1 proposes `4 / 2 / 0.6`. | `4 / 2 / 0.6` initial; calibrated in T-10. | Blocks T-03 Critic emit logic. |
| OD-3 | Should-refuse case selection (3 specific cases). Per §12.2 criteria: out-of-window date, unknown ticker, or deliberately ambiguous query. | Picked from existing golden set + 1–2 new cases authored Day 7. | Blocks T-10 (Day 8) freeze. |
| OD-4 | Defense talk length. Spec §12.4 assumes 15 minutes. | 15 min, 8 slides + appendix. | Blocks T-16 rehearsal target. |
| OD-5 | Layer 2 thresholds calibration. Spec §12.5: defaults are 30 macro / 5 per-ticker / 50 GDELT (Tier 1) or 20 fallback (Tier 2). | Spec defaults. | Blocks T-09 preflight gate values. |
| OD-6 | `direct_llm` baseline model. Choices: `claude-opus-4-7`, `claude-sonnet-4-6`, `claude-haiku-4-5-20251001`. Cost vs. answer-quality tradeoff. | `claude-sonnet-4-6` (mid-tier; matches typical "frontier baseline" framing without Opus-level cost). | Blocks T-05 baseline run. |

---

## 9. Self-Review Record

Conducted on plan v1, 2026-04-30. Four targeted checks per the user's brief.

### 9.1 P1 / P2 contamination check

Reviewed every task description and subtask for P1/P2 leakage.

- ✅ `rag_only` (W-01, P1) — not implemented in any task. T-11 explicitly compares only `direct_llm` vs `mcj_full`.
- ✅ Layer 3 (W-02, P2) — T-04 short-circuits to `refuse`; T-06 raises `NotImplementedError`. No related-entity logic.
- ✅ LLM-graded sufficiency (W-03, P2) — T-03 Subtask 3.2 uses threshold rules only.
- ✅ Budget circuit breaker (W-04, P2) — T-04 records `partial_budget_flag` but does not enforce mid-run stop.
- ✅ Model routing (W-05, P2) — single-model run path throughout; OD-6 is one model choice, not a routing policy.
- ✅ LangSmith (W-06, P1) — local SQLite only in T-07; no LangSmith export.
- ✅ Full eval matrix (W-07) — 10-case set only; not extended.
- ✅ G5 eval extraction (P1, cuttable) — T-12 makes G5 conditional on Day 10 GREEN; not in critical path.

**Result:** clean. P1/P2 references appear only as explicit deferrals.

### 9.2 Verification command runnability

Spot-checked each task's verification block:

- ✅ `find docs -type f -name "*.md"` (T-01) — standard POSIX.
- ✅ `pytest tests/ -v` invocations (T-03, T-04, T-06, T-07, T-08) — match the existing `verify_local_pytest.sh` conventions seen in `packages/agents/README.md`.
- ✅ Python one-liners with `assert` — runnable with the project venv.
- ✅ `jq` and `sha256sum` — assumed present on macOS+linux dev machines; if `jq` absent, fall back to `python -c "import json; ..."`.
- ✅ `git tag --list`, `git for-each-ref`, `git log --name-only` — all standard git.

**One caveat:** Verification commands using paths like `data/eval_reports/<frozen_timestamp>_*` are templates; the Day 8/9 actual timestamp must be substituted. The pattern `$(ls -t ... | head -1)` is used where possible to avoid hardcoding.

### 9.3 Strategy spec waiver conflicts

Cross-checked plan tasks against strategy spec §11 (W-01 through W-09):

| Waiver | Plan compliance |
|---|---|
| W-01 rag_only → P1 | ✅ Not in any P0 task. |
| W-02 Layer 3 → P2 | ✅ T-04, T-06 short-circuit. |
| W-03 LLM-graded sufficiency → P2 | ✅ T-03 uses thresholds. |
| W-04 Budget circuit breaker → P2 | ✅ Flag-only, no mid-run cut. |
| W-05 Model routing → P2 | ✅ Single model. |
| W-06 LangSmith → P1 | ✅ Not in T-07. |
| W-07 Full eval matrix → roadmap | ✅ 10-case only. |
| W-08 should_refuse_hit_rate substitution | ✅ T-11 gates use hit rate, not precision/recall. |
| W-09 GDELT contingency Tier 1/Tier 2 | ✅ T-09 implements both tiers; chosen tier recorded. |

**Result:** no conflicts.

### 9.4 Undeclared divergence from execution spec

Compared plan against `docs/full-version-execution-spec.md` for silent narrowing:

- §5 retrieval: execution spec calls for full 3 layers in v1. Plan implements 1+2 only — declared as W-02. ✅
- §6 DecisionRouter rules: plan implements all five branches per execution spec; one (`expand_related`) is short-circuited per W-02. ✅
- §7 output states: plan implements all four states. ✅
- §8 validator: plan implements all four checks (evidence id / time window / schema / magnitude). One nuance — execution spec mentions "time-window check"; plan's T-03 lists evidence-id / schema / magnitude only. **Minor gap.** Adding to T-03 Subtask 3.3 implicitly via the existence-check (evidence ids carry their dates from corpus, so a case-window-bound evidence_id would already be filtered at retrieval). Verification command should grep validator for `time_window`. **Action:** add to T-03 Subtask 3.3 explicitly.
- §9 harness taxonomy: 5 classes documented (T-08); 2 of 5 have regression tests in P0; remaining 3 listed as P1 enhancement. Acceptable — strategy spec §2.1 says "at least 2 regression tests".
- §10 trace fields: all required fields enumerated in T-07.
- §11 cost governance: budgets reported, not enforced (W-04). ✅
- §12 evaluation: `direct_llm` only (W-01). ✅
- §13 phasing: P0 covers Phase 1 + minimal Phase 2 + minimal Phase 3 (taxonomy doc + 2 tests). Phase 4 (full eval matrix) explicitly waived (W-07). ✅
- §14 docs strategy: 4-anchor enforced. ✅
- §15 ADR backlog (ADR-004 to ADR-007): not in P0; goes to roadmap.

**One real gap found and patched in self-review:** Validator's time-window check. T-03 Subtask 3.3 will add it as the fourth check (evidence-id existence, schema, magnitude, time-window). Updating the file inline.

---

**End of plan.** Open decisions OD-1 through OD-6 must be ruled before execution starts (T-01 can begin once OD-1 is settled; the rest are needed by their respective task entry days).
