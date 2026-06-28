# Catalyst Final Workbench — UI Design & Architecture Spec (Master)

**Date:** 2026-06-27
**Branch:** `ui/v0.1-workbench-redesign`
**Status:** Authoritative for the frontend. Replaces `2026-06-26-master-final-workbench-spec.md` (deleted).
**Roles:** Manager (this spec) → glmcodex (implementation) → human (review + GPU deploy).
**Constraint:** Mac 8GB — never run LanceDB / embeddings / full attribution / full pytest locally. Only the lightweight config/store/catalog test slice may run locally.

> This is a **design spec**, not an implementation. No code is committed and nothing is built from it until the human approves. Per project convention it contains no code blocks — design is expressed as tables, measurements, and prose. Color values are **direction, not locked tokens** (see §3, §10); structural values (spacing, type, dimensions) are concrete and binding.

---

## 0. How to read this spec

The target is a **precise, dense, premium financial work-cabin** — the visual register of Linear, Vercel dashboard, Stripe dashboard, and trading terminals — adapted to the existing dark cockpit and the ValueCell reference. The current build is not ugly by accident; it fails on four specific things this spec fixes:

1. surfaces sit at nearly the same value, so nothing reads as elevated (the modal "blend" bug, and the black gap between chart and details);
2. redundant labels add noise without information;
3. modules are fragmented instead of composed;
4. post-run content deforms because the view-model is not contract-aligned.

Everything below is in service of those four fixes plus an overall lift to "big-company SaaS" polish.

---

## 1. Design north star — where the premium feel actually comes from

Premium is not a color. It is the sum of these, and glmcodex must treat each as a hard requirement:

| Lever | Rule |
|---|---|
| Spacing rhythm | One 4px-based scale, applied consistently. No ad-hoc margins. (§4) |
| Surface hierarchy | Few elevation levels, each **perceptibly** separated by fill contrast first, hairline border second, shadow only for true overlays. (§3) |
| Typographic hierarchy | A tight scale with clear size/weight steps; tabular numerics for all figures. (§5) |
| Density | Compact but breathing — data-dense without cramping. Padding is generous at module edges, tight inside rows. |
| Composition | Related content lives in one container with internal dividers — never fragmented panels separated by raw page canvas. (§7) |
| Subtraction | Remove every label that does not inform a decision. (§11) |
| Restraint in color | Neutral base; one action accent; semantic up/down and status colors only. One accent per view. |
| Considered states | Empty, loading, and error are designed, not afterthoughts. (§12) |

**Anti-slop guardrails (from the artifact skill, enforced):** no all-centered layouts (financial data is left-aligned and grid-bound); no purple/gradient decoration (flat surfaces only); no single uniform corner radius (radius differs by element class — §6); not Inter (§5 font direction).

---

## 2. Layout architecture

Primary design canvas: **1440 × 900**. The page is a single vertical stack of four blocks. There is no global app sidebar — this is a single-surface workbench.

### 2.1 The four blocks

| # | Block | Contains | Maps to current component |
|---|---|---|---|
| 1 | Top control bar | Catalyst wordmark; ticker selector; date; Run Attribution (primary); Model Settings (secondary) | `WorkbenchHeader` |
| 2 | Market module | Cohesive container: K-line chart (left) + session details (right) | `MarketSessionPanel` + `SessionOverviewPanel`, **merged** |
| 3 | Candidate news | Section heading + news cards rail (input chain, always visible) | `EvidenceIntakePanel` (preview mode) |
| 4 | Attribution workspace | Tabbed result: Summary / Trace / Evidence / Diagnostics | `ResultWorkspaceTabs` |

### 2.2 Vertical budget at 1440×900

Usable height after browser chrome is ~820–860px. The first-screen contract: **blocks 1–3 render complete above the fold; block 4 may begin below the fold.** News is never truncated to fit the workspace — if space is tight, the workspace moves down, not the news.

| Region | Height | Cumulative top |
|---|---|---|
| Page top padding | 20px | 20 |
| Block 1 — control bar | 56px | 76 |
| Gap | 16px | 92 |
| Block 2 — market module | 380px | 472 |
| Gap | 16px | 488 |
| Block 3 — candidate news | 200px | 688 |
| Gap | 16px | 704 |
| Block 4 — workspace (begins; ready-state header peeks) | — | 704 → below fold |

Result: at 820px usable height, the candidate news rail bottom sits at ~704px — fully visible with ~100px of headroom; the workspace header peeks, signaling scroll. **No block may render half-cut at the fold.** If content forces overflow, the workspace flows below the fold intact; blocks 1–3 stay whole.

### 2.3 Horizontal grid

Page padding: **24px** left/right. Content max-width: none below 1600px (fills), capped at ~1600px above to avoid over-stretch. Inside the page, a 12-column mental grid with 24px gutters; the market module splits **chart ≈ 8 cols / details ≈ 4 cols** (≈ 66% / 34%).

### 2.4 Before-run vs after-run

- **Before run:** blocks 2–3 populated from market + news APIs; block 4 shows a designed empty state (§12), not a blank panel.
- **After run:** block 4 populates with tabs; candidate news (block 3) remains visible as the input record; the workspace Evidence tab shows *retrieved/graded* evidence. The transition is a content swap inside block 4 — blocks 1–3 do not reflow.

### 2.5 Transitions between modules

Modules are separated by the 16px vertical gap and by surface-value contrast, not by heavy rules. The page reads as a stack of distinct-but-related surfaces on a common canvas. No module shares a border with the page edge that looks like a wireframe cell.

---

## 3. Surface hierarchy & elevation (color = direction, not locked)

The single most important fix. Define **four elevation levels** by relationship, not by fixed hex:

| Level | Role | Separation rule |
|---|---|---|
| L0 | Page canvas | base |
| L1 | Module / panel (market, news, workspace) | clearly above L0 |
| L2 | Card / input / tab-active inside a panel | clearly above L1 |
| L3 | Overlay (Model Settings modal, dropdowns, popovers) | clearly above everything, **plus** scrim + shadow |

**Binding constraint:** each level must differ from the one below it by a *perceptible* value step — as a working target, **≥ ~10 L\* units** (or equivalent perceived contrast). The current modal fails because L3 (`#0d1520`) sits only ~3 points above L0 (`#060912`). This rule fixes the blend bug **independent of whether the final palette is dark, light, or hybrid.**

**Borders:** hairline, low-alpha, used to *reinforce* a separation that fill contrast already implies — never as the primary separator on every panel (that produces the wireframe look). **Shadows:** reserved for L3 overlays and, at most, a single very subtle lift on the market hero. Everything else is flat.

Indicative dark-cockpit values (for orientation only — finalized in WS1 via the §10 glance test): canvas ~#0a0f1a, panel ~#121a28, card ~#1a2435, overlay per chosen modal option. Do not treat these as locked.

---

## 4. Spacing system (concrete, binding)

Base unit **4px**. Allowed steps: **4, 8, 12, 16, 20, 24, 32, 40, 48**. Nothing off-scale.

| Context | Value |
|---|---|
| Page padding (horizontal) | 24px |
| Page padding (top) | 20px |
| Inter-module vertical gap | 16px |
| Panel padding (standard) | 20px |
| Panel padding (market hero) | 24px |
| Card padding | 16px |
| Row inner padding (dense lists/tables) | 8–12px |
| Internal component gap (label↔control, stat rows) | 12px |
| Tight pairing (icon↔text, badge internals) | 6–8px |

---

## 5. Typography system (concrete)

**Font direction (not Inter):** a precise neutral grotesque for UI — recommend **Geist** or **IBM Plex Sans** (fallback `system-ui`) — paired with a **monospaced / tabular** face for all numerics (prices, scores, tokens, latency): recommend **Geist Mono** or **IBM Plex Mono**. Numerics always use `font-variant-numeric: tabular-nums`.

| Role | Size / weight / line-height | Usage |
|---|---|---|
| Display | 24 / 600 / 1.2 | rare; top-level numbers only |
| Section heading (H1) | 18 / 600 / 1.3 | "Candidate news", workspace tab region title |
| Sub heading (H2) | 15 / 600 / 1.35 | card titles, cause headlines |
| Body | 13–14 / 400 / 1.5 | snippets, descriptions |
| Label / caption | 12 / 500 / 1.4 | field labels, meta |
| Micro | 11 / 500 / 1.4 | timestamps, source tags |
| Key numeric | 18–22 / 600, tabular | price, change %, grounding rate |

Sentence case everywhere. No ALL-CAPS eyebrow rows (those are the labels being deleted in §11). Color: primary text ~L0-relative high-contrast; secondary muted; tertiary for meta — three text tiers, no more.

---

## 6. Controls & components (dimensions binding; color directional)

Corner radius differs by element class (anti-slop): **inputs/buttons 6–8px, cards 8–10px, pills/badges full-round, chart container 10px.** No single uniform radius.

| Element | Spec |
|---|---|
| Button (default/secondary) | height 36px; horizontal padding 12–14px; hairline border; quiet fill |
| Button (primary — Run Attribution) | height 38–40px; accent fill; one per view |
| Icon button (Model Settings) | 36px square; shows a status dot when a model is configured |
| Text input / select | height 36px; visible hairline border; clear focus ring (accent, ~2px); placeholder is a real example, not the label |
| Badge / pill | height 20–22px; semantic fill at low alpha + same-family text |
| Tab (workspace) | height 36px; active = L2 fill + 2px bottom accent; inactive = transparent |
| News card | rail item width 280–320px; min-height 120px; 16px padding |
| Evidence row | full-width; 12–16px vertical padding; hairline row divider |
| Stat row (session details) | label left (muted), value right (tabular); 12px gap |

Accessibility: desktop control height is 36px for density; on the ≤768px breakpoint, controls grow to a **≥44px** effective tap target. Visible focus rings on every interactive element. Contrast for text/background must pass WCAG AA at the chosen palette (verified in WS1).

---

## 7. Market module — cohesion redesign (fixes the black gap)

**Problem:** chart and session details are two separate panels with raw page canvas showing between them, reading as a broken seam.

**Target:** one panel (L1), one background, internal split:

- **Left region — chart (~66%):** title shows **"AAPL" only** (ticker), with the key figure inline beside it — last price + change% (semantic up/down color) + the selected date. No "Daily Price" string. The candlestick chart fills the region; clicking a candle selects that date and drives both the session details and the candidate news fetch.
- **Internal divider:** a single hairline vertical rule (L1-on-L1, low alpha) between chart and details. Not a gap, not canvas.
- **Right region — session details (~34%):** the selected day's metrics as a compact stat list (open, high, low, close, volume, change% , range) — labels muted-left, values tabular-right. This is `SessionOverviewPanel` content, now living *inside* the market panel.

The whole module has 24px padding and a single subtle elevation. It is the visual hero of the page.

---

## 8. Candidate news — always complete (fixes truncation)

Candidate news is part of the attribution input chain and **must render complete on the first screen.** It is the one section that keeps a heading ("Candidate news") because the word carries meaning.

- Layout: a horizontal rail of news cards (3–4 visible at 1440, horizontal scroll for the rest) **or** a single full-width row of cards — whichever keeps the first row fully visible within the 200px budget.
- Card content: source tag (micro), date (micro), headline (H2, clamp 2 lines), optional one-line snippet. Pre-run there is no relevance score (nothing has graded them yet).
- Empty state: if no news for the date, a designed empty row ("No candidate news for this date"), not a collapsed gap.
- Post-run: candidate news stays; the *graded/retrieved* subset appears in the workspace Evidence tab (§9). Never hide candidate news to make room for the workspace — push the workspace below the fold instead.

---

## 9. Attribution workspace — tabs + contract binding

Four tabs (L1 panel, L2 active tab). Each tab binds to specific `WorkspaceResponse` fields — glmcodex must render from these exact fields, with graceful absence (hide the element, never show placeholder text):

| Tab | Source fields |
|---|---|
| Summary | `result.output_status`, `result.summary_md`, `result.grounding_rate`, `result.causes[]` (`text`, `category`, `direction`, `confidence`, `evidence_ids`), `result.validation_error` |
| Trace | `stages[]` (`label`, `status`, `duration_ms`, `input_tokens`, `output_tokens`, `cost_usd`, `summary`, `artifact_types`), `last_completed_node`, `predicted_next_node` |
| Evidence | `evidence[]` (`headline`, `snippet`, `source_type`, `reference_date`, `retrieval_score`, `rerank_score`, `critic_relevance`, `critic_category`, `critic_reasoning`, `critic_decision`, `cited`) |
| Diagnostics | `diagnostics` (`retrieved_count`, `reranked_count`, `graded_count`, `cited_count`, `top_evidence_score`, `total_tokens`, `total_cost_usd`), plus run meta (`runtime_ms`, `model_id`) |

Status-driven layouts the design must cover: a normal grounded result (causes + evidence), a **refusal / insufficient** result (`output_status` indicates no accepted evidence → show the refusal reason, not an empty causes list), and a **failure** (`failure` payload present → error state, §12). The Evidence tab must render correctly when `critic_decision` is null (an item is "ungraded") without collapsing the whole list to ungraded.

---

## 10. Model Settings modal — three options, decided by glance test

**Do not lock dark or light.** The real requirement: the panel must read as one independent surface at a glance, the background must not compete with form fields, and it must follow the §3 L3 rule (clear elevation + scrim + shadow) and ValueCell's "solid panel, generous padding, strong separation" pattern.

| Option | Description | Pros | Cons |
|---|---|---|---|
| A. Dark elevated, high-contrast | Clearly-raised dark panel well above canvas, hairline light border, strong blurred scrim, real shadow | Stays in cockpit family; cohesive | Needs careful value tuning to avoid the blend bug; dark forms read heavier |
| B. Light solid dialog (ValueCell) | Near-white panel, dark text, visible input borders, shadow-lg, ~50% black backdrop | Maximum separation; familiar settings pattern; inputs read crisply | Stark switch from dark cockpit; can feel "from another app" if not warmed |
| C. Hybrid warm-neutral panel | Desaturated graphite/slate panel lighter than canvas, off-white text (Linear-modal register) | Separates clearly while staying in-family; softer, premium | Needs a considered neutral ramp; muddy if value step too small |

**Manager lean:** C, then A. But the decision is deferred to a **screenshot glance test in WS1**: glmcodex renders all three over the *actual* live chart, captures screenshots, and the human picks the one that passes the glance test. Tokens are locked only then.

Modal UX (independent of color choice): portal-rendered, centered, `aria-modal`, real focus trap (Tab cycles within), ESC + backdrop close, body scroll lock, header height stable when it opens. Flow: Provider → API key → Test connection → (model select appears on success) → Use model. Custom provider adds Base URL + Model ID. Browser-key only in the UI (backend server_env support stays, hidden). Generous padding (≈24px), field gap ≈16px.

---

## 11. Text cleanup inventory (delete on sight)

| Location | Remove | Keep / replace with |
|---|---|---|
| Header | "Workbench" wordmark suffix | "Catalyst" only |
| Header | visible "Ticker" / "Session" text labels | self-evident controls: `AAPL` selector, date, Run Attribution, Model Settings |
| Chart | "Daily Price" | "AAPL" (ticker) only, with inline price/change |
| Panels | eyebrow labels: "Market Session", "Session Overview", "Evidence Intake", "Analysis" | nothing — let the content and one real heading speak |
| Candidate news | — | keep the heading "Candidate news" (meaningful) |

Rule going forward: a label survives only if it changes what the user does. Category eyebrows do not.

---

## 12. States — empty / loading / error (designed, concrete)

| State | Design |
|---|---|
| Chart loading | skeleton candles (shimmer), no spinner-on-blank |
| News loading | 3–4 skeleton cards at the real card dimensions |
| Workspace empty (pre-run) | left-aligned (not centered) prompt with an icon, one line ("Run attribution to generate a grounded explanation"), referencing the Run button; not a blank panel |
| Run in progress | workspace shows the stage progression (from `stages[]` / `predicted_next_node`); header shows a running status on the Run control |
| Evidence/ result error | inline error card: plain-language message + Retry; **never** a raw exception string (ties to backend scrub, WS4) |
| Refusal / insufficient | Summary tab states the refusal reason from `output_status`; no fabricated causes |
| Empty optional field | hide the element; no placeholder text leaks |

---

## 13. Responsive breakpoints

| Breakpoint | Behavior |
|---|---|
| ≥1280 (primary) | full 4-block stack; market module side-by-side (chart + details) |
| 1024–1280 | market details may narrow or wrap under the chart; news rail scrolls |
| 768–1024 (tablet) | chart stacks above details; news becomes a vertical list; header controls wrap |
| ≤768 (mobile) | single column; controls collapse (possibly into a sheet); all targets ≥44px; no horizontal overflow |

No horizontal scroll at any width. Evidence preview and news must never clip at the first viewport on the primary breakpoint.

---

## 14. Post-run contract-aligned fixture (not a production mock)

A dev/test fixture is required to prove the post-run UI does not deform. It is **not** marketing mock data — it is a faithful instance of the real contract.

- Shape: a `WorkspaceResponse` object conforming exactly to the interfaces in `apps/workbench/src/api/types.ts` (see appendix). One shared view-model type is the single source both this fixture and the live adapter conform to.
- Coverage cases the fixture must include (as variants): normal grounded result; refusal/insufficient (`output_status` set, empty `causes`); failure payload present; evidence with null `critic_decision` (ungraded) mixed with graded; very long `summary_md` and long headlines (overflow/clamp test); empty `evidence[]`; missing `diagnostics`; null optional numerics.
- Use: render Summary / Trace / Evidence / Diagnostics from each variant and snapshot-assert no layout deformation and no placeholder leakage. A contract test fails if the fixture diverges from the typed contract.

This is the guarantee behind "run attribution 后 UI 不变形."

---

## 15. Workstream plan (reordered — UI first)

| WS | Title | P | Summary |
|---|---|---|---|
| **WS0** | Final UI design architecture spec | P0 | This document. Approve before any build. |
| **WS1** | Frontend visual rescue | P0 | Implement §2–§13: layout budget, spacing/type/control system, market-module cohesion, text cleanup, surface hierarchy. Includes the §10 glance-test screenshots → modal decision. |
| **WS2** | Terminal fixture + view-model parity | P1 | Implement §9 + §14: shared view-model, contract fixture, fix the live adapter's dropped fields, snapshot tests for post-run UI. |
| **WS3** | Model Settings final UX polish | P1 | Apply the chosen modal option's tokens; focus trap, scroll lock, flow polish. |
| **WS4** | Backend hardening | P2 | critic_decision projection bug, test-green the BYOK slice, single model source of truth, exception scrub, single-worker deploy note. |

Backend matters, but it does not block the UI rescue. WS4 runs after the workspace is visually solid.

---

## 16. Definition of done — visual rescue (WS1)

- At 1440×900: blocks 1–3 render complete above the fold; no block is half-cut; the workspace begins below the fold cleanly.
- The market module reads as one surface; no black seam between chart and details.
- Every elevation level passes the §3 perceptible-step test; the Model Settings modal passes the glance test in screenshots over the live chart.
- All deleted text (§11) is gone; no category eyebrows remain.
- Candidate news is fully visible and never truncated to fit the workspace.
- Spacing, type, and control dimensions match §4–§6 exactly; no off-scale values; corner radii vary by element class; font is not Inter.
- Empty/loading/error states (§12) are present, not blank panels.
- No horizontal overflow from 768px to 1600px.

---

## Appendix — `WorkspaceResponse` contract reference

For WS2/WS14 binding. Source: `apps/workbench/src/api/types.ts`.

- `WorkspaceResponse`: `run_id`, `status`, `ticker?`, `trade_date?`, `model_id?`, `started_at?`, `ended_at?`, `runtime_ms?`, `last_completed_node?`, `predicted_next_node?`, `result?`, `evidence[]`, `stages[]`, `diagnostics?`, `failure?`
- `WorkspaceResult`: `output_status?`, `summary_md?`, `grounding_rate?`, `causes[]`, `validation_error?`, `validator_attempts?`
- `WorkspaceCause`: `text`, `category?`, `direction?`, `confidence?`, `evidence_ids[]`
- `WorkspaceEvidenceItem`: `id`, `ticker?`, `headline?`, `snippet?`, `source_type?`, `reference_date?`, `retrieval_score?`, `rerank_score?`, `critic_relevance?`, `critic_category?`, `critic_reasoning?`, `temporal_match?`, `event_specificity?`, `temporal_alignment?`, `evidence_granularity?`, `conflict_signal?`, `critic_decision?`, `cited`
- `WorkspaceStage`: `id`, `label`, `status`, `started_at?`, `ended_at?`, `duration_ms?`, `input_tokens?`, `output_tokens?`, `cost_usd?`, `summary?`, `artifact_types[]`
- `WorkspaceDiagnostics`: `resolved_stage_count`, `total_stage_count`, `retrieved_count`, `reranked_count`, `graded_count`, `cited_count`, `top_evidence_score?`, `total_tokens?`, `total_cost_usd?`
- `WorkspaceFailure`: `status?`, `message?`, `source?`, `node?`

---

## Appendix — superseded

- `2026-06-26-master-final-workbench-spec.md` — engineering-closure-led; deleted. UI direction here overrides it. Backend remediation from it survives as WS4.

---

# Detailed UI Implementation Design Addendum (WS1-ready)

> Grounding: ValueCell dialog/form patterns (`dialog.tsx`, `ai-model-form.tsx`, `model-detail.tsx`, `model-providers.tsx`) + the current `v4/` components + the real API contract in `api/types.ts` / `api/client.ts`. This addendum is execution-grade: glmcodex implements directly from it. Color **directions** are offered (A4/A5); exact tokens are decided by the WS1 glance test, not here. Structural values (spacing, type, dimensions, component anatomy) are binding.

## Decision log (2026-06-27, round 2)

1. **Model Settings stays a centered modal** — not converted to ValueCell's literal master-detail page. But three visual variants (A dark elevated / B light solid / C hybrid warm-neutral) must be screenshot-tested before one is chosen.
2. **The three modal variants are tested on the real Catalyst cockpit background**, not as isolated widgets. The current failure modes — panel/background color collision, wrong opacity, K-line chart bleeding into the form — can only be judged in a real screenshot. See the glance-test checklist in A5.4.
3. **Workflow (binding):** Claude spec/prompt → glmcodex `/writing-plans` → manager review → glmcodex `/executing-plans` → screenshot checkpoint → manager verify. glmcodex never goes straight to full implementation.
4. **Backend/API frozen during the UI rescue** — no new endpoints, no contract changes, no credential/backend business-logic changes. Remaining backend work (integration testing, real provider validation, full live attribution, `workspace_projection`/`critic_decision` hardening) is server-side and later. 8GB Mac never runs full pytest / LanceDB / vector / full attribution.

## A0. Default viewport integrity (首屏完整性) — binding acceptance standard

This is currently the single most visible failure: on default load/refresh the page shows truncated blocks, half-cut titles, partially-shown news cards, and disconnected composition. WS1 must fix this and **prove it with a screenshot**.

At **1440×900, on default load AND after refresh**, the first screen (above the fold) must show, complete:

1. header / controls (stable height)
2. market module — K-line chart + session details, complete, as one container
3. Candidate news section with **at least 2 full news cards** (when news exists)

The Attribution workspace MAY be below the fold. **Never truncate candidate news to fit the workspace.** If the workspace title would land at the very bottom of the first screen, it must either start completely or fall naturally to the next screen — no half-cut title.

The first screen MUST NOT show any of:

- a section title visible with no content under it
- a card clipped by the viewport edge
- the chart clipped vertically
- the details panel visually disconnected from the chart
- a large empty void between chart and details/news
- a black seam / gutter between the chart and the right-side details
- bottom content half-visible in a way that reads as broken

Fixed layout budget (binding): header stable (56px) → market module complete (chart + details, one container) → candidate news complete (2–3 cards) → workspace allowed below fold.

**Proof obligations (WS1):**
- a 1440×900 screenshot demonstrating this integrity on default load;
- a 390px screenshot demonstrating: no horizontal overflow, no clipped primary controls, and the Model Settings modal fits within the viewport.

## A1. Page information architecture

The homepage is a single vertical stack of exactly four blocks. No global sidebar.

| # | Block | Responsibility | Appears when | Empty state | Loading state | Error state |
|---|---|---|---|---|---|---|
| 1 | Top control bar | brand + run controls + model settings entry | always | n/a | controls disabled while bootstrapping | inline toast if bootstrap fails |
| 2 | Market module | K-line chart + selected-day session details, one cohesive container | always (after tickers load) | "Select a ticker" prompt | skeleton candles + skeleton stat rows | inline error card with retry |
| 3 | Candidate news | news for the selected date — the attribution input record | after a date is selected | "No candidate news for this date" (designed row, not blank) | 3–4 skeleton cards at real dimensions | inline error card with retry |
| 4 | Attribution workspace | tabbed result (Summary / Trace / Evidence / Diagnostics) | always present; populated after Run | left-aligned "Run attribution to generate a grounded explanation" | stage progression from `stages[]` / `predicted_next_node` | error state from `failure` payload; refusal handled in Summary |

**First-screen contract (1440×900):** blocks 1–3 render complete above the fold; block 4 may begin below it. No block is ever rendered half-cut at the fold line. If vertical space is tight, block 4 moves down — candidate news is never truncated to make room.

## A2. Layout, grid, spacing (binding values)

Base unit 4px. Allowed steps: 4, 8, 12, 16, 20, 24, 32, 40, 48.

| Token | Value |
|---|---|
| Page max-width | 1600px (centered above 1600; fluid below) |
| Page horizontal padding | 24px desktop, 20px tablet, 16px mobile |
| Page top padding | 20px |
| Inter-block vertical gap | 16px |
| Panel padding (news, workspace) | 20px |
| Panel padding (market hero, modal) | 24px |
| Card padding | 16px |
| Control-group gap (header controls) | 12px |
| Label↔control gap | 8px |
| Tight pairing (icon↔text) | 6–8px |

### A2.1 1440×900 vertical budget

| Region | Height | Cumulative top |
|---|---|---|
| Page top padding | 20 | 20 |
| Block 1 — control bar | 56 | 76 |
| Gap | 16 | 92 |
| Block 2 — market module | 380 | 472 |
| Gap | 16 | 488 |
| Block 3 — candidate news | 200 | 688 |
| Gap | 16 | 704 |
| Block 4 — workspace (begins, ready-state header peeks) | — | 704 → below fold |

Usable height after browser chrome ≈ 820–860px → blocks 1–3 finish at 704px with headroom; workspace header peeks and signals scroll.

### A2.2 Market module internal layout (fixes the black seam)

The chart and details are ONE container (L1 surface, 24px padding, single elevation). They are NOT two panels with page canvas between them.

| Element | Spec |
|---|---|
| Container | one panel, 24px padding, radius 10px, single subtle elevation |
| Chart region | flex ~66% (≈8/12 cols) |
| Internal divider | one hairline vertical rule (low-alpha, L1-on-L1), ~20–24px gutter each side — NOT a gap showing canvas |
| Details region | flex ~34% (≈4/12 cols) |
| Title row height | ~40px (ticker + price + change% + date inline) |
| Chart canvas height | ~300px |

### A2.3 Responsive rules

| Breakpoint | Layout |
|---|---|
| 1440 desktop (primary) | 4-block stack; market = chart + details side-by-side; news rail 3–4 cards |
| 768 tablet | market details wrap below the chart (still same container); news 2 columns; header controls may wrap to 2 rows; controls grow toward 44px |
| 390 mobile | single column; chart stacks above details; news 1 column (vertical list); header controls collapse (ticker+date row, Run + Model Settings row); all targets ≥44px; **no horizontal overflow** |

## A3. Typography

**Font direction (not Inter):** neutral precise grotesque for UI — Geist or IBM Plex Sans (fallback `system-ui`); a tabular/mono face for all numerics — Geist Mono or IBM Plex Mono. All numerics use `tabular-nums`.

| Role | size / weight / line-height | Example |
|---|---|---|
| Brand wordmark | 16 / 600 / 1.2 | "Catalyst" |
| Section title | 16–18 / 600 / 1.3 | "Candidate news", "Attribution workspace" |
| Chart title | 18 / 600 / 1.25 | "AAPL" (+ "2025-09-02" 13/500 muted, + price/change) |
| Metric value | 18–20 / 600 / 1.2, tabular | "182.34", "+1.8%" |
| Metric label | 12 / 500 / 1.4, muted | "Open", "Volume" |
| Card title (news headline, cause) | 14 / 600 / 1.35, clamp 2 lines | |
| Body / snippet | 13 / 400 / 1.5 | |
| Caption / meta (date, source, tokens) | 11–12 / 500 / 1.4, muted | |
| Button text | 13 / 500 / 1 | |

Three text tiers only (primary / secondary / muted). Sentence case throughout.

**Delete (no information value):** "Workbench", visible "Ticker" label, visible "Session" label, "Daily Price", "Market Session", "Session Overview", "Evidence Intake", "Analysis".
**Keep (real information):** "Catalyst", "AAPL", "AAPL 2025-09-02", "Candidate news", "Attribution workspace", tab names.

## A4. Surface / color / border / shadow

Do not lock the final palette. Define the **hierarchy** (binding) and offer **directions** (choose in WS1).

### A4.1 Surface hierarchy (5 roles, binding relationships)

| Role | Use | Separation rule |
|---|---|---|
| App background | page canvas | base |
| Primary panel | market / news / workspace | perceptibly above canvas (~≥10 L*) |
| Elevated panel / modal | Model Settings, dropdowns | above panels + scrim + shadow |
| Card / control surface | news card, input, active tab | above its panel |
| Interactive states | hover / focus / active | each a distinct, perceptible shift from rest |

### A4.2 Color directions (pick one in WS1)

| Direction | Character | How it avoids "one flat field of same color" |
|---|---|---|
| 1. Dark cockpit | very-dark slate canvas, lighter slate panels, blue action accent, green/red semantic | enforce the L* step per level; cards lift off panels; accent + semantics break the monochrome |
| 2. Light-solid SaaS (Stripe/ValueCell register) | light gray canvas, white panels, dark text, strong borders+shadow | white-on-gray separation is inherently high; borders/shadows define every surface |
| 3. Warm-neutral hybrid (Linear register) | graphite/warm-gray surfaces, off-white text, amber/teal accent | desaturated warm steps + accent; softer than pure dark, clearer than monochrome |

### A4.3 Border / shadow policy

| Where | Treatment |
|---|---|
| Page → panel | NO hard border — fill contrast separates (removes wireframe look) |
| Input fields | keep a visible hairline border + focus ring |
| Active tab | 2px accent underline (not a box) |
| Card on panel | hairline OR a one-step surface lift, not a heavy border |
| Modal | border + shadow + scrim (the elevation trio) |
| Chart container | subtle inset/elevation, no hard border |
| Market internal divider | one hairline rule between chart and details |

### A4.4 The Model-Settings-vs-background fix (explicit)

Current `--v4-surface-elevated:#0d1520` sits ~3 L* above canvas `#060912` → invisible. Required regardless of direction: modal surface ≥~10 L* from canvas + scrim (stronger than the current `rgba(2,6,23,0.72)`, optionally blurred) + a real shadow. The panel must read as one independent surface; the K-line chart behind must not compete with the form fields.

## A5. Model Settings UX (ValueCell-grounded)

### A5.1 Modal mechanics (from ValueCell `dialog.tsx`, adapted)

| Property | Spec |
|---|---|
| Render | portal to body, centered (`fixed` + translate -50%) |
| Scrim | flat dark, stronger than ValueCell's `bg-black/50` for the cockpit (≈0.6–0.7), optional blur |
| Panel | solid elevated surface, 1px border, radius 8–10px (`rounded-lg`), padding 24px (`p-6`), shadow-lg |
| Max width | ~512px (`max-w-lg`); `max-w-[calc(100%-2rem)]` on mobile |
| Close | top-right icon button (size-4), focus ring, `sr-only` label |
| Header height | unchanged when modal opens (portal isolation — confirm) |
| Focus | real trap (Tab cycles), ESC + backdrop close, return focus on close, body scroll lock |

### A5.2 Flow (single-column, adapted from ValueCell)

1. Click Model Settings trigger in header → modal opens.
2. **Provider** select (curated catalog from `GET /api/models/catalog`).
3. **API key** — password input with inline eye/eye-off toggle (ValueCell `InputGroupAddon align="inline-end"` pattern).
4. **Test connection** — explicit action calling `POST /api/models/validate`; inline result (success/error) via semantic tokens (not hardcoded green/red). Non-blocking, but model select is gated behind a successful test (ValueCell tests via "Check Availability"; we gate to keep BYOK honest).
5. On success, **model** select appears (populated from the provider's `models[]`).
6. **Use model** — explicit confirm (we do NOT auto-save on blur like ValueCell — risky for keys). Persists to component state + optional localStorage "remember key".
7. Re-open to change provider / key / model. Custom provider adds Base URL + Model ID fields.

Frontend default credential source = `browser_key`. `server_env` is supported by the backend but NOT surfaced as the default flow.

### A5.3 Form sizing & states

| Element | Spec |
|---|---|
| Field group gap | 16px |
| Label | 12–13 / 500, muted-foreground |
| Input height | 36px, visible border, clear focus ring |
| Buttons | height 36px (Test) / 38px (Use model, primary); primary right, secondary/cancel left |
| Required handling | inline field error below the input, sentence-case message |

| State | Treatment |
|---|---|
| idle / unconfigured | trigger shows "Model settings"; modal fields empty |
| typing key | model select hidden until tested |
| testing | Test button shows progress; mutating controls disabled (single `isBusy` flag) |
| connected (`status: ready`) | inline success row + latency; model select revealed |
| auth_failed / request_failed / timeout / invalid | inline error row with plain-language message; no raw exception |

### A5.4 Three modal visual options (decide by glance test in WS1)

A. Dark elevated, high-contrast · B. Light solid (ValueCell) · C. Hybrid warm-neutral.

These are NOT isolated widget mockups. glmcodex must render each variant **open over the real Catalyst cockpit** (live K-line chart visible behind the scrim) and capture a screenshot. The current problems — panel colliding in color with the page background, wrong backdrop opacity, the chart bleeding into the form — are only judgeable in a real screenshot.

**Glance-test checklist (every variant must be evaluated against all of these):**

- the panel reads as one independent surface, clearly separated from the page background (no color collision);
- inputs, selects, and buttons are unambiguously legible against the panel;
- the backdrop suppresses the background enough to focus the form, without looking dirty or muddy;
- the K-line chart behind does not bleed through and does not reduce form readability;
- the close / Test connection / Use model controls have sufficient contrast and adequate touch targets (≥44px on touch breakpoints).

The human picks the winning variant from the screenshots; tokens are locked only then.

## A6. Component-level anatomy

### A6.1 Header (control bar)
Height 56px, horizontal padding 24px, controls gap 12px. Left: "Catalyst" wordmark (16/600). Right cluster: ticker select → date control → Run Attribution (primary) → Model Settings (icon+label, status dot). No "Workbench", no "Ticker"/"Session" text labels.

### A6.2 Ticker & date controls
Ticker = a select whose displayed value IS the label (e.g. "AAPL") — self-explanatory without a separate caption. Date = a date control showing the selected trade date (e.g. "2025-09-02"). Both 36px height.

### A6.3 Run Attribution button
States: **disabled** (no model configured or no date) muted, non-interactive with a tooltip reason; **ready** accent fill; **running** shows progress + becomes a stop/disabled affordance; **error** returns to ready with an inline error near the workspace, never a raw exception.

### A6.4 Model Settings trigger
States: **unconfigured** (neutral, "Model settings"); **configured** (shows provider/model summary + a status dot); **connected** (dot in success color). Opening it never changes header height.

### A6.5 Market chart
Title area: "AAPL" (18/600) + date (13/500 muted) + last price + change% (semantic color), left-aligned. Canvas: candlesticks, ~300px tall, click-to-select-date. Instruction (e.g. "Click a candle to inspect that day") placed as a quiet caption near the chart, not a heavy banner. No "Daily Price".

### A6.6 Session details
A compact metric grid / stat rows: label (12/500 muted, left) + value (18–20/600 tabular, right). Rows: open, high, low, close, volume, change%, range. Up = success color, down = danger color, flat = muted. Optional readiness rows (data availability) in the same rhythm.

### A6.7 Candidate news
| Property | Spec |
|---|---|
| Grid | `auto-fit minmax(280px, 1fr)`; 3–4 cols at 1440, 2 at 768, 1 at 390 |
| Card | min-height 120px, padding 16px, radius 8px |
| Content order | source tag (11/500 muted) · date (11/500 muted) · headline (14/600, clamp 2) · optional snippet (13/400, clamp 2) |
| Empty / loading / error | designed row / skeleton cards / inline error (per A1) |

### A6.8 Attribution workspace
Tab bar height 40px; tabs 36px; active = accent underline. Post-run layouts (bind to contract, A7 / §9), graceful when fields absent:
- **Summary**: output status chip, grounding rate (tabular), `summary_md` (rendered markdown), causes list (text + category/direction/confidence + linked evidence ids). Refusal/insufficient → state the reason, no fabricated causes.
- **Trace**: stage rows (label, status, duration_ms, tokens, cost, summary), last/next node.
- **Evidence**: evidence rows (headline, snippet, source, scores, critic relevance/category/reasoning, decision, cited). Null `critic_decision` renders as "ungraded" per-item — never collapses the whole list.
- **Diagnostics**: counts (retrieved/reranked/graded/cited), top evidence score, totals (tokens, cost), run meta (runtime, model).

**No deformation after Run** is the hard rule, validated by A7.

## A7. Contract-aligned fixture (visual test of post-run UI)

A dev/test fixture built from the real `WorkspaceResponse` interfaces (api/types.ts) — NOT marketing mock. One shared view-model type is the single source both this fixture and the live adapter conform to. Required variants:

1. normal completed attribution (causes + graded evidence)
2. refusal / no result (`output_status` set, empty `causes`)
3. failure (`failure` payload present)
4. empty evidence (`evidence: []`)
5. long text (very long `summary_md`, long headlines — overflow/clamp)
6. many evidence items (scroll/virtualize behavior)
7. missing optional diagnostics (`diagnostics: null`, null numerics)

Use: render each variant into Summary / Trace / Evidence / Diagnostics and snapshot — assert no layout deformation and no placeholder leakage. A contract test fails if the fixture diverges from the typed contract.

## A8. Backend / API status (do not expand now)

All endpoints the frontend needs already exist (verified in `api/client.ts`):

| Need | Endpoint |
|---|---|
| tickers | `GET /api/tickers` |
| OHLCV / range | `GET /api/ohlcv/{ticker}`, `GET /api/range-local` |
| news | `GET /api/news/{ticker}` |
| fundamentals | `GET /api/fundamentals/{ticker}` |
| model catalog | `GET /api/models/catalog` |
| model validate | `POST /api/models/validate` |
| create live run | `POST /api/live-runs` |
| run status / events / artifacts | `GET /api/live-runs/{id}`, `/events`, `/artifacts` |
| workspace | `GET /api/live-runs/{id}/workspace` |
| retry / cancel / health | `POST /retry`, `/cancel`, `/cancel-all`, `GET /api/health/runtime` |

**Do not add new APIs during the UI rescue.** Remaining backend work (WS4, later, on the server — not the 8GB Mac): integration testing, server-only tests, real provider validation, full live attribution, and the `workspace_projection` / `critic_decision` fix. The 8GB Mac does NOT run full pytest / LanceDB / vector / full attribution.

## A9. glmcodex work plan (spike-first, screenshot-gated)

Process for every workstream: glmcodex uses `superpowers:writing-plans` to write the plan → manager reviews and approves → glmcodex uses `superpowers:executing-plans` → manager verifies against the acceptance checklist (A10). Do not big-bang the whole project.

| WS | Scope | Deliverables | Guardrails |
|---|---|---|---|
| **WS1 — Visual spike** | frontend layout / CSS / component composition only | (1) 1440×900 screenshot; (2) 390px screenshot; (3) three Model Settings modal variants over the real cockpit | NO backend, NO API contract, NO business-logic changes |
| **WS2 — Post-run parity** | shared view-model + contract fixture + fix adapter dropped fields | snapshot tests across A7 variants; live==demo render | no contract changes |
| **WS3 — Model Settings polish** | apply chosen modal tokens, focus trap, scroll lock, flow polish | final modal screenshots | — |
| **WS4 — Backend hardening** | critic_decision projection, BYOK test-green, single model source, exception scrub | server-side; single-worker deploy note | run on server, not 8GB Mac |

WS1 is gated by a screenshot checkpoint: the manager reviews the three deliverables and picks the modal direction before WS2 begins.

## A10. Acceptance checklist (WS1 visual spike)

- **Default viewport integrity (A0) passes**, proven by the 1440×900 screenshot: header, market module (complete chart + details), and candidate news with ≥2 full cards are all complete above the fold; workspace may be below the fold; none of the A0 "must not show" failures are present.
- chart + details read as one cohesive module — no black seam.
- no invalid section eyebrows remain (A3 delete list applied).
- Model Settings modal is clearly distinct from the background; the form is readable; the chart does not bleed through.
- header does not deform when the modal opens.
- all controls have consistent spacing/padding per A2.
- 390px: no horizontal overflow.
- browser console is clean.
- `npm run build` + frontend tests pass.
- no server-only heavy tests run on the 8GB Mac.
