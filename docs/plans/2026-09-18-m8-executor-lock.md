# M8 Executor Lock (Codex campaign)

**Status:** Binding for dscodex execution. Reviewer/decision-maker: Grok session
on 2026-09-18.

**Authority order:** this lock > recovery design > implementation plan TDD
steps > original M8 cleanup plan.

**Campaign finish line:** Stage-1 attribution quality under a new identity.
Not cleanup. Not FAST-green. Not a tidy RC.

M8-E is **out of this campaign**. Do not start cleanup.

---

## 0. What dscodex can and cannot finish

| Slice | Who | Finish in this campaign? |
|---|---|---|
| M8-0 git lineage | Codex, local git only | Yes |
| M8-A code seams | Codex | Yes |
| M8-A real candidate rebuild + FTS | Codex on CPU, hours, ~10GB disk | Yes, if it does not stall on tests |
| M8-B retrieval gate | Codex, no provider | Yes, after A candidate exists |
| M8-C FTS attribution probe | Codex + operator provider keys | Yes only after user pastes keys / budget |
| M8-D embed / promote / hybrid Stage-1 | Codex **after** user rents GPU | **No** until a later prompt |
| M8-E cleanup/RC | nobody in this campaign | **No** |

dscodex must **not** claim “M8 complete” after code commits. Complete means
M8-B gates pass and M8-C (if authorized) meets recovery gates. M8-D is a
separate prompt.

---

## 1. Speed and test policy (overrides writing-plans TDD)

The implementation plan’s RED/GREEN/commit theater is **not required**.

Do:

- Implement the smallest code that reuses M3 owners.
- One or two focused contract tests per fail-closed rule, written **with** the
  code, not as a 5-step ritual.
- Treat **19/19 SQL**, **Recall@8**, **primary-source hit**, and **non-empty
  `included_evidence_ids`** as the real tests.

Do not:

- Spend the majority of time on pytest.
- Run full `packages/*` suites unless a contract test fails and you need a
  bisect.
- Add factories, extra abstractions, or a second pipeline.
- Reinterpret M7 metrics or weaken citation/materiality.

Required contract tests (cap: six files, keep them short):

1. Source-selection rejects gold keys (`case_id`, `evidence_id`, `gold`, …).
2. Coverage audit classifies FULL_TEXT vs omitted vs served-not-FTS.
3. `prepare` does not take Stage-1 `cases.jsonl`.
4. Retrieval runner never sends expected evidence IDs into
   `retrieve_lexical`.
5. Candidate-FTS adapter never flips `is_current`.
6. Empty citation denominator is `NOT_EXERCISED` and fails recovery gates.

That is enough.

---

## 2. Immutable identities (never write)

| Item | Value |
|---|---|
| M7 tag | `v1.1-m7-stage1` = `d2e2f7f82d3dc4175ec6700ded0c7d59cf59d748` |
| M7 eval_id | `2792d333e7c01eb528fce77b5a54531343e6acdc2633212d2d3315fc97bb32cc` |
| M7 output | `data/eval_reports/v1_1_stage1_327b3eed/` |
| Q-001 DB | `/Users/yiannischen/Desktop/Catalyst/data/snapshots/catalyst_m3_derivative_7a004acc.db` |
| Q-001 SHA-256 | `eb3a37b604f6076a696233619ff71275b9f6fbfd0a84bce1343cb7d3096c080d` |
| served corpus | `f912470487c308df999d2f0f99e8379217aa437100bb9855906f7d58798c33f8` |
| served build | `521e06c3d464397fe847f4122b11c39f4570878838b72cbefdf9f23fd7ccc97a` |
| served dense | `367f678b503ccb349fd1fbe445458644c1789516231dd4489b7a8ca9e4962426` |
| Q-011 candidate build | `bc80d9bc2aa8bd5e32f43c12c3391b6add71ee0c999395618a159304ba6b30ed` (`is_current=0`) |

Do not mutate Q-001. Do not promote Q-011. Do not reuse 162,434 title vectors
or 374,179 as the next embed count.

Filing-body code is **already in M7** (`347ab92` restore filing payloads).
M8-A is a **rebuild**, not a new parser project.

---

## 3. Git lock

Current fact:

```text
Desktop/Catalyst               3037ff8  v1.1/integration     (NO origin branch)
Desktop/Catalyst-v1.1-m7       d2e2f7f  v1.1/m7-stage1-eval  (on origin)
Desktop/Catalyst-v1.1-m8-plan  a660ddb+ v1.1/m8-causal-quality-plan (local docs)
origin has: main, v1.1/m7-stage1-eval
origin does NOT have v1.1/integration
```

M7 is a descendant of integration. Integration is not yet an ancestor of M7.

### 3.1 Cleanliness

Tracked files must be unmodified. These untracked paths are **pre-approved**
and must not be added:

- `scratch/`
- `scripts/arclaude`
- `docs/archive/reviews/Catalyst-factual-architecture-audit.md` (M7 worktree)

Do not stash, reset, or clean those.

### 3.2 M8-0 sequence (local)

Work in `/Users/yiannischen/Desktop/Catalyst` for merges, then create a new
worktree. Never implement in `Catalyst-v1.1-m8-plan`. Never commit on the M7
worktree.

```text
1. cd Desktop/Catalyst
2. confirm no modified tracked files
3. git merge --no-ff v1.1/m7-stage1-eval
4. git merge --no-ff v1.1/m8-causal-quality-plan
5. git merge-base --is-ancestor v1.1-m7-stage1 HEAD   # must pass
6. git branch v1.1/m8-causal-quality-recovery HEAD
7. git worktree add /Users/yiannischen/Desktop/Catalyst-v1.1-m8 \
     v1.1/m8-causal-quality-recovery
```

**Do not `git push` unless the user prompt contains `PUSH_INTEGRATION=yes`.**
First push of `v1.1/integration` creates a new remote branch. Default: local
only. After the implementation worktree exists, `git push -u origin
v1.1/m8-causal-quality-recovery` is allowed so CPU work is not laptop-only.

### 3.3 Freeze

- `Catalyst-v1.1-m7`: read-only.
- `Catalyst-v1.1-m8-plan`: read-only after the merge.
- All code/data work: `Catalyst-v1.1-m8`.

---

## 4. Source-selection policy (the A completeness hole, now closed)

`selection_policy_id`: `general-public-fulltext-v1`

**Include by class, never by gold ID:**

- All SEC primary documents and exhibits already in the new derivative that
  have hash-bound bodies (8-K, 10-Q, 10-K, 6-K, EX-99.x, other exhibits).
- All issuer IR, regulator/government, and cutoff-safe news rows that have
  hash-bound **bodies**. Title/description-only stays `METADATA_ONLY`/`TITLE_ONLY`
  and is not citable.
- Class-based supplement from **local public archives** if the Q-001 copy is
  missing whole document classes. Allowed file sources include Q-011 scratch
  **as a public-document byte archive**, not as a generation. Ingest by
  URL / accession / form type / source_class.

**Forbidden:**

- `case_id`, `evidence_id`, `expected_primary_*`, `oracle_status`, `gold`,
  `label`, `role` in the selection manifest.
- Copying `scratch/q011/selective_ingest_corrected/derivative.db` as the M8
  candidate.
- Setting `is_current=1` on `01e95e20…`.
- Adding the 19 certified IDs to the manifest because they failed an audit.

**19/19 handling:**

1. Seal source-selection and candidate **first**.
2. Then audit the 19 IDs from
   `packages/eval/benchmarks/v1_1/stage1/cases.jsonl` (read-only).
3. If missing, emit a **class gap** (form type / source_class / publisher),
   expand that class generally, rebuild **once**.
4. If still missing after one class expansion, **stop and report**. Do not
   cherry-pick IDs. Do not loop.

Q-011 proved the 19 can exist as FULL_TEXT. A Q-001-only rebuild will miss
documents that were never in Q-001 (at least White House news, UNH EX-99.1,
ORCL EX-99.1). Class supplement is expected, ID supplement is forbidden.

---

## 5. Operator paths for `prepare`

Copy Q-001 to a **new** derivative; never open Q-001 read-write.

| Arg | Value |
|---|---|
| `--derivative` | `/Users/yiannischen/Desktop/Catalyst-v1.1-m8/data/snapshots/catalyst_m8a_derivative_7a004acc.db` (create via copy; gitignored) |
| `--benchmark-manifest` | worktree `data/baseline/benchmark_accessions_v1.json` (DATA-01 parse gate, **not** Q-011 gold) |
| `--q005-approval` | worktree `data/baseline/q005_sec_time_approval_v1.json` |
| `--snapshot-id` | `7a004accb187a17dd5661821913f5b84785af7fe448d6a92331f0bd8aea92f49` |
| `--source-bundle-output-root` | worktree `data/m8a/source_bundle/` |
| `--preparation-evidence` | worktree `data/m8a/preparation_evidence.json` |
| `--expected-implementation-head` | `git rev-parse HEAD` of the M8 worktree |
| `--probe-report-id` / `--postbuild-readiness-id` | Reuse the snapshot-certified pair already bound by M3 for snapshot `7a004acc…`. Read them from existing M3 index_manifest / source-bundle / prepare artifacts. Print the values. Do not mint new IDs. |

Copy command sketch:

```bash
cp -c /Users/yiannischen/Desktop/Catalyst/data/snapshots/catalyst_m3_derivative_7a004acc.db \
  /Users/yiannischen/Desktop/Catalyst-v1.1-m8/data/snapshots/catalyst_m8a_derivative_7a004acc.db
# verify SOURCE sha256 still eb3a37b6… ; never write the source
```

`prepare` only. No `promote`. Active pointer bytes must be unchanged.

Reuse:

- `build_canonical_corpus_records`
- `stage_corpus_candidate`
- `retrieve_lexical(..., inactive_build_id=...)`
- `promote_v1_1_generation.py prepare`

Do not call `run_q011_candidate_pool.py`.

Reconciliation deadline: raise `--reconciliation-deadline-seconds` if 900s is
too low for a full body corpus. Do not silently skip FTS.

---

## 6. Gates (quality, not ceremony)

### M8-A exit (CPU)

- New derivative ≠ Q-001 inode/path.
- Q-001 SHA unchanged.
- Candidate `is_current=0`.
- Filing body chunks > 0.
- `FULL_TEXT` count in candidate ≫ 0.
- Post-seal audit: **19/19** FULL_TEXT, cutoff-valid, FTS-indexed.
- Coverage report + source-selection id committed as sanitized JSON only.

### M8-B exit (CPU, no model)

Reuse `STAGE1_RETRIEVAL_GATES`. Empty denom = fail.

- Recall@8 ≥ 0.75
- primary-source hit ≥ 0.80
- duplicate-adjusted Precision@8 ≥ 0.60
- ticker/cutoff violations = 0
- 9/9 expected-primary cases have non-empty `included_evidence_ids`
- ContextPack carries citable body on those 9

### M8-C exit (provider, no GPU) — only if user authorizes keys

- Analyst called on every case with citable evidence
- ≥ 6/11 gold-answerable non-ABSTAIN
- c01 non-ABSTAIN
- c04 ABSTAIN
- citation correctness 100% on exercised claims
- unsupported primary causal claims = 0
- false SUFFICIENT ≤ 1/12
- new eval_id ≠ `2792d333…`; new output dir ≠ `v1_1_stage1_327b3eed`

### M8-D — later prompt only

GPU only after M8-B and M8-C pass. Embed the **new** `to_embed` set. Then
promote atomically and rerun Stage-1. Targets: ≥ 7/9 gold SUFFICIENT
non-ABSTAIN, status match ≥ 8/12, same citation/unsupported rules.

---

## 7. Stop conditions (report, do not improvise)

- Gold fields in source-selection.
- Q-001 SHA changed.
- `is_current` flipped without M8-D authorization.
- 19/19 still failing after one class expansion.
- M8-B fail: no provider, no GPU.
- M8-C fail: no GPU; inspect retrieval → pack → Analyst, in that order.
- Any request to “just promote Q-011” or loosen citation: refuse.

When stopped, write a short report: identities, counts, missing classes,
exact command, next human decision. Then wait.

---

## 8. Commits

Conventional, focused, explicit `git add <files>`. Never `git add .`.
Do not commit DBs, vectors, raw bodies, or provider logs.
Sanitized reports under `data/baseline/reports/v1_1_m8*` are allowed after
a secret scan.
