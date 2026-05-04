# Cursor Agent Handoff — T-15 & T-16 Implementation

## Project Context

**Catalyst** is a financial attribution agent that explains stock price movements. The architecture follows a LangGraph MCJ pipeline: Miner → Critic → DecisionRouter → Judge → Validator → Finalizer. The project uses a plan-driven gating model (T-01 through T-16) where each task has strict entry/exit criteria.

We are in the **P0 construction sprint final stretch**. Tasks T-01 through T-14 are complete, committed, and merged to `feat/full-v1`. The frozen eval has been run (10 direct_llm + 10 mcj_full = 20 traces), all gates pass (GREEN), and thresholds are calibrated.

## Your Role

You are the **implementation agent** (replacing Codex, whose usage is exhausted). You write code, tests, and artifacts. A separate reviewer (Claude in Cowork) will review your output before the human authorizes commits. Follow the same patterns established by Codex:

1. **Branch from `feat/full-v1`** with a descriptive branch name (e.g., `t15-verification-audit`).
2. **Stage files with `git add`** but do NOT commit — the human will commit after review.
3. **Run tests** and report results.
4. **Print a checklist** of what was done, matching the plan exit criteria.
5. **Print `git diff --cached --stat`** at the end.

## Critical Constraints

- **CLAUDE.md rules apply**: no vibe coding, Conventional Commits, no AI mentions in code/commits, English only for code/comments.
- **§J.6 source-freeze invariant**: NO changes to `packages/` source code since `task/T-13b-close`. T-15 and T-16 only produce docs, audit logs, report artifacts, notebooks, and deck polish. Any file under `packages/` must NOT be modified.
- **Frozen artifacts are immutable**: `data/catalyst_eval_frozen.db`, `packages/eval/golden_set/v1_2_p0_set.jsonl`, and all `data/eval_reports/freeze_header_*.json` / `preflight_*.json` must not change.
- **Python venv**: use `/Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python` for running scripts.

## Key Frozen Anchors

- `frozen_ts`: `20260503_154053`
- `db_sha256`: `3d8a1ee3a86b216779dfd9cc2e6ccd45090326c65b08e4a19e7b89d25935e491`
- `lancedb_dir_sha256`: `DEFERRED_P1`
- Tag `task/T-13b-close` exists (source-freeze anchor)
- Tag `task/T-14-close` exists
- Golden set: 10 cases (5 sufficient, 2 partial, 3 should_refuse)
- All 5 gates GREEN: evidence_validity=1.0, schema_validity=1.0, trace_completeness=1.0, should_refuse_hit_rate=1.0, cost_latency_reported=True

## Existing File Structure (key paths)

```
docs/plans/2026-04-30-p0-construction-plan-v3.md   — Master plan (read T-15 at line 1252, T-16 at line 1298)
docs/defense/deck.md                                — 8-slide deck draft (T-14)
docs/testing/failure-taxonomy.md                     — 5 failure classes (T-12)
packages/eval/scripts/check_p0_gate.py              — Gate script (exits 0/1/2)
packages/eval/scripts/run_frozen_eval.py            — Frozen eval orchestrator (808 lines)
packages/eval/scripts/preflight.py                  — Preflight checks
packages/eval/catalyst_eval/harness/frozen_eval.py  — Eval helpers
data/eval_reports/20260503_154053_comparison.json    — Frozen comparison
data/eval_reports/preflight_20260503_154053.json     — Preflight artifact
data/eval_reports/freeze_header_20260503_154053.json — Freeze header
data/traces/*.json                                   — 20 trace files
data/catalyst_eval_frozen.db                         — Frozen SQLite corpus
```

---

## T-15: Day 10 Gate + Verification Audit

### Entry: T-14 complete ✅

### Exit Criteria (what you must produce):

1. **Run `check_p0_gate.py`** against the comparison artifact. Record the GREEN/RED decision.

2. **Create `docs/testing/audit_<date>.md`** with a Day-10-Gate section, then a per-task verification table covering T-01 through T-14. Each row must re-run (or cite) the verification command from the plan and record PASS/FAIL.
   - Format: `| T-NN | Task Name | Verification Result | Notes |`
   - Must have ≥14 rows (one per task).

3. **Tier-A reproducibility rerun**: Run `mcj_full` against the frozen DB again and output to:
   - `data/eval_reports/rerun_<ts>_mcj_full.{md,json}`
   - `data/traces/rerun_<run_id>.json`
   - Diff the rerun results against the original per §I.5 Tier A (status distributions must match).

4. **§J.6 source-freeze check**: Verify no `packages/` changes since `task/T-13b-close`:
   ```bash
   T13B_COMMIT=$(git rev-parse task/T-13b-close)
   git log "$T13B_COMMIT"..HEAD --name-only --pretty=format: | grep -v '^$' | grep -E '^packages/'
   # Must be empty
   ```

### Verification Commands (from plan):
```bash
# Audit row count
AUDIT=$(ls docs/testing/audit_*.md | sort | tail -1)
ROWS=$(grep -c "^| T-" "$AUDIT")
test "$ROWS" -ge 14

# Rerun artifact exists
ls data/eval_reports/rerun_*_mcj_full.json | head -1

# DB SHA unchanged
python -c "
import json, hashlib, glob
r = json.load(open(sorted(glob.glob('data/eval_reports/*_comparison.json'))[-1]))
freeze_sha = r['header']['db_sha256']
current = hashlib.sha256(open('data/catalyst_eval_frozen.db','rb').read()).hexdigest()
assert freeze_sha == current"

# Schema versions = 1.0
python -c "
import json, glob
c = json.load(open(sorted(glob.glob('data/eval_reports/*_comparison.json'))[-1]))
p = json.load(open(sorted(glob.glob('data/eval_reports/preflight_*.json'))[-1]))
assert c.get('schema_version') == '1.0'
assert p.get('schema_version') == '1.0'
print('artifact schema_version=1.0 OK')"

# §J.6 source freeze
T13B_COMMIT=$(git rev-parse task/T-13b-close)
VIOLATIONS=$(git log "$T13B_COMMIT"..HEAD --name-only --pretty=format: | grep -v '^$' | grep -E '^(packages/|data/catalyst_eval_frozen\.db|data/lancedb_gold/eval_frozen/|packages/eval/golden_set/v1_2_p0_set\.jsonl)$')
test -z "$VIOLATIONS"
```

---

## T-16: Tech Dry-Run + Code Freeze + Deck Polish + Defense Rehearsal

### Entry: T-15 complete

### Exit Criteria:

1. **`notebooks/demo.ipynb`** authored — demonstrates the frozen eval pipeline offline (NO live API calls). Must execute cleanly via `jupyter nbconvert --execute`.
   - Produce `notebooks/demo_executed.ipynb` as output.

2. **Annotated tag `defense-freeze-YYYY-MM-DD`** at HEAD. Tag commit SHA must equal the eval-frozen `code_git_sha` from the comparison report header.

3. **Deck final** (`docs/defense/deck.md`): replace any placeholders with real data from frozen artifacts (gate results table, status distribution, cost/latency numbers).

4. **Freeze whitelist**: after the tag, only `docs/**/*.md`, `docs/defense/**`, and `README.md` may be modified.

### Verification Commands (from plan):
```bash
# Notebook executes offline
jupyter nbconvert --to notebook --execute notebooks/demo.ipynb --output /tmp/demo_check.ipynb

# No live API references
grep -E "(api\.openai|api\.anthropic|claude\.ai)" notebooks/demo.ipynb
# Expected: empty

# Annotated freeze tag exists
TAG=$(git tag --list 'defense-freeze-*' | sort | tail -1)
test -n "$TAG"
test "$(git for-each-ref refs/tags/$TAG --format='%(objecttype)')" = "tag"

# Tag SHA = eval-frozen code SHA
TAG_SHA=$(git rev-parse "${TAG}^{commit}")
TAG_SHA="$TAG_SHA" python -c "
import json, glob, os
r = json.load(open(sorted(glob.glob('data/eval_reports/*_comparison.json'))[-1]))
eval_sha = r['header']['code_git_sha']
tag_sha = os.environ['TAG_SHA']
assert eval_sha == tag_sha
print(f'SHA match OK: {eval_sha[:8]}')"

# Freeze whitelist
VIOLATIONS=$(git log "${TAG}..HEAD" --name-only --pretty=format: | grep -v '^$' | grep -vE '^(docs/.*\.md|docs/defense/.*|README\.md)$')
test -z "$VIOLATIONS"
```

---

## Delivery Format

After completing each task, provide:
1. A checklist mapping each exit criterion to DONE/NOT DONE
2. `git diff --cached --stat` output
3. Test results (if any tests were run)
4. List of staged files
5. Any blockers or warnings

Start with T-15. Branch from `feat/full-v1`.
