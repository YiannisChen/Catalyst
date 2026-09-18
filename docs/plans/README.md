# Active Plans and Contracts

This directory holds binding technical contracts, package implementation plans,
and the current operator runbook. Superseded session prompts and handoffs are
under `docs/archive/`.

## Start here

| Priority | Document | Role |
| --- | --- | --- |
| 0 | `2026-09-18-m8-executor-lock.md` | Binding Codex/operator lock: paths, source policy, test cap, git, quality gates |
| 0.1 | `2026-09-18-m8-dscodex-prompt-guide.md` | Short copy-paste prompts P0–P5; one at a time |
| 0.2 | `2026-09-18-catalyst-v1.1-m8-causal-quality-recovery-design.md` | Approved M8 ordering and architecture amendment after the sealed M7 attribution failure |
| 0.5 | `2026-09-18-catalyst-v1.1-m8-causal-quality-recovery.md` | Executable M8-0/A–D plan; TDD steps optional under the executor lock |
| 0.75 | `2026-08-19-catalyst-v1.1-m8-cleanup-release-candidate.md` | M8-E cleanup plan; blocked until M8-D; out of the first Codex campaign |
| 1 | `2026-08-09-catalyst-post-gpu-handoff.md` | Historical verified state after the earlier GPU embedding/import |
| 2 | `2026-08-06-b6-g-cloud-execution-runbook.md` | Historical GPU embedding + LanceDB import operator playbook |
| 3 | `2026-08-08-post-import-completion-plan.md` | Historical post-import plan (INDEX_READY_B6_INCOMPLETE; MANAGER_PLAN_COMPLETE → Wave1 T1–T3) |
| 4 | `2026-08-06-protected-artifact-ci.md` | Protected DB marker / strict CI contract |
| 5 | `2026-07-21-b2-b7-technical-contracts.md` | Cross-package binding contracts |
| 6 | `2026-07-21-catalyst-roadmap.md` | High-level B2–B7 roadmap |

The older B2-B7 handoff/runbooks remain historical and subsystem authorities,
but they are not the current milestone entry point. Current filesystem commands
use `/Users/yiannischen/Desktop/Catalyst` and its Desktop-rooted milestone
worktrees; historical `/Users/yiannischen/Projects/...` paths are provenance
only.

## Package plans (B2–B7)

| Document | Package |
| --- | --- |
| `2026-07-22-b2-data-update-provenance.md` | B2 |
| `2026-07-23-b2o-data-readiness.md` (+ design sibling) | B2-O |
| `2026-07-22-b3-corpus-chunking.md` | B3 |
| `2026-07-22-b4-cutoff-safe-lexical-retrieval.md` | B4 |
| `2026-07-22-b5-attribution-runtime-assurance.md` | B5 |
| `2026-07-22-b6-dense-reranker.md` | B6 |
| `2026-07-22-b7-api-evaluation-release.md` | B7 |

## Pre-B6 / Pre-GPU evidence

| Document | Role |
| --- | --- |
| `2026-07-29-pre-b6-evidence-convergence-design.md` | Design |
| `2026-07-29-pre-b6-evidence-convergence.md` | Execution plan |
| `2026-08-01-pre-b6-streaming-publication-design.md` | Streaming publication design |
| `2026-08-01-pre-b6-streaming-publication.md` | Streaming publication plan |

## Architecture roots (Jul 19)

Keep for portfolio and architecture narrative; do not treat as the live
execution checklist:

- `2026-07-19-catalyst-open-source-workbench-design.md`
- `2026-07-19-catalyst-final-package-architecture.md`
- `2026-07-19-catalyst-evaluation-architecture-review.md`
- `2026-07-19-catalyst-provider-provenance-and-chunking-design.md`
- `2026-07-21-b2-b7-final-adversarial-review.md`
- `2026-07-22-b2-b7-pre-execution-ratification.md`
