# Catalyst P1 Workbench Frontend Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a production-ready P1 frontend workbench that uses only existing backend APIs for OHLCV, live attribution runs, runtime transparency, retry, and health.

**Architecture:** The UI is a single-page React/Vite workbench with three areas: market context (left), attribution/evidence (middle), and runtime console (right). Data flow is API-first with strict polling during active runs and zero assumptions about P2 news/fundamentals endpoints. Interaction logic is state-machine driven (`idle -> queued/running -> terminal`) with deterministic error/failure rendering.

**Tech Stack:** React 18 + TypeScript + Vite, TradingView Lightweight Charts, React Testing Library + Vitest + MSW, CSS token system (finance dark mode), existing `apps/workbench/src/api/*` client.

---

## Preconditions

- Worktree: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console`
- Frontend root: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/apps/workbench`
- Backend API base: `VITE_API_BASE_URL` (default `/api`)
- Scope lock: P1 only. Do not add `/api/news*`, fundamentals, or any P2 placeholder behavior that implies implemented data.

## UI/UX Guardrails (from `ui-ux-pro-max`)

- Accessibility first: keyboard focus visible, status not color-only, chart has text summary fallback.
- One primary CTA per page: `Run Attribution`.
- Layout: 1440/1024/768/375 breakpoints, no horizontal scroll.
- Data density with readability: spacing `4/8/12/16/24/32`, numeric tabular font in data cells.
- Motion: 150-300ms only for meaningful state changes, respect `prefers-reduced-motion`.
- Chart clarity: subtle grid, strong up/down contrast, tooltip values, explicit empty/error states.

## Mandatory Review Gate (from `superpowers:requesting-code-review`)

After each task commit, run a code-review subagent before starting next task.

1. Resolve SHAs:
   - `BASE_SHA=$(git rev-parse HEAD~1)`
   - `HEAD_SHA=$(git rev-parse HEAD)`
2. Request review with:
   - `WHAT_WAS_IMPLEMENTED`: current task summary
   - `PLAN_OR_REQUIREMENTS`: this plan + task section
   - `BASE_SHA`, `HEAD_SHA`
   - `DESCRIPTION`: concise change note
3. Triage review findings:
   - Critical: fix immediately
   - Important: fix before next task
   - Minor: backlog only with explicit note in commit or task log

No task is considered complete until review gate is passed.

Execution ownership:
- Step 6 review gate is triggered by Claude/human orchestrator.
- Codex execution subagent performs Step 1-5 and then pauses for review.

## Quantified UX Acceptance Criteria (from `ui-ux-pro-max`)

- Text contrast: body text >= 4.5:1, secondary text >= 3:1.
- Focus: all interactive elements show visible focus ring.
- Touch/click target: interactive controls >= 44x44px.
- Motion: 150-300ms; `prefers-reduced-motion` fallback present.
- Layout: no horizontal scroll at `375/768/1024/1440`.
- Status semantics: status is never color-only; icon/text label required.
- Chart accessibility: provide selected-candle text summary outside canvas.
- Performance: reserve chart/console height during loading to reduce layout shift.

---

### Task 1: Test Harness + Quality Baseline

**Files:**
- Modify: `apps/workbench/package.json`
- Modify: `apps/workbench/vite.config.ts`
- Create: `apps/workbench/src/test/setup.ts`
- Create: `apps/workbench/src/test/server.ts`
- Create: `apps/workbench/src/test/render.tsx`
- Create: `apps/workbench/src/test/fixtures/api.ts`

**Step 1: Write the failing test**

```tsx
// apps/workbench/src/test/setup.test.ts
import { describe, it, expect } from "vitest";
describe("test harness", () => {
  it("loads test environment", () => {
    expect(globalThis.fetch).toBeDefined();
  });
});
```

**Step 2: Run test to verify it fails**

Run: `cd apps/workbench && npm run test -- --run`  
Expected: FAIL because test script/config is missing.

**Step 3: Write minimal implementation**

- Add scripts: `test`, `test:watch`, `test:coverage`.
- Add vitest + jsdom + rtl + msw dependencies.
- Configure `test.environment = "jsdom"` and setup file in `vite.config.ts`.
- In `src/test/fixtures/api.ts`, define minimum artifact fixtures:
  - `retrievedChunksArtifactFixture`: `payload` includes `title`, `source`, `date`, `score`.
  - `errorSnapshotArtifactFixture`: `payload` includes `error_type`, `message`.

**Step 4: Run test to verify it passes**

Run: `cd apps/workbench && npm run test -- --run src/test/setup.test.ts`  
Expected: PASS.

**Step 5: Commit**

```bash
git add apps/workbench/package.json apps/workbench/vite.config.ts apps/workbench/src/test
git commit -m "feat(frontend): add p1 frontend test harness"
```

**Step 6: Request code review**

Run:

```bash
BASE_SHA=$(git rev-parse HEAD~1)
HEAD_SHA=$(git rev-parse HEAD)
```

Then trigger `superpowers:requesting-code-review` with:
- `WHAT_WAS_IMPLEMENTED="Task 1: test harness + api fixtures"`
- `PLAN_OR_REQUIREMENTS="Task 1 in docs/plans/2026-05-27-catalyst-p1-workbench-frontend-implementation-plan.md"`
- `BASE_SHA`, `HEAD_SHA`
- `DESCRIPTION="Frontend test foundation and fixture contract"`

---

### Task 2: App State Model + Polling Controller (TDD)

**Files:**
- Create: `apps/workbench/src/state/workbench-state.ts`
- Create: `apps/workbench/src/state/polling.ts`
- Test: `apps/workbench/src/state/workbench-state.test.ts`
- Test: `apps/workbench/src/state/polling.test.ts`

**Step 1: Write the failing test**

```ts
it("transitions queued->running->terminal and stops polling", () => {
  const s0 = createInitialState();
  const s1 = reduceRunSummary(s0, { status: "QUEUED", run_id: "r1" });
  const s2 = reduceRunSummary(s1, { status: "RUNNING", run_id: "r1" });
  const s3 = reduceRunSummary(s2, { status: "SUCCEEDED", run_id: "r1" });
  expect(isPollingRequired(s3)).toBe(false);
});

it("stops polling for all terminal statuses", () => {
  const TERMINAL_STATUSES = [
    "SUCCEEDED",
    "PARTIAL",
    "INSUFFICIENT",
    "FAILED_SYSTEM",
    "FAILED_REQUEST",
  ] as const;
  for (const status of TERMINAL_STATUSES) {
    const state = reduceRunSummary(createInitialState(), { status, run_id: "r1" });
    expect(isPollingRequired(state)).toBe(false);
  }
});
```

**Step 2: Run test to verify it fails**

Run: `cd apps/workbench && npm run test -- --run src/state/workbench-state.test.ts`  
Expected: FAIL with missing reducer/controller.

**Step 3: Write minimal implementation**

- Add pure reducer for run status, selected ticker/date, run_id, last event seq.
- Add polling helper (`shouldPoll`, `pollIntervalMs`).
- Keep logic deterministic; no React hooks here.

**Step 4: Run test to verify it passes**

Run: `cd apps/workbench && npm run test -- --run src/state/*.test.ts`  
Expected: PASS.

**Step 5: Commit**

```bash
git add apps/workbench/src/state
git commit -m "feat(frontend): add workbench state and polling model"
```

**Step 6: Request code review**

Run:

```bash
BASE_SHA=$(git rev-parse HEAD~1)
HEAD_SHA=$(git rev-parse HEAD)
```

Then trigger `superpowers:requesting-code-review` with Task 2 metadata.

---

### Task 3: Context Bar Data Wiring (tickers + range-local + health)

**Files:**
- Create: `apps/workbench/src/hooks/useBootstrapData.ts`
- Modify: `apps/workbench/src/App.tsx`
- Create: `apps/workbench/src/components/context/ContextBar.tsx`
- Test: `apps/workbench/src/components/context/ContextBar.test.tsx`

**Step 1: Write the failing test**

```tsx
it("renders ticker options and runtime health badge from api", async () => {
  render(<App />);
  expect(await screen.findByRole("combobox", { name: /ticker/i })).toBeInTheDocument();
  expect(await screen.findByText(/ready|degraded|failed/i)).toBeInTheDocument();
});
```

**Step 2: Run test to verify it fails**

Run: `cd apps/workbench && npm run test -- --run src/components/context/ContextBar.test.tsx`  
Expected: FAIL (placeholder app has no async bootstrap).

**Step 3: Write minimal implementation**

- On mount call `getTickers`, `getRangeLocal`, `getRuntimeHealth`.
- Initialize ticker/date selectors from returned data constraints.
- Render explicit loading/error states.

**Step 4: Run test to verify it passes**

Run: `cd apps/workbench && npm run test -- --run src/components/context/ContextBar.test.tsx`  
Expected: PASS.

**Step 5: Commit**

```bash
git add apps/workbench/src/App.tsx apps/workbench/src/hooks apps/workbench/src/components/context
git commit -m "feat(frontend): wire context bar bootstrap data"
```

**Step 6: Request code review**

Run:

```bash
BASE_SHA=$(git rev-parse HEAD~1)
HEAD_SHA=$(git rev-parse HEAD)
```

Then trigger `superpowers:requesting-code-review` with Task 3 metadata.

---

### Task 4: K-Line Panel with TradingView Lightweight Charts

**Files:**
- Create: `apps/workbench/src/components/chart/KLinePanel.tsx`
- Create: `apps/workbench/src/components/chart/useLightweightChart.ts`
- Modify: `apps/workbench/src/App.tsx`
- Test: `apps/workbench/src/components/chart/KLinePanel.test.tsx`

**Step 1: Write the failing test**

```tsx
it("loads ohlcv for selected ticker and shows empty state when no candles", async () => {
  render(<App />);
  expect(await screen.findByText(/no ohlcv data for this range/i)).toBeInTheDocument();
});
```

**Step 2: Run test to verify it fails**

Run: `cd apps/workbench && npm run test -- --run src/components/chart/KLinePanel.test.tsx`  
Expected: FAIL.

**Step 3: Write minimal implementation**

- Fetch OHLCV on ticker/date range changes.
- Render chart area with reserved height to avoid layout shift.
- Handle `loading / empty / error` states and OHLCV tooltip summary area.

**Step 4: Run test to verify it passes**

Run: `cd apps/workbench && npm run test -- --run src/components/chart/KLinePanel.test.tsx`  
Expected: PASS.

**Step 5: Commit**

```bash
git add apps/workbench/src/components/chart apps/workbench/src/App.tsx
git commit -m "feat(frontend): add p1 k-line panel with ohlcv states"
```

**Step 6: Request code review**

Run:

```bash
BASE_SHA=$(git rev-parse HEAD~1)
HEAD_SHA=$(git rev-parse HEAD)
```

Then trigger `superpowers:requesting-code-review` with Task 4 metadata.

---

### Task 5: Selected Day Context + Run Attribution Action

**Files:**
- Create: `apps/workbench/src/components/attribution/SelectedDayContext.tsx`
- Modify: `apps/workbench/src/App.tsx`
- Test: `apps/workbench/src/components/attribution/SelectedDayContext.test.tsx`

**Step 1: Write the failing test**

```tsx
it("submits create run and shows queued status chip", async () => {
  render(<App />);
  await user.click(screen.getByRole("button", { name: /run attribution/i }));
  expect(await screen.findByText(/queued/i)).toBeInTheDocument();
});
```

**Step 2: Run test to verify it fails**

Run: `cd apps/workbench && npm run test -- --run src/components/attribution/SelectedDayContext.test.tsx`  
Expected: FAIL.

**Step 3: Write minimal implementation**

- Bind button to `createLiveRun`.
- Disable button while same run is active.
- Render status chip and inline request validation feedback.

**Step 4: Run test to verify it passes**

Run: `cd apps/workbench && npm run test -- --run src/components/attribution/SelectedDayContext.test.tsx`  
Expected: PASS.

**Step 5: Commit**

```bash
git add apps/workbench/src/components/attribution apps/workbench/src/App.tsx
git commit -m "feat(frontend): add selected-day run entry flow"
```

**Step 6: Request code review**

Run:

```bash
BASE_SHA=$(git rev-parse HEAD~1)
HEAD_SHA=$(git rev-parse HEAD)
```

Then trigger `superpowers:requesting-code-review` with Task 5 metadata.

---

### Task 6: Live Polling Loop (summary + events incremental)

**Files:**
- Create: `apps/workbench/src/hooks/useLiveRunPolling.ts`
- Modify: `apps/workbench/src/App.tsx`
- Test: `apps/workbench/src/hooks/useLiveRunPolling.test.tsx`

**Step 1: Write the failing test**

```tsx
it("polls events with after_seq and stops at terminal status", async () => {
  expect.fail("write MSW polling assertions before implementation");
});
```

**Step 2: Run test to verify it fails**

Run: `cd apps/workbench && npm run test -- --run src/hooks/useLiveRunPolling.test.tsx`  
Expected: FAIL.

**Step 3: Write minimal implementation**

- Poll `getLiveRun` + `getLiveRunEvents(after_seq)` every 1.5s while active.
- Advance local `lastEventSeq`.
- Stop polling on terminal statuses.

**Step 4: Run test to verify it passes**

Run: `cd apps/workbench && npm run test -- --run src/hooks/useLiveRunPolling.test.tsx`  
Expected: PASS.

**Step 5: Commit**

```bash
git add apps/workbench/src/hooks/useLiveRunPolling.ts apps/workbench/src/App.tsx
git commit -m "feat(frontend): add live run polling controller"
```

**Step 6: Request code review**

Run:

```bash
BASE_SHA=$(git rev-parse HEAD~1)
HEAD_SHA=$(git rev-parse HEAD)
```

Then trigger `superpowers:requesting-code-review` with Task 6 metadata.

---

### Task 7: Runtime Console (nodes, predicted-next wording, failure visibility)

**Files:**
- Create: `apps/workbench/src/components/runtime/RuntimeConsole.tsx`
- Create: `apps/workbench/src/components/runtime/NodeTimeline.tsx`
- Create: `apps/workbench/src/components/runtime/NodeCard.tsx`
- Test: `apps/workbench/src/components/runtime/RuntimeConsole.test.tsx`
- Modify: `apps/workbench/src/App.tsx`

**Step 1: Write the failing test**

```tsx
it("shows last completed and predicted next, never current node label", () => {
  render(<RuntimeConsole ... />);
  expect(screen.getByText(/last completed/i)).toBeInTheDocument();
  expect(screen.getByText(/predicted next/i)).toBeInTheDocument();
  expect(screen.queryByText(/current node/i)).toBeNull();
});
```

**Step 2: Run test to verify it fails**

Run: `cd apps/workbench && npm run test -- --run src/components/runtime/RuntimeConsole.test.tsx`  
Expected: FAIL.

**Step 3: Write minimal implementation**

- Render event rows with node/status/latency/tokens/cost.
- Expand latest meaningful node by default.
- Show clear failure banner mapping from status + failure payload.
- `predicted_next_node` source of truth:
  - use `GET /api/live-runs/{run_id}` field when present;
  - fallback to frontend `NODE_ORDER` inference when absent.

**Step 4: Run test to verify it passes**

Run: `cd apps/workbench && npm run test -- --run src/components/runtime/RuntimeConsole.test.tsx`  
Expected: PASS.

**Step 5: Commit**

```bash
git add apps/workbench/src/components/runtime apps/workbench/src/App.tsx
git commit -m "feat(frontend): add runtime console with node transparency"
```

**Step 6: Request code review**

Run:

```bash
BASE_SHA=$(git rev-parse HEAD~1)
HEAD_SHA=$(git rev-parse HEAD)
```

Then trigger `superpowers:requesting-code-review` with Task 7 metadata.

---

### Task 8: Artifact Tabs + Evidence Preview (P1 Evidence Only)

**Files:**
- Create: `apps/workbench/src/components/artifacts/ArtifactTabs.tsx`
- Create: `apps/workbench/src/components/artifacts/EvidencePreview.tsx`
- Create: `apps/workbench/src/components/artifacts/RawResponseViewer.tsx`
- Test: `apps/workbench/src/components/artifacts/ArtifactTabs.test.tsx`
- Modify: `apps/workbench/src/App.tsx`

**Step 1: Write the failing test**

```tsx
it("renders artifact payload in payload field and supports type filter", async () => {
  render(<App />);
  expect(await screen.findByText(/retrieved_chunks|raw_llm_response/i)).toBeInTheDocument();
});
```

**Step 2: Run test to verify it fails**

Run: `cd apps/workbench && npm run test -- --run src/components/artifacts/ArtifactTabs.test.tsx`  
Expected: FAIL.

**Step 3: Write minimal implementation**

- Fetch artifacts by `run_id`, optional `event_seq` and `artifact_type`.
- Show `payload` (not `payload_json`) contract.
- Add bounded raw viewer with copy + wrap toggle.
- Evidence panel must be labeled `Evidence Used by This Run`.

**Step 4: Run test to verify it passes**

Run: `cd apps/workbench && npm run test -- --run src/components/artifacts/ArtifactTabs.test.tsx`  
Expected: PASS.

**Step 5: Commit**

```bash
git add apps/workbench/src/components/artifacts apps/workbench/src/App.tsx
git commit -m "feat(frontend): add artifact tabs and p1 evidence preview"
```

**Step 6: Request code review**

Run:

```bash
BASE_SHA=$(git rev-parse HEAD~1)
HEAD_SHA=$(git rev-parse HEAD)
```

Then trigger `superpowers:requesting-code-review` with Task 8 metadata.

---

### Task 9: Retry Flow + Terminal State UX

**Files:**
- Create: `apps/workbench/src/components/attribution/AttributionSummary.tsx`
- Modify: `apps/workbench/src/App.tsx`
- Test: `apps/workbench/src/components/attribution/AttributionSummary.test.tsx`

**Step 1: Write the failing test**

```tsx
it("creates retry run for terminal state and switches polling target", async () => {
  render(<App />);
  await user.click(screen.getByRole("button", { name: /retry/i }));
  expect(await screen.findByText(/run_id:/i)).toHaveTextContent("retry");
});
```

**Step 2: Run test to verify it fails**

Run: `cd apps/workbench && npm run test -- --run src/components/attribution/AttributionSummary.test.tsx`  
Expected: FAIL.

**Step 3: Write minimal implementation**

- Enable retry only for terminal runs.
- On retry success, swap active run to returned `run_id`.
- Preserve selected ticker/date/chart state.

**Step 4: Run test to verify it passes**

Run: `cd apps/workbench && npm run test -- --run src/components/attribution/AttributionSummary.test.tsx`  
Expected: PASS.

**Step 5: Commit**

```bash
git add apps/workbench/src/components/attribution apps/workbench/src/App.tsx
git commit -m "feat(frontend): add retry flow and terminal summary states"
```

**Step 6: Request code review**

Run:

```bash
BASE_SHA=$(git rev-parse HEAD~1)
HEAD_SHA=$(git rev-parse HEAD)
```

Then trigger `superpowers:requesting-code-review` with Task 9 metadata.

---

### Task 10: UI Token System + Responsive Layout Hardening

**Files:**
- Modify: `apps/workbench/src/styles.css`
- Modify: `apps/workbench/src/App.tsx`
- Test: `apps/workbench/src/App.layout.test.tsx`

**Step 1: Write the failing test**

```tsx
it("has one primary run action and responsive panel landmarks", () => {
  render(<App />);
  const runButtons = screen.getAllByRole("button", { name: /run attribution/i });
  expect(runButtons).toHaveLength(1);
});
```

**Step 2: Run test to verify it fails**

Run: `cd apps/workbench && npm run test -- --run src/App.layout.test.tsx`  
Expected: FAIL.

**Step 3: Write minimal implementation**

- Apply finance token palette and typography scale from design doc.
- Implement 3 breakpoints: `>=1440`, `1024-1439`, `<1024`.
- Ensure no horizontal overflow and visible keyboard focus ring.

**Step 4: Run test to verify it passes**

Run: `cd apps/workbench && npm run test -- --run src/App.layout.test.tsx`  
Expected: PASS.

**Step 5: Commit**

```bash
git add apps/workbench/src/styles.css apps/workbench/src/App.tsx apps/workbench/src/App.layout.test.tsx
git commit -m "feat(frontend): apply p1 finance design tokens and responsive layout"
```

**Step 6: Request code review**

Run:

```bash
BASE_SHA=$(git rev-parse HEAD~1)
HEAD_SHA=$(git rev-parse HEAD)
```

Then trigger `superpowers:requesting-code-review` with Task 10 metadata.

---

### Task 11: End-to-End API Contract Smoke (Frontend-side)

**Files:**
- Create: `apps/workbench/src/test/workbench.api-contract.test.tsx`
- Modify: `apps/workbench/src/api/client.ts` (only if contract mismatch found)
- Modify: `apps/workbench/src/api/types.ts` (only if contract mismatch found)

**Step 1: Write the failing test**

```tsx
it("consumes all 9 p1 endpoints without current_node/news assumptions", async () => {
  // mock complete p1 flow through msw and assert rendered UI states
});
```

**Step 2: Run test to verify it fails**

Run: `cd apps/workbench && npm run test -- --run src/test/workbench.api-contract.test.tsx`  
Expected: FAIL initially.

**Step 3: Write minimal implementation**

- Align client mapping to DTO reality:
  - events may include `SUFFICIENT` / `SYSTEM_ERROR`
  - artifacts render `payload`
  - no `current_node`
- Ensure no call is made to any `news` endpoint.
- Keep terminal-stop logic in Task 2 as the single source of truth for polling stop conditions.

**Step 4: Run test to verify it passes**

Run: `cd apps/workbench && npm run test -- --run src/test/workbench.api-contract.test.tsx`  
Expected: PASS.

**Step 5: Commit**

```bash
git add apps/workbench/src/test/workbench.api-contract.test.tsx apps/workbench/src/api
git commit -m "test(frontend): verify p1 api contract integration"
```

**Step 6: Request code review**

Run:

```bash
BASE_SHA=$(git rev-parse HEAD~1)
HEAD_SHA=$(git rev-parse HEAD)
```

Then trigger `superpowers:requesting-code-review` with Task 11 metadata.

---

### Task 12: Verification + Documentation Sync

**Files:**
- Modify: `docs/plans/2026-05-26-attribution-workbench-ui-ux-design.md` (only if implementation deviations exist)
- Create: `apps/workbench/README.md`

**Step 1: Write the failing check list**

```md
- [ ] npm run typecheck passes
- [ ] npm run build passes
- [ ] npm run test -- --run passes
- [ ] no p2 endpoint references in frontend code
```

**Step 2: Run verification and capture output**

Run:

```bash
cd apps/workbench
npm run typecheck
npm run build
npm run test -- --run
rg -n "/api/news|fundamentals|categories|sentiments" src
```

Expected:
- All commands pass.
- `rg` returns no matches.

**Step 3: Write minimal documentation**

- Add local run/test commands.
- Add P1 scope statement and known non-goals.

**Step 4: Re-run verification**

Run same command set above.  
Expected: PASS.

**Step 5: Commit**

```bash
git add apps/workbench/README.md docs/plans/2026-05-26-attribution-workbench-ui-ux-design.md
git commit -m "docs(frontend): add p1 runbook and scope lock"
```

**Step 6: Request code review**

Run:

```bash
BASE_SHA=$(git rev-parse HEAD~1)
HEAD_SHA=$(git rev-parse HEAD)
```

Then trigger final `superpowers:requesting-code-review` and clear all Critical/Important findings before merge.

---

## Execution Notes

- Keep commits small and task-scoped; do not batch multiple tasks in one commit.
- If a task reveals backend contract mismatch, stop and open a focused fix task instead of silently patching behavior.
- Do not add P2 placeholders that imply implemented backend behavior.

## Final Acceptance Criteria

- Frontend uses only 9 implemented P1 APIs.
- Full interaction loop works: select ticker/date -> run -> poll -> inspect events/artifacts -> retry.
- Runtime transparency is explicit and honest (`last_completed_node`, `predicted_next_node`).
- Financial data-dense UI is readable, accessible, and responsive.
- Test suite and build are green in the target environment.
- All task-level review gates are executed and resolved.
- No unresolved Critical/Important code-review findings remain.
