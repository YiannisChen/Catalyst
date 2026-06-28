# WS1 Visual Spike Implementation Plan

> **For the executor:** Use `superpowers:executing-plans` to implement this plan task-by-task — but ONLY after the manager approves this plan. Do NOT begin execution until approved. Do NOT commit (WS1 guardrail).

**Goal:** Frontend-only visual rescue of the Catalyst workbench so the default 1440×900 viewport is compositionally whole (A0), the market module reads as one cohesive container, invalid labels are deleted, and the Model Settings modal is presented in three glance-test variants (A/B/C) over the real cockpit — proven by screenshots.

**Architecture:** Pure CSS + component-composition changes inside `apps/workbench`. No new dependencies. The four-block vertical stack (header → market module → candidate news → workspace) gets a binding vertical budget and a centered max-width shell. The market chart and session details merge into one L1 container with an internal hairline divider. The Model Settings modal gains a `data-variant` switch so A/B/C can be rendered over the live chart without code edits between shots. All modal tokens move to a scope the portaled panel actually inherits.

**Tech Stack:** React 19, TypeScript 5.9, Vite 7, D3 candlestick, plain CSS custom properties. Tests via Node's built-in runner (`node --experimental-strip-types --test`), Node v22.13.1.

---

## 0. Binding context (read before executing)

- **Source of truth:** `docs/plans/2026-06-27-master-ui-design-spec.md`, especially the Detailed UI Implementation Design Addendum — A0 (default viewport integrity), A1–A6, A5.4 (modal glance test), A10 (acceptance checklist). Structural values (spacing/type/dimensions) are binding; color values are direction, locked only after the A5.4 glance test.
- **Hand-off prompt:** `docs/plans/ws1-glmcodex-spike-prompt.md` — defines the screenshot checkpoint and guardrails.
- **This plan contains no code blocks** (project convention: plans express design as tables, measurements, file paths, and commands).

### 0.1 Current-state findings (verified by inspection — these drive the tasks)

1. **No page shell in the live path.** `LiveWorkbench.tsx` renders `<div className="v4-page">` with the four blocks directly inside. The `.v4-shell` rule (max-width 1600, horizontal padding, grid) exists in CSS but is NOT used by `LiveWorkbench`. Result: no centered max-width cap, no consistent 24px page padding → content can touch edges. This is an A0 integrity defect.
2. **Market-module class mismatch.** Components emit `v4-market-composite`, `v4-market-chart-col`, `v4-market-session-col`, `v4-market-chart-header`, `v4-market-chart-title`. The CSS instead defines `v4-market-row` (grid 2fr/0.85fr) — a different class name that no component uses. So the market composite has **no matching layout rules**; chart and details render as unstyled siblings with page canvas between them — the structural root of the "black seam" / disconnected composition.
3. **Modal tokens are out of scope of the portal.** `--v4-modal-*` variables are declared on `.v4-page`, but `ModelSettings` portals the dialog to `document.body` (outside `.v4-page`). The portaled panel does not inherit those variables. The current modal panel is already light (`--v4-modal-bg: #f8fafc`) — i.e. the current state already corresponds to **Option B (light solid)**. A and C do not exist yet.
4. **Surface blend.** `--v4-bg #060912`, `--v4-surface #0b111c`, `--v4-surface-elevated #0d1520`. The elevated level sits only ~3 L* above canvas — the modal-blend bug source. The L0→L1→L2→L3 ladder does not yet meet the ≥~10 L* step rule.
5. **Header height mismatch.** CSS `.v4-header` is 64px; spec A2.1/A6.1 binds header at 56px. The vertical budget assumes 56px.
6. **Font.** CSS uses `Inter`; spec A3/§5 binds "not Inter" (Geist or IBM Plex Sans + a tabular mono). See Task 8 for the scoped, reversible handling.
7. **Copy test is already red on exactly the A3 delete targets.** Baseline (`node --experimental-strip-types --test`): `workbench-copy.test.ts` = 11 tests, 7 pass, **4 fail**. The 4 failures assert presence of labels WS1 must delete ("Ticker", "Session Overview", "Evidence Intake", "Analysis"). All other frontend test files are green: `live-workbench-contract.test.ts` 60/60, `trace-redaction.test.ts` 20/20, `workbench-model.test.ts` 3/3, `workflow-state.test.ts` 3/3, `price-chart-source.test.ts` 2/2, `demoCases.test.ts` 4/4. **Baseline total: 103 tests, 99 pass, 4 fail (all in copy test, all A3-related).**

---

## 1. Task breakdown

Each task is bite-sized and reversible. **No commits** — leave all changes in the working tree for manager review. Verify per task with the listed command.

### Task 1 — Page shell + A0 vertical/horizontal budget

**Files:** `apps/workbench/src/components/workbench/v4/workbench-v4.css`; `apps/workbench/src/components/workbench/LiveWorkbench.tsx`

**Change:**
- Introduce a page shell wrapper around the four blocks in `LiveWorkbench` (or apply shell rules to a dedicated container) so the 1440 canvas gets: max-width 1600px centered, 24px horizontal padding, 20px top padding, 16px inter-block vertical gaps. Map to the binding vertical budget: page top 20 → header 56 → gap 16 → market module 380 → gap 16 → candidate news 200 → gap 16 → workspace (begins, header peeks below fold).
- Give the stack a single 4px-based rhythm; remove any ad-hoc margins off the scale.
- Ensure the workspace is allowed to flow below the fold intact; blocks 1–3 must never render half-cut at the fold.

**Verify:** `cd apps/workbench && npm run dev`; at 1440×900 the four blocks stack with even gaps and 24px side padding; no block is clipped at the fold; workspace header peeks. (Screenshot captured in Task 10.)

### Task 2 — Header reorg + A3 text cleanup (header)

**Files:** `apps/workbench/src/components/workbench/v4/WorkbenchHeader.tsx`; `apps/workbench/src/components/workbench/v4/workbench-v4.css`

**Change:**
- Wordmark = "Catalyst" only (delete any "Workbench" suffix). Header height → 56px (reconcile CSS 64px → 56px). Controls gap 12px.
- Remove visible "Ticker"/"Session" text labels; the ticker select's displayed value IS the label (e.g. "AAPL"); the date control shows the trade date (e.g. "2025-09-02"). Both 36px desktop height.
- Run Attribution = primary; Model Settings = icon+label with status dot. Opening Model Settings must not change header height (portal isolation — confirm in Task 7).
- Apply A3 delete list to the header only here (chart/panel eyebrows follow in Tasks 3–5).

**Verify:** `cd apps/workbench && npm run dev`; header reads "Catalyst" + self-evident controls, 56px tall, no "Workbench"/"Ticker"/"Session" text labels.

### Task 3 — Market module cohesion (kill the black seam)

**Files:** `apps/workbench/src/components/workbench/v4/workbench-v4.css`; `apps/workbench/src/components/workbench/v4/MarketSessionPanel.tsx`; `apps/workbench/src/components/workbench/v4/SessionOverviewPanel.tsx`; `apps/workbench/src/components/workbench/LiveWorkbench.tsx`

**Change:**
- Reconcile the class mismatch (finding #2): make `v4-market-composite` the ONE L1 container (24px padding, radius 10px, single subtle elevation) holding chart + details. Either rename the dead `v4-market-row` rule to `v4-market-composite` or add the composite rule and drop the unused one — pick one, do not leave both.
- Internal split: chart region ~66% (≈8/12), details region ~34% (≈4/12), separated by ONE hairline vertical rule (low-alpha, L1-on-L1) with a ~20–24px gutter each side — NOT a gap exposing page canvas.
- Chart title row: "AAPL" (ticker, 18/600) + date (13/500 muted) + last price + change% (semantic up/down) inline. **Delete "Daily Price"** and any "Market Session" eyebrow.
- Session details become a compact stat list (open, high, low, close, volume, change%, range): label muted-left, value tabular-right. Move `SessionOverviewPanel` content visually inside the market container; **delete the "Session Overview" eyebrow**. Keep readiness/event-window rows in the same rhythm.

**Verify:** `cd apps/workbench && npm run dev`; chart + details read as one panel with a single hairline divider, no black seam, no canvas gap, no "Daily Price"/"Market Session"/"Session Overview" text.

### Task 4 — Candidate news rail (always complete)

**Files:** `apps/workbench/src/components/workbench/v4/EvidenceIntakePanel.tsx`; `apps/workbench/src/components/workbench/v4/workbench-v4.css`

**Change:**
- Keep the heading "Candidate news" (meaningful — do not delete). In preview mode the title is "Candidate news"; do not show a relevance score pre-run.
- Card rail: `auto-fit minmax(280px,1fr)`; 3–4 cards visible at 1440. Card: min-height 120px, 16px padding, radius 8px. Content order: source tag (11/500 muted) · date (11/500 muted) · headline (14/600, clamp 2) · optional snippet (13/400, clamp 2).
- Ensure **≥2 full cards** render complete within the 200px news budget at 1440×900 (A0 hard requirement). If space is tight, the workspace moves down — news is never truncated.
- Designed empty state ("No candidate news for this date") and loading skeleton (3–4 cards at real dimensions) — keep/align to A1.

**Verify:** `cd apps/workbench && npm run dev`; at 1440×900 the news section shows ≥2 complete cards above the fold; no clipped card; heading "Candidate news".

### Task 5 — Workspace block (below fold, eyebrow cleanup)

**Files:** `apps/workbench/src/components/workbench/v4/ResultWorkspaceTabs.tsx`; `apps/workbench/src/components/workbench/v4/workbench-v4.css`

**Change:**
- Workspace is block 4; it may begin below the fold but its header must either start completely or fall naturally to the next screen — no half-cut title (A0).
- Keep the panel title "Attribution Workspace". **Delete the "Analysis" eyebrow** (A3). Tab bar 40px, tabs 36px, active = 2px accent underline (not a box).
- Pre-run empty state: left-aligned prompt ("Run attribution to generate a grounded explanation") referencing the Run button — not a blank panel, not centered (§12).
- Do NOT change tab data binding, the contract field mapping, or any business logic. Visual only.

**Verify:** `cd apps/workbench && npm run dev`; workspace header is not half-cut; no "Analysis" eyebrow; pre-run empty state is left-aligned.

### Task 6 — Surface hierarchy tokens (L0–L3, ≥10 L* steps)

**Files:** `apps/workbench/src/components/workbench/v4/workbench-v4.css`

**Change:**
- Define four perceptibly-separated elevation levels per A4.1: L0 canvas, L1 panel (market/news/workspace), L2 card/input/active-tab, L3 overlay (modal/dropdowns). Each level ≥~10 L* above the one below (fixes finding #4).
- Borders: hairline, low-alpha, reinforcing separation that fill contrast already implies — not a wireframe border on every panel. Shadows reserved for L3 overlays (+ at most one subtle lift on the market hero).
- Directional dark-cockpit values as the working target (canvas ~#0a0f1a, panel ~#121a28, card ~#1a2435); finalize only after the A5.4 glance test. Do not lock final hex in this task.

**Verify:** `cd apps/workbench && npm run dev`; panels, cards, and the modal read as distinct surfaces by fill contrast first. (Modal separation judged in Task 7 / Task 10.)

### Task 7 — Model Settings modal A/B/C variants + portal scope fix

**Files:** `apps/workbench/src/components/workbench/v4/ModelSettings.tsx`; `apps/workbench/src/components/workbench/v4/workbench-v4.css`

**Change:**
- **Portal scope fix (finding #3):** move `--v4-modal-*` tokens to a scope the portaled panel inherits (`:root`, or declare them on `.v4-modal-panel` itself). Confirm the panel renders its intended background, not a fallback.
- **Variant switch:** add a `data-variant` attribute (`a` | `b` | `c`) on `.v4-modal-panel`. Define three token sets:
  - **A — dark elevated, high-contrast:** clearly-raised dark panel ≥~10 L* above canvas, hairline light border, strong blurred scrim, real shadow.
  - **B — light solid (ValueCell):** near-white panel, dark text, visible input borders, shadow-lg, ~0.6–0.7 black backdrop (this is essentially the current `#f8fafc` state — formalize it as variant B).
  - **C — hybrid warm-neutral:** desaturated graphite/slate panel lighter than canvas, off-white text (Linear-modal register).
- Keep modal UX identical across variants: portal-rendered, centered, `aria-modal`, ESC + backdrop close, body scroll lock, header height stable on open/close. Flow unchanged (Provider → API key w/ eye toggle → Test Connection → model select on success → Use model). **No business-logic / API / credential-flow changes** — `credential_source: 'browser_key'` stays the default; `server_env` remains hidden.
- Generous padding (~24px), field gap ~16px, input height 36px, focus rings on all controls.

**Verify:** `cd apps/workbench && npm run dev`; toggle each `data-variant` and open the modal — panel is a single independent surface, scrim suppresses the chart without looking dirty, inputs/buttons legible, header height unchanged. (Glance-test screenshots in Task 10.)

### Task 8 — Responsive (390px + 768px)

**Files:** `apps/workbench/src/components/workbench/v4/workbench-v4.css`

**Change:**
- **390px:** single column; chart stacks above details (still one container); news 1 column; header controls collapse to two rows (ticker+date row, Run + Model Settings row); all targets ≥44px; **no horizontal overflow**; Model Settings modal fits within viewport (`max-width: calc(100% - 2rem)`).
- **768px tablet:** market details wrap below the chart (same container); news 2 columns; header controls may wrap; controls grow toward 44px.
- **Font handling (scoped, reversible):** spec binds "not Inter." For the spike, switch the UI family to `IBM Plex Sans`/`Geist` with `system-ui` fallback and numerics to `IBM Plex Mono`/`Geist Mono` with tabular-nums — but only via self-hosted woff2 in `apps/workbench/public` (no new runtime dependency). If self-hosting assets is blocked on the 8GB Mac, fall back to `system-ui` for the spike and defer the exact typeface to WS3. A0 integrity and the modal glance test do NOT depend on the exact typeface; this task is non-blocking.

**Verify:** `cd apps/workbench && npm run dev`; at 390px width no horizontal scroll, no clipped primary controls, modal fits. (390px screenshot in Task 10.)

### Task 9 — Update `workbench-copy.test.ts` to the A3 copy (frontend test only)

**Files:** `apps/workbench/src/components/workbench/workbench-copy.test.ts`

**Change:**
- The 4 currently-failing tests assert presence of the exact labels A3 deletes ("Ticker", "Session Overview", "Evidence Intake", "Analysis"). Invert them to assert the NEW copy and the absence of deleted labels: header contains "Catalyst" and "Run Attribution" and does NOT contain "Workbench"/"Ticker"/"Session" labels; SessionOverviewPanel does NOT contain "Session Overview"; EvidenceIntakePanel contains "Candidate news" and does NOT contain "Evidence Intake"; ResultWorkspaceTabs contains "Attribution Workspace" and does NOT contain "Analysis".
- Keep all other copy-test assertions (deprecated-label ban, no-emoji, criticDecision model checks) intact. This is a test-expectation update aligned to the A3 spec — **not** a business-logic or contract change.

**Verify:** `cd apps/workbench && node --experimental-strip-types --test src/components/workbench/workbench-copy.test.ts` → 11/11 pass.

### Task 10 — Screenshot capture (the manager checkpoint)

**Prerequisite:** dev server + lightweight backend endpoints only (tickers / ohlcv / news / model catalog) for the live chart behind the modal. No attribution run, no LanceDB, no embeddings (8GB guardrail). If the backend is unavailable, fall back to the demo path — but prefer the live cockpit per the spec ("live chart visible behind the scrim"). Post-run workspace content is WS2; for the 1440×900 default-load shot use the pre-run state (no fixture needed).

**Capture:**
1. **1440×900 default-viewport screenshot** (A0 proof): default load/refresh — header + complete market module (chart + details, one container) + candidate news with ≥2 full cards, all complete above the fold; no clipped cards, no half-cut titles, no black seam; workspace may be below the fold.
2. **390px screenshot:** no horizontal overflow, no clipped primary controls, Model Settings modal fits within viewport.
3. **Model Settings A/B/C — three modal screenshots, each open OVER the real cockpit** (live chart visible behind the scrim), one per `data-variant`, for the A5.4 glance test. Evaluate each against the full glance-test checklist: panel independence, input/select/button legibility, backdrop suppresses background without looking dirty, chart does not bleed into the form, close/Test/Use-model controls have contrast + adequate touch targets.

**Deliver alongside screenshots:** a changed-files list, which A10 items pass, and a recommendation among A/B/C (manager + human make the final call; tokens lock only then).

**Verify:** all five screenshots exist and demonstrate the A0/A5.4 acceptance criteria.

### Task 11 — Build + frontend test verification

**Commands (run from `apps/workbench`):**
- Build: `npm run build` (=`tsc -b && vite build`) — must pass with zero TS errors.
- Frontend tests: `node --experimental-strip-types --test src/components/workbench/workbench-copy.test.ts src/components/workbench/live-workbench-contract.test.ts src/components/workbench/v4/trace-redaction.test.ts src/components/workbench/workbench-model.test.ts src/components/workbench/workflow-state.test.ts src/components/workbench/price-chart-source.test.ts src/mock/demoCases.test.ts` — target 103/103 green.
- Browser console clean at 1440 and 390.

**Verify:** build green; all frontend tests green (was 99/103 baseline; copy-test update in Task 9 brings it to 103/103). No heavy backend/pytest/LanceDB run on the 8GB Mac.

---

## 2. Frontend files involved

**Modify (visual only):**
- `apps/workbench/src/components/workbench/v4/workbench-v4.css` — primary: shell, vertical budget, market composite, surface hierarchy, modal A/B/C tokens + portal scope, responsive.
- `apps/workbench/src/components/workbench/LiveWorkbench.tsx` — page shell wrapper, market composite structure, four-block stack.
- `apps/workbench/src/components/workbench/v4/WorkbenchHeader.tsx` — Catalyst wordmark, 56px, self-evident controls, A3 deletes.
- `apps/workbench/src/components/workbench/v4/MarketSessionPanel.tsx` — one-container chart region, ticker+price+date title, delete "Daily Price".
- `apps/workbench/src/components/workbench/v4/SessionOverviewPanel.tsx` — stat rows inside market container, delete "Session Overview" eyebrow.
- `apps/workbench/src/components/workbench/v4/EvidenceIntakePanel.tsx` — keep "Candidate news", ≥2 full cards, rail.
- `apps/workbench/src/components/workbench/v4/ResultWorkspaceTabs.tsx` — below-fold workspace, delete "Analysis" eyebrow, keep "Attribution Workspace".
- `apps/workbench/src/components/workbench/v4/ModelSettings.tsx` — `data-variant` switch on panel; no logic/API/credential-flow change.
- `apps/workbench/src/components/workbench/workbench-copy.test.ts` — align copy assertions to A3 (frontend test only).
- `apps/workbench/src/components/workbench/DemoWorkbench.tsx` — apply the same market-composite class/structure changes for parity (it shares `v4-market-composite`); visual only.

**Explicitly NOT touched (contract / business logic / backend):**
- `apps/workbench/src/api/types.ts`, `apps/workbench/src/api/client.ts` — API contract, unchanged.
- `apps/workbench/src/components/workbench/workspace-adapter.ts` — view-model/field parity is WS2, not WS1.
- `apps/workbench/src/mock/demoCases.ts` — data model, unchanged.
- Any backend: `packages/app`, `catalyst_app`, `packages/*`, `scripts/*` — out of scope.

---

## 3. Tests & builds run after execution (and only these)

| Check | Command (from `apps/workbench`) | Target |
|---|---|---|
| Type-check + bundle | `npm run build` | green, 0 TS errors |
| Copy test (A3-aligned) | `node --experimental-strip-types --test src/components/workbench/workbench-copy.test.ts` | 11/11 (was 7/11) |
| Contract test | `node --experimental-strip-types --test src/components/workbench/live-workbench-contract.test.ts` | 60/60 (must stay green) |
| Trace redaction | `node --experimental-strip-types --test src/components/workbench/v4/trace-redaction.test.ts` | 20/20 |
| Workbench model | `node --experimental-strip-types --test src/components/workbench/workbench-model.test.ts` | 3/3 |
| Workflow state | `node --experimental-strip-types --test src/components/workbench/workflow-state.test.ts` | 3/3 |
| Price-chart source | `node --experimental-strip-types --test src/components/workbench/price-chart-source.test.ts` | 2/2 |
| Demo cases | `node --experimental-strip-types --test src/mock/demoCases.test.ts` | 4/4 |

**Aggregate target: 103/103 frontend tests green + build green.** No pytest, no LanceDB, no embeddings, no attribution, no server-only tests on the 8GB Mac.

---

## 4. Guardrails (binding)

- **Frontend only.** No backend changes, no API contract changes, no new endpoints, no business-logic changes, no credential-flow changes. `credential_source: 'browser_key'` stays the default; `server_env` stays hidden.
- **No commits.** Leave all changes in the working tree for manager review. Do not run `git commit`/`git push` (per WS1 + project git policy; stage only if needed, never commit).
- **No heavy runs.** 8GB Mac: `npm run build` + frontend unit tests only. No pytest / LanceDB / vector / full attribution.
- **Contract-aligned only if needed.** If a screenshot needs post-run content, use a fixture shaped to `WorkspaceResponse` (no invented fields). Post-run parity is WS2 — do the minimum to render a screenshot here. The 1440×900 default-load shot needs no fixture (pre-run state).
- **Stop at the screenshot checkpoint.** Do not start WS2.

---

## 5. Acceptance mapping (A10)

- A0 default viewport integrity — proven by the 1440×900 screenshot (Task 10.1).
- Chart + details one cohesive module, no black seam — Task 3 + screenshot.
- No invalid section eyebrows remain (A3 list applied) — Tasks 2–5; verified by Task 9.
- Model Settings modal clearly distinct from background, form readable, chart not bleeding — Task 7 + Task 10.3.
- Header does not deform when modal opens — Task 7 (portal isolation).
- Consistent spacing/padding per A2 — Tasks 1–6.
- 390px no horizontal overflow — Task 8 + Task 10.2.
- Browser console clean — Task 11.
- `npm run build` + frontend tests pass — Task 11.
- No server-only heavy tests on the 8GB Mac — guardrails §4.

---

## 6. Risks & open decisions

- **Copy-test coupling:** the 4 red baseline tests are exactly the A3 delete targets; Task 9 must land with the text cleanup or the suite stays red. Low risk (frontend test-only).
- **Contract test sensitivity:** `live-workbench-contract.test.ts` (60/60) must remain green; avoid touching `workspace-adapter.ts` and any field mapping. If a visual change risks it, verify immediately.
- **Modal token scope:** the portal-to-body bug (finding #3) must be fixed or variants A/C may render with fallback backgrounds. Verify the panel actually receives its token set.
- **Screenshot prerequisite:** modal/390 shots need the live chart → lightweight backend endpoints or demo fallback. Confirm availability before capture.
- **Font assets:** self-hosting Geist/IBM Plex may be blocked on the 8GB Mac; system-ui fallback keeps the spike unblocked; exact typeface deferred to WS3.
- **DemoWorkbench parity:** it shares the market composite; apply the same class changes or it will visually diverge.

---

## 7. Out of scope (deferred)

- WS2: shared view-model, contract fixture, adapter dropped-field fixes, post-run snapshot tests.
- WS3: chosen modal tokens lock, focus-trap/scroll-lock polish, final typeface.
- WS4: backend hardening (critic_decision projection, BYOK test-green, single model source, exception scrub) — server-side, not the 8GB Mac.
- Final palette lock — deferred until the A5.4 glance test picks A/B/C.

---

**Status: PLAN ONLY. Awaiting manager approval before any execution. Do not implement, do not commit.**
