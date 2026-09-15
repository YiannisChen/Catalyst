# M7 Completion Operator Guide

> **For implementation Codex:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` and execute the binding M7 plan task-by-task.

**Goal:** Complete Catalyst V1.1 M7 from the current Q-011 checkpoint through a human-audited, reproducible Stage-1 benchmark report and supervisor-owned tag.

**Architecture:** The binding specification remains `docs/plans/2026-08-19-catalyst-v1.1-m7-stage1-eval.md`. This guide only defines the remaining order, ownership, human stops, and naming policy. No model may fabricate authorization, runtime identity, budget, credentials, audit decisions, or a passing seal.

**Tech Stack:** Python, Pydantic, pytest, SQLite, the M6 app/SSE runtime, provider adapters, canonical JSON/JSONL, Git.

---

## 1. Current checkpoint

- Worktree: `/Users/yiannischen/Desktop/Catalyst-v1.1-m7`
- Branch: `v1.1/m7-stage1-eval`
- Observed HEAD before this guide: `f93338bcd079e611964bf088d705ad59a185183c`
- Existing M7 Option B/Q-011 and Phase A implementation changes are modified or untracked and must be preserved and reviewed; they are not yet committed.
- The authorized 12-case candidate exists only under `scratch/q011/human_review/authorized_stage1_staging/`.
- The formal V1.1 Stage-1 benchmark has not been published.
- M7-10 has not executed and no final report or tag exists.

Revalidate every fact at the start. A changed HEAD or diff is not automatically an error, but it must be explained before continuing.

## 2. Naming policy

Use professional benchmark terminology for all new V1.1 public surfaces:

- directory: `packages/eval/benchmarks/v1_1/stage1/`
- files: `cases.jsonl`, `stratification.json`, `manifest.json`, `README.md`
- public contracts: `BenchmarkCase` (`is GoldenCase`), `BenchmarkDatasetManifest`, `HumanOutputAudit` (`is Stage1OutputAudit`)
- loader: `catalyst_eval.v1_1.loader.load_benchmark_cases`
- reports: `v1_1_stage1_<sha8>.*`

Do not rename or rewrite sealed historical V1/V1.2 artifacts merely to remove legacy `golden_set` terminology. If compatibility is needed, use an explicit deprecated alias or adapter and test it. New V1.1 commands, errors, docs, and artifacts must use `benchmark`, not `golden`.

**Publication boundary.** The published benchmark records **no provider-call ceiling and no USD cost ceiling**. Approved ceilings are operator decisions made at Human Gate 2 and are recorded only in the runtime `prepare_summary.json` under `data/eval_reports/`; they are never part of the benchmark publication. The draft `packages/eval/benchmarks/v1_1/stage1/README.md` states this and the directory stays README-only until Gate 1.

## 3. Remaining sequence

### Phase A — Offline corrective pass

Owner: implementation Codex. No providers, credentials, benchmark publication, staging, commit, tag, push, or database mutation.

1. Revalidate branch, HEAD, tracked diff, authoritative plan, and Q-011 artifact hashes.
2. Review and finish the existing Option B changes without discarding them.
3. Add RED tests, then normalize the new V1.1 benchmark names and paths under the policy above.
4. Make `--runtime-db` mandatory for `prepare` and `all`; remove the `runtime-id:stage1` and repeated-`d` fallback identities.
5. Hash large runtime databases incrementally rather than loading a multi-gigabyte file with `read_bytes()`; assert multiple bounded reads over a multi-chunk file.
6. Add a directly executable production operator factory for `M6AppSseRunnerAdapter`; the default CLI must remain fail-closed if required configuration is absent.
7. Make plan, `--help`, `prepare`, and `execute` commands agree on stratification, runtime DB, provider-call ceiling, and cost ceiling.
8. Separate the immutable Q-001 data DB from the writable M6 runtime DB: `--runtime-db` stays read-only and SHA-bound; the runtime writes go to an output-dir-owned `runtime.sqlite3`, and Q-001 is never initialized/WAL-enabled/migrated/admitted into.
9. On `execute`, incrementally re-hash Q-001 requiring byte-identical equality with the prepare summary, require the execute ceilings to equal the prepared ceilings exactly, and refuse a `0/0` prepare that would later authorize positive execution.
10. Bind the remaining budget, prepared identity, explicit case timeout, and pre-submit RunManifest capture into the production adapter; enforce the total ceiling before work that could exceed the remaining budget.
11. Seal run facts only from authoritative persisted M6 events/artifacts: no synthetic pool id, no empty ranking list presented as a real result, no defaulted trajectory success, no manifest/result hash substituted for a missing ContextPack/claim/assurance artifact. Read and verify the authoritative terminal event through the persisted event/SSE contract; missing required facts fail closed.
11a. Keep `data_runtime_identity_object_hash` (canonical object hash) distinct from `q001_file_sha256` (immutable SQLite file SHA); verify both before dispatch. Resolve credentials as env-name -> stripped env-value -> missing fail-closed, and never persist the value. Persist every terminal case to the atomic ledger before reconciliation; unknown cost is not `$0`, and a missing retrieval timestamp fails closed. Analyst/Writer retry boundaries must record one usage pair per dispatched attempt from `usage_metadata`/`response_metadata`; whole-run provider totals and `run_facts.tokens` are numeric only when every attempt has complete, verifiable usage, while cost remains charged at the conservative reservation bound when usage is unknown. Top-level `tokens`, `latency_ms`, and `cost_usd` are the single run-accounting fields shared by run facts, report, and output-audit reconstruction; missing values remain non-scorable and are never summed as zero. Run-facts reconstruction must use strict recursive persisted models: legal non-empty status/attribution enums, exact booleans, non-coercing non-negative integers and finite costs, unique non-empty IDs, complete trajectory/retrieval/provider blocks, valid PoolManifest and observation truth, and token/cost/provider-call reconciliation; missing/invalid fields fail closed. Hermetic verification removes every `/Users/yiannischen/Desktop/Catalyst/packages/*` entry from `sys.path` and proves all four imports resolve under this checkout before and after each full package suite. A V1-only retrieval adapter may not infer arms/finals from lossy hits, and a completion/cancellation race must persist provider accounting before acknowledging `CANCELLED`.
11b. Close the observability gap at its source: the agents-owned `RunDiagnostics` (`v1.1_run_diagnostics_v1`) contract, retained by the retrieval adapter/executor/graph and published by the M6 app runtime as the versioned `run_diagnostics` artifact in the terminal transaction. Eval computes stop correctness, recovery/usefulness, and materiality against `ExpectedResearchBehavior`; the runtime reports only observed facts.
11c. Enforce a real shared provider budget at the provider invocation boundary (`ProviderBudgetGuard` + `invoke_with_bounded_retry`): per-case and cumulative call budgets before dispatch, identity-bound model pricing with bounded per-role token limits for cost, and fail-closed when no defensible pre-call upper bound exists. Reconcile the persisted per-case accounting after every case.
11d. Write the source-to-metric closure matrix (`docs/plans/2026-09-14-m7-stage1-source-to-metric-closure-matrix.md`) and classify every consumed field as production-observed fact, benchmark/human truth, derived eval metric, or unavailable/non-scorable.
12. Prepare the exact publication diff from the authorized staging artifacts, but do not copy it into the formal benchmark directory yet.
13. Run focused RED/GREEN tests, then all relevant package suites, `git diff --check`, secret scan, canonical-DB before/after hashes, and explicit changed-file review.
14. Stop with exact HEAD, diff, tests, canonical DB hashes, runtime/data DB separation evidence, authoritative artifact mapping, the closure matrix, end-to-end test output, provider-budget proof, draft README, exact publication command, non-overlapping proposed commits, runtime prerequisites, and unresolved blockers.

Then GROK performs an independent read-only source review. Codex adjudicates its findings.

### Human Gate 1 — Publication and commits

The user explicitly authorizes the exact reviewed diff, formal benchmark publication, and listed commits. Without that authorization, stop.

After authorization, implementation Codex publishes the benchmark and creates small explicit-path commits, normally:

1. `fix(eval): align Stage-1 corrective eligibility gates`
2. `refactor(eval): standardize Stage-1 benchmark contracts`
3. `fix(eval): enforce Stage-1 operator preflight`
4. `data(eval): publish V1.1 Stage-1 benchmark`

Commit boundaries may change if the actual diff requires it, but `git add -A` is forbidden.

### Phase B — Provider-free operational preflight

Owner: implementation Codex. No provider calls.

1. Start from the clean committed execution HEAD.
2. Resolve the real Q-001 operational derivative and its authoritative runtime identity from repository evidence; never select a database by filename or size alone.
3. Verify required lexical/dense/hybrid/reranked assets and active runtime composition.
4. If the production-bound dense index is missing, stop and produce a minimal GPU rebuild/import plan. Do not rent a GPU or reuse an inactive Q-011 candidate as production state without separate authority.
5. Perform the pre-Gate-2 readiness/identity/index checks and *calculate a proposed* provider-call and USD budget from the actual call graph (per-agent: initial + at most one corrective Analyst call, one Writer call, at most one technical retry per logical role), the case count, the models, and current published pricing. The same work produces the identity-bound `--pricing-json` the operator will supply at execute. Because a case can end FAILED after dispatching provider work, the proposed USD ceiling must also cover the conservative pre-call upper bound of every attempt the run may dispatch, not only the attempts of a COMPLETED case. Do not invent or silently round a budget, and do not yet run the authoritative `prepare`.
6. Stop and ask for Human Gate 2.
7. Only after Gate 2 approval, run the authoritative `prepare` with the approved ceilings and verify the EvalManifest/eval ID, 12 ordered cases, Q-002 `NON-COMPARABLE`, clean execution HEAD, resumable ledger, the separate `data_runtime_identity_object_hash` and `q001_file_sha256`, and zero provider calls.

### Human Gate 2 — Provider budget and credentials

The user approves the exact maximum provider-call count and USD ceiling, configures the secret locally, and authorizes live execution. Secrets must never appear in prompts, logs, artifacts, Git, or reports.

### Phase C — Live execution

Owner: implementation Codex.

0. **Prerequisites (fail-closed, verified against source).** The observability
   gap is closed: the M6 app persists the versioned `run_diagnostics` artifact
   in the terminal transaction and the versioned `run_provider_accounting`
   artifact before a FAILED/CANCELLED/TIMEOUT terminal event, and the adapter
   seals run facts from persisted rows + artifacts only. Live execution still
   fails closed when:
   - the `run_diagnostics` artifact is missing, hash-mismatched, bound to
     another run, or disagrees with the terminal event;
   - a non-COMPLETED run has no persisted `run_provider_accounting` artifact
     (the consumed calls/cost are never reported as zero);
   - the provider budget guard cannot bound the next attempt (call budget
     exhausted, outstanding reservations already hold the ceiling, no
     identity-bound price, or no per-role token limit);
   - the persisted accounting does not reconcile with the guard after a case,
     or the case ends holding an unsettled reservation.
   Do not weaken any of these to make a run pass.

   **Artifact/event schema (binding).**

   | Artifact | Schema | When published | Bound to |
   | --- | --- | --- | --- |
   | `run_manifest` | app-owned | admission (pre-submit capture) | run_id |
   | `context_pack` | app-owned | context-pack stage | run_id |
   | `claim_detail` | app-owned | claim-validation stage | run_id |
   | `attribution_result` | app-owned | terminal transaction | terminal seq |
   | `assurance` | app-owned | terminal transaction | terminal seq |
   | `run_diagnostics` | `v1.1_run_diagnostics_v1` | terminal transaction (COMPLETED) | run_id + terminal seq + data-runtime identity |
   | `run_provider_accounting` | `v1.1_run_provider_accounting_v1` | before any FAILED/CANCELLED/TIMEOUT terminal event (any provider work) | run_id |

   `run_diagnostics` carries `retrieval` (arms, ordered candidate/final
   evidence ids, per-task rank changes, measured latency, degradation reasons,
   computed ticker/cutoff violations, `observed`), `trajectory` (executed
   corrective rounds with gap reason codes/action ids/evidence deltas),
   `provider` (calls, attempts, tokens, cost + `cost_method`), and `terminal`
   (status, attribution type, status ceiling, refusal availability).

   Retrieval candidates/rank changes come from the real pre-conversion
   observation at the `ProductionHybridRetriever` boundary; they are never
   inferred from the lossy V1.1 hit ranks, and a field the runtime cannot
   observe stays typed unavailable/non-scorable rather than an empty value.
1. Execute through the production M6 app/SSE adapter using the prepared identity and approved ceilings, passing `--pricing-json <non-secret prices + per-role token limits>` whenever the approved USD ceiling is positive.
2. Persist resumable state; resume only identity-valid completed cases.
3. Stop on identity mismatch, secret leakage, missing runtime capability, budget exhaustion, or a non-zero hard gate. Do not weaken gates or restart from a new identity.
4. Produce immutable run/result artifacts for all 12 cases and a compact execution summary.

### Phase D — Independent audit assistance

1. WebGPT reviews the 12 immutable outputs and produces non-authoritative audit proposals.
2. GROK independently reviews the same outputs without seeing WebGPT's conclusions first.
3. Codex compares both reviews with the schema, run identities, claims, citations, and hard-gate definitions, then presents only disputed or material items to the user.

Neither model writes human approval fields or modifies run artifacts.

### Human Gate 3 — Final 12-case audit

The user reviews the compact 12-case summary and either approves it or lists corrections. Implementation Codex may persist only that explicit decision with the authorized pseudonymous reviewer ID and actual UTC timestamp.

### Phase E — Seal and M7 exit

Owner: implementation Codex for generation; Codex for final acceptance and tag.

1. Validate the human audit against immutable run/result hashes.
2. Generate the write-once EvalManifest, audit JSONL, report JSON, and deterministic Markdown.
3. Re-run report generation and prove byte-identical idempotence.
4. Run full M7 gates, migration/user-smoke regression, leakage/secret scan, package suites, and `git diff --check`.
5. Stage only the four authoritative report artifacts and commit `chore(eval): seal V1.1 Stage-1 report <sha8>`.
6. Stop for Codex review. Codex verifies exact commits, tests, identities, budgets, case results, hard gates, comparability, files, and Git status.
7. Only after that review may Codex create annotated tag `v1.1-m7-stage1` and declare M7 complete. M8 starts from the reviewed integration point, not from an unreviewed worktree.

### Non-secret pricing input (`--pricing-json`)

At execute time the operator supplies the identity-bound prices and bounded
per-role token limits the provider budget guard needs. The file contains no
secrets (published per-million-token prices and token bounds only):

```json
{
  "models": {
    "<provider>/<model_id>": {
      "input_usd_per_million_tokens": 0.0,
      "output_usd_per_million_tokens": 0.0
    }
  },
  "role_token_limits": {
    "evidence_analyst": {"max_input_tokens": 0, "max_output_tokens": 0},
    "streaming_writer": {"max_input_tokens": 0, "max_output_tokens": 0}
  }
}
```

Role keys are the agents' role constants (`evidence_analyst`,
`streaming_writer`). A positive `--max-cost-usd` with a missing price for the
exact `(provider, model_id)` or a missing role limit fails closed before the
first provider attempt; the guard then charges the conservative pre-call upper
bound whenever the role does not report actual token usage, and labels the run
`upper_bound_charged`.

## 4. Implementation Codex prompt

```text
Continue Catalyst V1.1 M7 in /Users/yiannischen/Desktop/Catalyst-v1.1-m7. Treat docs/plans/2026-08-19-catalyst-v1.1-m7-stage1-eval.md as binding, docs/plans/2026-09-13-m7-completion-operator-guide.md as the current checkpoint guide, and docs/plans/2026-09-14-m7-stage1-source-to-metric-closure-matrix.md as the source-to-metric authority. Execute Phase A only. Preserve the existing M7 Option B/Q-011 and Phase A changes. Use RED tests first; standardize only new V1.1 surfaces to benchmark terminology while preserving sealed legacy compatibility; close the real-runtime identity, mandatory --runtime-db, large-file hashing, production M6AppSseRunnerAdapter wiring, CLI/plan command mismatches, provider-cost overrun persistence, complete fusion candidate observation, and role-accurate FAILED accounting. Do not call providers, inspect secrets, publish the formal benchmark, stage, commit, tag, push, or mutate canonical databases. Finish all safe offline verification, then STOP with exact HEAD, diff, tests, proposed commits, publication file list, and blockers.
```

## 5. GROK review prompt after Phase A

```text
Perform an independent read-only review of the current M7 Phase A diff against all three binding plan files. Verify the Option B amendment, V1.1 benchmark naming/legacy compatibility, strict real Q-001 runtime identity, mandatory runtime DB, incremental DB hashing, executable M6 app/SSE operator wiring, CLI/docs parity, provider-cost overrun persistence before fail-closed, complete fusion candidate observation, role-accurate FAILED accounting, RED regressions, publication boundary, and secret/provider safety. Do not edit files and do not assume the implementation conclusions are correct. Report findings by P0-P3 with exact file/line evidence, required fixes, test gaps, and a final GO/NO-GO for Human Gate 1.
```

## 6. Human authorization wording

Gate 1:

```text
我已审阅 Phase A 摘要，授权按已列出的确切文件发布 V1.1 Stage-1 benchmark，并按已列出的 commit boundaries 提交；不授权 provider 调用、tag、push 或 M8。
```

Gate 2:

```text
我批准本次 M7-10 最多 <N> 次 provider calls、总费用不超过 <USD> 美元；密钥已由我在本地环境配置。授权按已 seal 的 prepare identity 执行，禁止超额或更换 identity。
```

Gate 3:

```text
我已查看最终 12-case 审计摘要，批准所列 human audit 决定，并授权以我的 pseudonymous reviewer ID 和实际 UTC 时间持久化；未列出的内容不视为批准。
```
