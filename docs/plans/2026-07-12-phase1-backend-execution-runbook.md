# Catalyst Phase 1 — Backend Execution Runbook

**Role:** Execution protocol. Read before any Phase 1 implementation turn.
**Branch:** ws4b/article-level-data
**Date:** 2026-07-12
**Amended:** 2026-07-12 (orchestrator review corrections)

---

## Goal Mode Setup

```
/goal Execute Catalyst Phase 1 Mac backend slices W1-A through W2 in strict
order from the reviewed 2026-07-12 plans; pause after every slice for
orchestrator verification; do not stage, commit, call live providers, print
secrets, or advance past an unverified checkpoint.
```

| Command | When |
|---|---|
| `/goal Execute...` | Start Phase 1 |
| `/goal pause` | Worker completes slice, produces handoff report |
| `/goal resume` | Orchestrator verifies, architect authorizes next slice |
| `/goal` | Inspect current goal status |
| `/goal edit` | Modify objective (rarely) |
| `/goal clear` | Only after Phase 1 complete or abandoned |

---

## Per-Slice Workflow

### A — Pre-Slice Revalidation

1. Read the slice plan from `docs/plans/2026-07-12-w*.md`
2. Inspect all files the plan says to modify — verify they exist
3. If a prior slice changed something the plan assumes unchanged: report, amend plan, do not proceed
4. Record git status + DB SHAs before touching anything

### B — TDD Implementation

Use `superpowers:executing-plans`. Write failing test → verify red → minimal fix → green. Canonical test after every task group: `.venv/bin/python -m pytest packages/data-core -q`

### C — Self-Verification

1. Focused tests green
2. Canonical suite unchanged or improved from baseline (703/0/1/1)
3. Per-slice allowlist check (see below)
4. DB SHAs: Dev unchanged unless slice owns migration; frozen always unchanged
5. No provider artifacts in diff
6. Produce handoff report

### D — Slice Pause

Worker completes exactly one slice. Reports. Issues `/goal pause`. Does NOT advance.

### E — Orchestrator Verification

Independent re-run of canonical suite, diff inspection, DB SHA comparison, migration verification, adversarial checks.

### F — Approval and Resume

Architect authorizes staging/commit. `/goal resume` for next slice.

---

## Per-Slice Allowlist Check

### Purpose

Detect tracked modifications, tracked deletions, staged changes, and untracked
new files that fall outside the slice plan's allowed-file manifest.

### Current tree state

The working tree currently carries unstaged W3-A modifications and untracked
plan/docs files.  The orchestrator must choose one of:

**A (recommended):** Commit W3-A and the plan package before W1-A starts so
the working tree is clean and every subsequent slice diff is unambiguous.

**B:** Record an immutable pre-slice dirty-tree snapshot and attribute only
*new* diffs (relative to that snapshot) to the current slice.

Recommendation A for auditability.  Do not commit without architect
authorization.

### Protocol (clean-tree baseline, option A)

**Pre-slice snapshot** (worker records before touching code):

```
git status --porcelain=v1 -z --untracked-files=all > /tmp/pre_slice_status.txt
git diff --name-only -z > /tmp/pre_slice_tracked_diff.txt
git diff --cached --name-only -z > /tmp/pre_slice_cached_diff.txt
# SHA-256 of every pre-existing dirty tracked path (for change detection)
while IFS= read -r -d "" f; do
  [ -f "$f" ] && shasum -a 256 "$f"
done < /tmp/pre_slice_tracked_diff.txt > /tmp/pre_slice_dirty_shas.txt
```

**Post-slice verification** (worker runs after implementation):

```
git status --porcelain=v1 -z --untracked-files=all > /tmp/post_slice_status.txt
git diff --name-only -z > /tmp/post_slice_tracked_diff.txt
git diff --cached --name-only -z > /tmp/post_slice_cached_diff.txt
```

**Complete allowlist-checker** (run after capturing post-slice snapshots):

The script below compares pre-slice and post-slice snapshots across all
dimensions — tracked unstaged modifications, staged paths, renames, deletes,
untracked paths — plus SHA-256 change detection for paths that were already
dirty before the slice.  Any path outside the per-slice allowlist fails.

```python
import hashlib, os, sys

# ---- per-slice allowlist (from the slice plan Git Boundary section) ----
ALLOWLIST = {
    "packages/data-core/catalyst_data/update_planner.py",
    "packages/data-core/catalyst_data/update_pipeline.py",
    "packages/data-core/tests/test_update_planner.py",
    # ... add remaining allowed paths for this slice
}

# ---- helpers ----
def load_null_delimited(path):
    """Load NUL-delimited path list.  Returns set of bytes."""
    if not os.path.exists(path):
        return set()
    with open(path, "rb") as f:
        data = f.read()
    if not data:
        return set()
    separator = bytes([0])
    return set(data.rstrip(separator).split(separator))

def parse_porcelain_z(path):
    """Parse git status --porcelain=v1 -z output.

    Returns (tracked_paths, untracked_paths) where each path is bytes.
    Handles renames (R), copies (C), and filenames with spaces.
    Does NOT use .strip() on path names.
    """
    tracked = set()
    untracked = set()
    if not os.path.exists(path):
        return tracked, untracked
    with open(path, "rb") as f:
        data = f.read()
    if not data:
        return tracked, untracked
    separator = bytes([0])
    entries = data.rstrip(separator).split(separator)
    i = 0
    while i < len(entries):
        entry = entries[i]
        if len(entry) < 3:
            i += 1
            continue
        status = entry[:2].decode("ascii", errors="replace")
        rest = entry[3:]  # path after status+space
        if status == "??":
            untracked.add(rest)
        elif status[0] in "RAC" or status[1] in "MD":
            # Rename/Copy: next entry is the new path
            # Modified/Deleted: path is in this entry
            path_bytes = rest
            if status[0] in "RC" and i + 1 < len(entries):
                # rename/copy: rest is old path, next entry is new path
                i += 1
                path_bytes = entries[i]
            tracked.add(path_bytes)
        i += 1
    return tracked, untracked

def load_sha_map(path):
    """Load {path_bytes: sha_hex} from shasum output."""
    m = {}
    if not os.path.exists(path):
        return m
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) == 2:
                sha, fpath = parts
                m[fpath] = sha
    return m

# ---- load pre-slice snapshots ----
pre_tracked   = load_null_delimited("/tmp/pre_slice_tracked_diff.txt")
pre_cached    = load_null_delimited("/tmp/pre_slice_cached_diff.txt")
pre_stat_t, pre_stat_u = parse_porcelain_z("/tmp/pre_slice_status.txt")
pre_shas      = load_sha_map("/tmp/pre_slice_dirty_shas.txt")

# ---- load post-slice snapshots ----
post_tracked  = load_null_delimited("/tmp/post_slice_tracked_diff.txt")
post_cached   = load_null_delimited("/tmp/post_slice_cached_diff.txt")
post_stat_t, post_stat_u = parse_porcelain_z("/tmp/post_slice_status.txt")

# ---- compute pre/post path sets ----
pre_all  = pre_tracked | pre_cached | pre_stat_t | pre_stat_u
post_all = post_tracked | post_cached | post_stat_t | post_stat_u

# ---- pre-existing dirty paths (unchanged by this slice) ----
pre_existing = {p.decode() if isinstance(p, bytes) else p for p in pre_all}
print(f"Pre-existing dirty paths (not from this slice):")
for p in sorted(pre_existing):
    print(f"  {p}")

# ---- newly introduced paths ----
new_paths = post_all - pre_all
new_str = {p.decode() if isinstance(p, bytes) else p for p in new_paths}

if new_str:
    print(f"\nNewly introduced paths ({len(new_str)}):")
    for p in sorted(new_str):
        print(f"  {p}")
else:
    print("\nNo newly introduced paths detected.")

# ---- content-change detection for pre-existing dirty paths ----
changed_pre_existing = []
for p_bytes in pre_tracked & post_tracked:
    p_str = p_bytes.decode() if isinstance(p_bytes, bytes) else p_bytes
    if not os.path.isfile(p_str):
        continue
    with open(p_str, "rb") as f:
        current_sha = hashlib.sha256(f.read()).hexdigest()
    # Try path-as-key first, then raw bytes
    prev_sha = pre_shas.get(p_str) or pre_shas.get(p_bytes.decode() if isinstance(p_bytes, bytes) else p_bytes)
    if prev_sha and prev_sha != current_sha:
        changed_pre_existing.append((p_str, prev_sha[:12], current_sha[:12]))

if changed_pre_existing:
    print(f"\nContent changes to pre-existing dirty paths ({len(changed_pre_existing)}):")
    for p_str, old_s, new_s in changed_pre_existing:
        print(f"  {p_str}  {old_s}... -> {new_s}...")
    # These must be in the allowlist too
    unexpected_content = [p for p, _, _ in changed_pre_existing if p not in ALLOWLIST]
    if unexpected_content:
        print(f"FAIL: content-changed path(s) outside allowlist:")
        for p in unexpected_content:
            print(f"  {p}")
        sys.exit(1)

# ---- allowlist check for newly introduced paths ----
unexpected = [p for p in new_str if p not in ALLOWLIST]
if unexpected:
    print(f"\nFAIL: {len(unexpected)} new path(s) outside allowlist:")
    for p in sorted(unexpected):
        print(f"  {p}")
    sys.exit(1)

print(f"\nAll paths in allowlist.")
```

**Pre-existing dirty paths** (diffs present in both pre- and post-slice
snapshots) are reported separately but not attributed to the slice.

Each slice plan's §Git Boundary lists the exact allowlist.

---

## Database Safety

### Real Dev DB

- Never migrated or updated during automated implementation tests
- Code verification uses fixtures/temp DBs
- Migration verification uses temp DB copies
- Later explicitly authorized operator update of Dev DB is separate

### Frozen DB

- Never writable
- SHA checked before/after every slice
- No migrations, no copied replacement
- `FROZEN_PATHS` guard active

### WAL/SHM Safety

- Never delete sidecars to manufacture a stable SHA
- Ensure all known project processes are stopped before recording
- Inspect sidecars: `ls -la data/catalyst_dev_ws4b.db*`
- If active WAL exists, report and stop — do not checkpoint or mutate to calculate hash
- Before W1-F, backup uses safe copy method (not `scripts/update.py backup` which doesn't exist yet)
- Fixture-copy strategy: `sqlite3 source.db ".backup dest.db"` or equivalent safe API

---

## Live Provider Policy

Automated Goal checkpoints contain **zero live provider calls**.

Live OHLCV smoke (1 ticker × 3 days) is:
- Optional
- Separately authorized
- Outside the automated Goal
- Documented in a separate validation section

W7 (Phase 2) owns required real-provider validation.

---

## W3-A Commit

W3-A commit boundary is an architect Git decision (`AD-GIT-1`), not a technical implementation gate. W3-A modifications may remain unstaged until explicitly authorized.

---

## Per-Slice Checkpoints

### W1-A
**Allowlist:** `update_planner.py`, `update_pipeline.py`, `test_update_planner.py`
**DB:** Both SHAs unchanged
**Dynamic check:** Zero-write proof against DB copy

### W1-B
**Allowlist:** `config.py`, `trading_calendar.py`, `update_planner.py`, `test_update_planner.py`, `test_trading_calendar.py`
**DB:** Both SHAs unchanged
**Dynamic check:** Per-ticker watermarks derived at execution time (do not hardcode `2025-05-02`)

### W1-C
**Allowlist:** `polygon.py`, `sqlite.py`, `fallback.py`, `update_pipeline.py`, `migrations.py`, `test_ohlcv_execution.py`
**DB:** Dev unchanged (migration tested on temp copy); frozen unchanged

### W1-D
**Allowlist:** `update_service.py`, `update_pipeline.py`, `quality.py`, `migrations.py`, `run_report.py`, `test_update_service.py`
**DB:** Dev unchanged (migration tested on temp copy); frozen unchanged

### W1-E
**Allowlist:** `quality.py`, `update_pipeline.py`, `update_service.py`, `migrations.py`, `doctor.py`, `test_durable_control.py`
**DB:** Dev unchanged (migration tested on temp copy); frozen unchanged

### W1-F
**Allowlist:** `scripts/update.py`, `docs/runbooks/update-operations.md`, `backfill.py`, `test_update_cli.py`
**DB:** Both SHAs unchanged

### W3-B
**Allowlist:** Per-group test files and fix targets. Full package green required.

### W2
**Allowlist:** `certification.py`, `certify.py`, `test_certification.py`
**DB:** Both SHAs unchanged

---

## Forbidden Actions (All Slices)

| Action | Reason |
|---|---|
| Stage/commit without authorization | All slices |
| Print or inspect .env / API keys | Secret safety |
| Open frozen DB writable | FROZEN_PATHS |
| Stage provider_discovery/ or provider_probe/ | Out of scope |
| Modify catalyst_agents/retrieval/policy.py | W1 boundary |
| HTTP API or frontend | Phase 7–8 |
| Live provider calls in automated tests | Mocked connectors |
| --rootdir or alternative venv | Canonical command |
| Silently weaken assertions | Must cite group ID |
| Proceed past unverified checkpoint | Serialized |

---

## Commit Authorization

1. Never stage/commit without explicit architect authorization
2. When authorized: commit message exactly as in plan
3. Per-slice commits follow roadmap §H chain
4. Re-run canonical suite after every commit
