# Cursor Agent — P0 Closure Sprint

## Situation

All 16 tasks (T-01 → T-16) of the Catalyst P0 construction plan are **complete and merged** to `feat/full-v1`. Day 10 Gate is **GREEN** (all 5 gates pass). The Tier-A mcj_full rerun shows zero status mismatches. The annotated tag `defense-freeze-2026-05-04` exists and points to `code_git_sha = 9f844eddfffff83a22077eb5c9ea16025a148019`.

Codex usage is exhausted; you are the implementation agent now. A separate reviewer (Claude in Cowork) reviews your output. **Stage only, do not commit.**

## Your Role

You are doing **P0 closure housekeeping** — no new features, no `packages/` code changes. Everything you produce is docs, configuration, or git operations.

## Critical Constraints (unchanged)

- **§J.6 source-freeze**: NO changes to anything under `packages/` or to `data/catalyst_eval_frozen.db`.
- **Frozen artifacts immutable**: preflight, freeze_header, comparison, golden set.
- **CLAUDE.md rules**: Conventional Commits, no AI mentions, English only for code/comments.
- **Python venv**: `/Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python`

## Tasks

### Task A: Final Artifact Inventory Verification

Create `docs/defense/artifact_inventory.md` that lists every artifact from §I.6 of the plan, its expected path, and whether it exists + is non-empty. This is a checklist document.

Required artifacts (from plan §I.6):

| Artifact | Expected Path |
|---|---|
| Frozen DB | `data/catalyst_eval_frozen.db` |
| Frozen vector index | `data/lancedb_gold/eval_frozen/` (exists but empty — W-15) |
| direct_llm run report | `data/eval_reports/20260503_154053_direct_llm.{md,json}` |
| mcj_full run report | `data/eval_reports/20260503_154053_mcj_full.{md,json}` |
| Comparison report | `data/eval_reports/20260503_154053_comparison.{md,json}` |
| Trace files (20 original) | `data/traces/*.json` (excluding `rerun_*`) |
| Tier-A rerun report | `data/eval_reports/rerun_20260504_072205_mcj_full.{md,json}` |
| Rerun traces (10) | `data/traces/rerun_*.json` |
| Audit log | `docs/testing/audit_2026-05-04.md` |
| Demo notebook (cleared) | `notebooks/demo.ipynb` |
| Demo notebook (executed) | `notebooks/demo_executed.ipynb` |
| Deck | `docs/defense/deck.md` |
| Preflight report | `data/eval_reports/preflight_20260503_154053.json` |
| Freeze header | `data/eval_reports/freeze_header_20260503_154053.json` |
| Golden set (frozen) | `packages/eval/golden_set/v1_2_p0_set.jsonl` |
| Threshold calibration | `data/eval_reports/20260503_154053_threshold_calibration.json` |
| Failure taxonomy | `docs/testing/failure-taxonomy.md` |

For each: check existence, file size > 0, and for JSON files validate they parse correctly. Write the results as a markdown table with ✅/❌ status.

### Task B: `public/showcase` Branch Update

The `public/showcase` branch is the curated public-facing branch. Update it:

```bash
git checkout public/showcase
git merge --no-ff feat/full-v1 -m "merge: P0 construction sprint complete (T-01 → T-16)"
```

If `public/showcase` doesn't exist yet, create it from `feat/full-v1`:
```bash
git checkout -b public/showcase feat/full-v1
```

Then switch back to `feat/full-v1`.

### Task C: P0 Completion Summary

Create `docs/defense/p0_completion_summary.md` containing:

1. **Sprint overview**: T-01 → T-16 completion dates, all GREEN.
2. **Gate results**: the 5 gate values from the frozen comparison.
3. **Tier-A reproducibility**: rerun status match confirmation.
4. **Frozen anchor summary**: all SHAs, tags, frozen_ts.
5. **Waiver summary**: W-01 through W-15 status (done/deferred-P1/deferred-P2), one line each.
6. **Known limitations / residual risks**:
   - W-15: SQL-only retrieval (LanceDB deferred)
   - T-01 verbatim phrase partial pass
   - T-06 polygon_news count below nominal band
   - §J.6 operational anchor is T-14-close (not strict T-13b for packages/)
7. **Defense readiness**: rehearsals pending (human), all artifacts present, deck has 8 slides + appendix.

Pull all numbers from the actual frozen artifacts (comparison.json, preflight.json, freeze_header.json, calibration.json). Do not hardcode — read and extract.

### Task D: Clean Up Handoff Docs

Remove or unstage the temporary handoff prompts that were scaffolding:
- `docs/cursor-t15-t16-handoff.md` — delete (untracked, just leave it)
- `docs/cursor-p0-closure-prompt.md` — do not stage this file

### Task E: Final Verification Script

Create `docs/defense/verify_p0_complete.sh` — a single bash script that runs ALL plan verification commands for T-13a through T-16 in sequence and reports PASS/FAIL for each. This is the "one command to prove P0 is done" script.

It should:
1. Check trace count ≥ 20 (non-rerun) + ≥ 10 (rerun)
2. Verify Tier-A pinning in both config reports
3. Run `check_p0_gate.py` → expect exit 0
4. Verify DB SHA matches header
5. Verify schema_version = 1.0 on comparison + preflight
6. Verify §J.6 from task/T-14-close (no packages/ changes)
7. Verify defense-freeze tag exists and is annotated
8. Verify tag SHA matches comparison header code_git_sha
9. Verify audit has ≥ 14 T-NN rows
10. Verify deck has ≥ 8 slides
11. Verify notebook exists and has no live API URLs
12. Print a summary table at the end

Use the venv python: `/Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python`

---

## Delivery

1. Branch from `feat/full-v1` as `p0-closure`
2. Stage all new files with `git add`
3. Do NOT commit
4. Print checklist, `git diff --cached --stat`, and staged file list
5. Run `verify_p0_complete.sh` and include output
