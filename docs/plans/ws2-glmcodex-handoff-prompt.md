# WS2 — Post-run View-Model Parity & Contract Fixture — Hand-off Prompt for glmcodex

> Paste the block below to glmcodex. WS2 is frontend-only and ends with green snapshot/contract tests proving the post-run UI does not deform. glmcodex plans first, waits for manager approval, then executes. It does NOT touch the backend, the API contract, or the modal (modal polish is WS3).

---

You are implementing **WS2 (Post-run view-model parity + contract-aligned fixture)** of the Catalyst final workbench. You are the worker; a manager reviews your plan and your output. Plan first; do not big-bang; do not commit.

## Workflow (binding — do not skip a step)
1. `superpowers:writing-plans` → write the WS2 plan. **Then STOP and wait for manager approval. Do not implement yet.**
2. After the manager approves → `superpowers:executing-plans` → implement.
3. Run the WS2 tests + build → this is the manager checkpoint. Stop here. Do not start WS3/WS4.

## Direction — read this FIRST (this is the whole point of WS2)

The post-run UI deforms today for ONE root reason: the live adapter **fabricates placeholder values** for fields the real API does not provide, so "live" looks half-empty/placeholder while "demo" looks rich. WS2 fixes this the honest way.

**The core principle: reconcile the view-model to what the API contract actually returns. Do not invent data. Do not paper over gaps with placeholders. Where the contract lacks a field, the component degrades gracefully — it hides that element, it does not render an empty string or a fake value.**

Concretely, the current `workspace-adapter.ts` hardcodes placeholders (e.g. `rationale = ''`, `quality = 'medium'`, fixed `supportLevel`/`role`). These exist because the view-model types (borrowed from `mock/demoCases`) carry fields the real `WorkspaceResponse` never had. Check the real contract in `apps/workbench/src/api/types.ts`:

- `WorkspaceCause` = `text, category?, direction?, confidence?, evidence_ids[]` — **there is no `rationale`.** So the Summary must render category/direction/confidence + linked evidence, and NOT show a rationale block when the contract has none.
- `WorkspaceEvidenceItem` = `headline?, snippet?, source_type?, reference_date?, retrieval_score?, rerank_score?, critic_relevance?, critic_category?, critic_reasoning?, critic_decision?, cited`, etc. — **there is no `quality`/`supportLevel`/`role`.** Bind to the real scoring/decision fields instead.
- `critic_decision` may be null → render that item as "ungraded" **per item**; never collapse the whole list to ungraded. (Note: a backend projection bug currently emits "ungraded" even when accepted — that is WS4's job to fix; WS2 must render whatever the contract says, honestly, and not hide the distinction.)

So the right shape of WS2 is: **one shared view-model that is honest about the contract**, the live adapter mapping real fields into it (no fabrication), the demo fixtures conforming to the same view-model, and components that render correctly when optionals are absent.

## Step 0 — Read before planning
- `docs/plans/2026-06-27-master-ui-design-spec.md` — your binding source of truth is addendum **§9 (tab → contract field binding)**, **A7 (fixture variants)**, and main-spec **§14 (post-run fixture)**.
- `apps/workbench/src/api/types.ts` — the real contract (`WorkspaceResponse`, `WorkspaceResult`, `WorkspaceCause`, `WorkspaceEvidenceItem`, `WorkspaceStage`, `WorkspaceDiagnostics`, `WorkspaceFailure`). This is the authority for every field.
- `apps/workbench/src/components/workbench/workspace-adapter.ts` — the adapter to fix (find the hardcoded placeholders and the field drops).
- `apps/workbench/src/mock/demoCases.ts` — where the view-model TYPES currently live (`AttributionResult`, `EvidenceItem`, `PipelineStep`). v4 components import their prop types from here — that coupling is what you will relocate.
- `apps/workbench/src/components/workbench/v4/` — the four post-run tab components (`SummaryTab`, `TraceTab`, `EvidenceTab`, `DiagnosticsTab`) and `ResultWorkspaceTabs`.
- `apps/workbench/src/components/workbench/live-workbench-contract.test.ts` — the existing 60/60 contract test. It MUST stay green.

## Scope of WS2 (your plan should cover these, in your own task breakdown)
1. **Shared view-model module.** Extract the view-model types out of `mock/demoCases` into a neutral module (e.g. a `view-model.ts`) and re-point the v4 components + adapter to import from it. Reconcile the types to the contract: drop or make-optional any field the API cannot supply (`rationale`, `quality`, `supportLevel`, `role`, …).
2. **Honest live adapter.** Rewrite `workspace-adapter.ts` to map real `WorkspaceResponse` fields into the view-model with NO fabricated placeholders. Absent optional → omitted, so the component can hide it.
3. **Graceful components.** Ensure `SummaryTab`/`TraceTab`/`EvidenceTab`/`DiagnosticsTab` render correctly when optionals are absent — no empty-string leakage, no layout deformation. (Visual binding per §9.)
4. **Contract-aligned fixture.** Add a fixture built from faithful `WorkspaceResponse` instances (NOT marketing mock — every field must be something the API could actually return). Put it in a test/dev fixtures location.
5. **Tests.** Add snapshot/render tests that render each fixture variant into the four tabs and assert no deformation + no placeholder leakage, plus a contract assertion that fails if a fixture diverges from the typed `WorkspaceResponse`.

## Fixture variants (A7 — cover all)
1. normal completed attribution (causes + graded evidence)
2. refusal / no result (`output_status` set, empty `causes`)
3. failure (`failure` payload present)
4. empty evidence (`evidence: []`)
5. long text (very long `summary_md`, long headlines — overflow/clamp)
6. many evidence items (scroll behavior)
7. missing optional diagnostics (`diagnostics: null`, null numerics)

## Hard guardrails
- Frontend only. NO backend changes. NO API contract changes (`api/types.ts` is read-only authority — do not edit it). NO business-logic/credential changes. NO modal changes (WS3).
- Do NOT commit. Leave changes in the working tree for manager review.
- 8GB Mac: `npm run build` + frontend unit tests only. No pytest / LanceDB / vector / full attribution.
- Keep `live-workbench-contract.test.ts` green (60/60). If a refactor risks it, verify immediately.
- Do not delete `demoCases.ts` — only relocate the shared types it exports; demo stays as a contract-conformant reference.

## Required deliverables
1. **Written WS2 plan** (from step 1, approved before execution).
2. The shared view-model module + the re-pointed imports.
3. The rewritten, fabrication-free `workspace-adapter.ts`.
4. The contract-aligned fixture covering all 7 variants.
5. Snapshot/contract tests; all green. Report the test counts (before/after).
6. A short note: changed files, which §9/A7/A10 items pass, and any field the contract genuinely cannot supply (so the manager knows what the UI now hides vs shows).
7. `npm run build` + full frontend test suite result (target: all green, including the existing 103 plus the new WS2 tests).

## Acceptance (manager will verify)
- Each of the 7 fixture variants renders into Summary/Trace/Evidence/Diagnostics with **no layout deformation and no placeholder/empty-string leakage**.
- The live adapter no longer fabricates values; absent optionals are omitted and handled gracefully.
- One shared view-model; demo fixtures and the live adapter both conform to it; the contract test fails if a fixture diverges.
- `critic_decision: null` renders "ungraded" per item without collapsing the list.
- `live-workbench-contract.test.ts` still 60/60; build green; console clean.

## Note — not WS2
The WS1 modal cleanups (literal `…` placeholder/text, active-tab box→underline, Test Connection contrast, variant differentiation) are tracked for **WS3**, not WS2. Do not bundle them here.

End at the test checkpoint. Do not start WS3.
