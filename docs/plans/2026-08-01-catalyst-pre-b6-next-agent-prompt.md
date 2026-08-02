# Next Agent Prompt: Finish Catalyst Pre-B6

You are taking over Catalyst in the existing local working tree at
`/Users/yiannischen/Projects/Catalyst`.

Use these skills before acting:

- `superpowers:using-superpowers`
- `superpowers:executing-plans`
- `superpowers:test-driven-development`
- `superpowers:systematic-debugging`
- `superpowers:requesting-code-review`
- `superpowers:verification-before-completion`

Read this handoff completely before running or editing anything:

`/Users/yiannischen/Projects/Catalyst/docs/plans/2026-08-01-catalyst-pre-b6-session-handoff.md`

Then read the five binding documents listed in its Section 2. Inspect the actual
code, tests, Git state, database, reports, and CLI help; do not rely only on old
completion reports and do not invent commands or identities.

The current worktree is intentionally dirty and contains required accumulated
Pre-B6 changes. Work in place. Do not reset, checkout, stash, clean, create a new
worktree, or discard any existing change. Do not stage, commit, push, create a PR,
call providers, or run GPU/B6 work.

Your execution objective is:

1. Reproduce and finish the three open streaming-publication findings in Section
   6 of the handoff using strict RED -> GREEN TDD:
   - bounded and resumable corrupt-FTS suffix cleanup, including a fresh GREEN
     verification of the partially implemented fix;
   - bounded lockstep checkpoint validation with no full-range tuple/list or
     `fetchall`, proven with an instrumented large synthetic test and <=500-row
     reads;
   - `after_staging_batch` after every committed batch, including final document
     batches, with one-batch and multi-batch interruption/resume idempotency.
2. Run the focused and expanded verification matrix in Section 7. Treat every
   failure outside the explicitly missing protected-root DB artifacts as a real
   blocker. Preserve the 6 GiB production resource gate.
3. Request a fresh independent read-only code review. The reviewer must reproduce
   the three findings and check same-manifest rebuild isolation, persisted FTS
   corruption detection, reconciliation reasons, legacy API compatibility,
   source identity preservation, and selector-only atomic cutover. Amend and
   re-review until the verdict is APPROVED. Do not self-approve.
4. Only after approval, execute Section 8 through official production entrypoints:
   prebuild audit -> snapshot verification/regeneration -> exactly one streaming
   corpus/FTS publisher -> postbuild audit -> 40/40 corpus and 40/40 lexical
   probes -> promotion -> 12-case lexical baseline -> identity-bound vector-free
   source bundle.
5. Monitor the one publisher about every 15 minutes and wait for completion. On a
   typed interruption, resume the same build identity. Never launch a second
   executor or bypass an error. A status of `PIPELINE IN PROGRESS` is not a final
   result.
6. Stop after verifying the source bundle. Do not upload it to a GPU and do not
   begin embeddings, dense retrieval, RRF, reranking, or attribution experiments.

Before every production transition, record candidate integrity/FK/user_version,
source-table logical hashes, disk, active pointer, running executor count, and
Git status. Never print secrets. Never change source rows or mandatory gates to
make a check pass.

Return the complete evidence report required by Section 10 of the handoff and end
with exactly one decision:

- `READY_FOR_GPU_MANAGER_REVIEW`, or
- `BLOCKED: <specific verified reasons>`.
