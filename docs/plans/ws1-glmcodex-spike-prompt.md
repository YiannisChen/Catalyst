# WS1 Visual Spike — Hand-off Prompt for glmcodex

> Paste the block below to glmcodex. WS1 is scoped to a frontend-only visual spike that ends at a screenshot checkpoint. glmcodex plans first, waits for manager approval, then executes. It does NOT proceed to WS2+ and does NOT touch backend / API / business logic.

---

You are implementing **WS1 (Visual Spike)** of the Catalyst final workbench. You are the worker; a manager reviews your plan and your output. Do not big-bang the project. Do not commit.

## Workflow (binding — do not skip a step)
1. `superpowers:writing-plans` → write the WS1 plan. **Then STOP and wait for manager approval. Do not implement yet.**
2. After the manager approves the plan → `superpowers:executing-plans` → implement the frontend visual spike.
3. Produce the required screenshots (below) → this is the manager checkpoint. Stop here.

## Step 0 — Read before planning
- `docs/plans/2026-06-27-master-ui-design-spec.md` — the whole file. Your binding source of truth is the **Detailed UI Implementation Design Addendum**, especially **A0 (default viewport integrity)**, A1–A6, and A5.4 (modal glance test).
- `apps/workbench/src/components/workbench/v4/` — components to restyle/compose (`WorkbenchHeader`, `MarketSessionPanel`, `SessionOverviewPanel`, `EvidenceIntakePanel`, `ResultWorkspaceTabs` + tabs, `ModelSettings.tsx`, `workbench-v4.css`).
- `apps/workbench/src/components/workbench/LiveWorkbench.tsx` — how the four blocks are assembled.
- `apps/workbench/src/api/types.ts`, `api/client.ts` — the contract. You do NOT change these.

## Scope of WS1 (frontend visual only)
Implement, per the addendum: page layout/grid/spacing, surface hierarchy, typography, **default viewport integrity (A0)**, market-module cohesion (one container; kill the black seam between chart and right-side details), the A3 text deletions, and the three Model Settings modal variants.

## Hard guardrails
- Frontend only. NO backend changes. NO API contract changes. NO credential/business-logic changes. NO new endpoints.
- Do NOT commit. Leave changes in the working tree for manager review.
- 8GB Mac: do NOT run full pytest / LanceDB / vector / full attribution. `npm run build` + frontend unit tests only.
- If a screenshot needs post-run content, use a contract-aligned fixture shaped to `WorkspaceResponse` (do not invent fields). Full post-run parity is WS2 — do only the minimum to render a screenshot here.

## Required deliverables (all of these)
1. **Written WS1 plan** (from step 1, approved before execution).
2. **1440×900 default-viewport screenshot** proving A0 integrity on default load: header + market module (complete chart + details) + candidate news with **≥2 full cards** all complete above the fold; no clipped cards, no half-cut titles, no black seam, no broken composition; workspace may be below the fold.
3. **390px screenshot** proving: no horizontal overflow, no clipped primary controls, and the Model Settings modal fits within the viewport.
4. **Model Settings A/B/C — three modal screenshots, each rendered open OVER the real Catalyst cockpit** (live chart visible behind the scrim), for the A5.4 glance test:
   - A: dark elevated · B: light solid (ValueCell-like) · C: hybrid warm-neutral.
   - Each must let the manager judge: panel independence, input/select/button legibility, backdrop suppressing the background without looking dirty, chart not bleeding into the form, and close/Test/Use-model controls having contrast + adequate touch targets.
5. **A clear note of changed files** + which A10 acceptance items pass + your recommendation among A/B/C (manager + human make the final call).
6. **Build/frontend test result** after execution (`npm run build` + frontend tests). No heavy backend tests.

## Model Settings flow to reflect (visual only in WS1)
Centered modal; portal; header height unchanged on open/close. Flow: provider → API key (password input with eye toggle) → Test Connection (inline loading/success/error) → on success, model select appears → Use model. Changing provider or key resets model readiness. Frontend default credential source = `browser_key` (server_env not surfaced as the default flow).

End at the screenshot checkpoint. Do not start WS2.
