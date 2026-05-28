# Live MCJ Runtime Workspace Boundary Plan

> **For implementation:** This boundary plan must be treated as a prerequisite guardrail before writing the full live runtime implementation plan.

**Goal:** Define a safe repository boundary for the live MCJ runtime, LiveRun API, artifact persistence, and runtime console work so implementation does not get mixed with thesis materials, historical reports, nested repos, or old experiment outputs.

**Architecture:** The current root working tree remains a read-only reference source. All implementation work happens in the isolated `feature/live-mcj-runtime-console` worktree. The boundary is enforced by directory classification, branch/worktree isolation, and a strict rule that runtime work may only touch implementation-owned packages and explicitly approved planning documents.

**Tech Stack:** Git worktrees, repository policy in `CLAUDE.md`, Catalyst package boundaries (`packages/agents`, `packages/data-core`, `packages/eval`), planning docs under `docs/plans`

---

### 1. Purpose

This document exists to reduce implementation risk before the live runtime work begins.

The main risk is not lack of design clarity. The main risk is repository boundary pollution:

- live runtime code being mixed with thesis materials
- implementation diffs being buried under historical reports and plans
- nested `DemoUI/` state leaking into new UI work
- runtime outputs being written into existing experiment data areas

This plan defines what can move, what must stay frozen, and what must be handled later as separate cleanup work.

### 2. Working Tree Policy

The root repository at `/Users/yiannischen/Desktop/Catalyst` is now a reference source only for this initiative.

The implementation entrypoint is the isolated worktree:

- `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console`

Rules:

- do not implement live runtime features in the root working tree
- do not clean historical files in parallel with implementation
- do not migrate nested `DemoUI/` changes into the runtime branch by accident
- treat existing root-tree dirt as background context, not as the implementation surface

### 3. Directories Approved For This Implementation

The following directories may be modified as part of the live runtime effort:

- `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/agents/`
- `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/data-core/`
- `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/eval/`
- `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/configs/`
- `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/docs/plans/`

New implementation directories may be introduced only if they are clearly owned by the runtime/API/UI effort and do not reuse ignored or nested repo paths.

If a new API or frontend directory is required, it must be created in the clean worktree and treated as first-class project code, not as an extension of `DemoUI/`.

### 4. Directories Frozen For This Implementation

The following directories are explicitly out of scope during runtime implementation:

- `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/docs/thesis/`
- `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/docs/reports/`
- `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/docs/figures/`
- `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/data/`
- `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/DemoUI/`
- `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/.local/`
- `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/.venv/`
- `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/.pytest_cache/`

Frozen means:

- no cleanup
- no migration
- no refactor
- no opportunistic fixes

### 5. Planning Documents To Keep Active

The following design documents are active inputs to implementation and should be retained in scope:

- `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/docs/plans/2026-05-25-live-mcj-runtime-design-brief.md`
- `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/docs/plans/2026-05-25-live-mcj-runtime-full-design.md`
- `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/docs/plans/2026-05-24-attribution-workbench-frontend-design.md`
- `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/docs/design/frontend-design-feasibility-review.md`
- `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/docs/design/catalyst-thin-backend-design.md`

All other thesis, report, and remediation documents remain available as historical context but are not implementation drivers for this branch.

### 6. `DemoUI/` Policy

`DemoUI/` is reference-only.

Rules:

- do not implement inside `DemoUI/`
- do not attempt to clean its internal git state
- do not move code out of it wholesale
- only read specific files for interaction reference when necessary

If behavior from `DemoUI/` is reused, it must be re-expressed cleanly in the new implementation-owned directory structure.

### 7. `data/` Policy

`data/` is fixture and artifact source, not the runtime implementation surface.

Rules:

- do not clean `data/` as part of this feature
- do not rewrite existing eval or trace outputs in place
- do not use existing scattered experiment outputs as the new product storage contract

If the live runtime needs persistence:

- define new controlled schema
- define a clear owned path or DB contract
- keep that contract separate from legacy eval outputs

### 8. Archive And Cleanup Candidates

The following are recognized as later cleanup or archive candidates, but are not to be touched during runtime implementation:

- root-level `.DS_Store`
- root-level `.dmg` artifacts
- thesis change-list documents outside active implementation scope
- historical deep-dive report bundles
- old experiment packages under `docs/reports/`
- ignored temporary files under `data/`

These belong to a later workspace hygiene effort, not to the runtime branch.

### 9. External Workspace Policy

Directories outside the active worktree, including other worktrees and desktop-adjacent folders, must remain isolated from this effort.

Examples:

- `/Users/yiannischen/Desktop/Catalyst`
- `/Users/yiannischen/Desktop/Catalyst-scheme-c`
- `/Users/yiannischen/Desktop/Catalyst-scheme-c-hotfix`
- `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/`

Rules:

- do not copy implementation artifacts from those locations without explicit review
- do not use them as hidden dependencies
- do not merge their unfinished state into the runtime branch

### 10. Minimal Execution Order

The safe order is:

1. keep the root repository frozen as reference-only
2. treat this boundary plan as active guardrail
3. write the full live runtime implementation plan in the isolated worktree
4. only after the implementation plan is approved, begin code changes
5. postpone repository cleanup and workspace hygiene until after runtime implementation is bounded and stable

### 11. Decision

This branch is now ready for the next planning phase.

The next action is:

- write the formal implementation plan for live runtime/API/UI work inside the isolated worktree

The next action is not:

- cleaning historical documents
- reorganizing old data outputs
- fixing `DemoUI/`
- touching thesis/report material
