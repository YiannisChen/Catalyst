# Catalyst Attribution Workbench UI/UX Design

Date: 2026-05-26
Scope: design document only. No implementation changes.

## 1. Product Positioning

Catalyst is a financial event attribution workbench, not a stock prediction tool.

The product answers: for a selected `ticker` and `trade_date`, what market move occurred, what evidence was considered, and which events or information may plausibly explain that day's price movement?

The primary value is explainability and auditability:

- Select a ticker/date from a K-line chart.
- Inspect the selected day's price context.
- Run attribution analysis against available evidence.
- Review concise attribution results.
- Audit the MCJ pipeline node by node, including evidence, reasoning summaries, artifacts, and raw output.

Catalyst should not imply price forecasting, trading advice, or guaranteed causality. It should consistently frame outputs as evidence-grounded attribution hypotheses with visible quality and failure states.

## 2. Page Layout

Recommended page: single-page `Attribution Workbench`.

Desktop defense scenario is the primary target. At `1440px+`, use a three-zone layout:

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ Context Bar: ticker | date/range | query | runtime health | model/status      │
├───────────────────────────────┬──────────────────────────────┬───────────────┤
│ Main Analysis Column           │ Evidence / Summary Column     │ Runtime       │
│                               │                              │ Console       │
│ KLinePanel                    │ AttributionSummary            │ NodeTimeline  │
│ TradingView-style chart       │ EvidencePreview               │ NodeCards     │
│                               │ NewsPlaceholder               │ ArtifactTabs  │
│ SelectedDayContext            │                              │               │
└───────────────────────────────┴──────────────────────────────┴───────────────┘
        DetailDrawer / Artifact Detail Viewer opens from bottom or right
```

Suggested desktop widths:

- Left main chart column: `minmax(620px, 1fr)`.
- Middle insight column: `360-440px`.
- Right runtime console: `360-420px`.
- Page gutter: `16px` or `24px`, fixed across panels.

Responsive behavior:

- `>=1440px`: three columns.
- `1024-1439px`: chart + selected day on left, summary + runtime in right stacked tabs.
- `<1024px`: single column with tabs: `Chart`, `Attribution`, `Runtime`, `Artifacts`.

Information hierarchy:

- The chart remains the visual center.
- The selected day context is the primary action surface.
- Attribution summary is concise by default.
- Runtime console is transparent but not allowed to become a full-page log wall.
- Artifact raw output lives in a drawer/tabs, not in the main chart surface.

## 3. K-line Interaction Design

Use TradingView Lightweight Charts, or a TradingView-style candlestick component if the exact package is not selected.

Required interactions:

- Hover candle shows OHLCV tooltip:
  - date
  - open / high / low / close
  - volume
  - daily return
  - intraday range
- Click candle sets selected context:
  - `selectedTicker`
  - `selectedTradeDate`
- Selected candle shows a marker or highlight:
  - vertical guide line
  - selected candle outline
  - small date marker on x-axis
- Chart markers are lightweight only:
  - run status marker
  - attribution available marker
  - failure marker
  - insufficient/partial marker

Do not place long attribution text inside the chart. The chart should show price context and event availability, while explanation appears in `AttributionSummary`, `EvidencePreview`, and `RuntimeConsole`.

Chart guidance:

- Visible candle count: target `120-250`, avoid rendering more than `500` candles at once.
- Bullish candle: `--chart-up`.
- Bearish candle: `--chart-down`.
- Volume bars: same direction colors at `35-45%` opacity.
- Markers must include shape or icon differences, not color alone.
- Provide accessible text summary outside the chart for selected candle values.

## 4. Selected Day Context

`SelectedDayContext` appears directly below the K-line chart. It is the main bridge between market data and attribution.

Fields:

- ticker
- trade date
- open
- high
- low
- close
- volume
- daily return
- intraday range
- selected date status:
  - no run
  - queued/running
  - succeeded
  - partial
  - insufficient
  - failed request
  - failed system

Primary CTA:

- `Run Attribution`

CTA rules:

- This is the only primary button on the page.
- Disable while required context is missing.
- Disable or show loading while a run for the same ticker/date is actively queued/running.
- Show inline validation for unsupported ticker/date before calling `POST /api/live-runs` when possible.

Suggested layout:

- First row: ticker, date, status chip, Run Attribution.
- Second row: OHLC summary with tabular figures.
- Third row: return/range/volume and concise helper text if the selected date has no attribution yet.

## 5. News / Evidence Design (P1 Only)

The current system does not have a formal news API. P1 must not present a full news feed or fake related articles.

Use the label:

`Evidence Used by This Run`

This panel may display evidence-like content from attribution artifacts:

- `retrieved_chunks`
- `reranked_chunks`
- `graded_evidence`
- `all_graded_chunks`

Evidence preview behavior:

- Default show top `3-5` evidence items.
- Each item shows title/source/date if present in artifact payload.
- If source metadata is missing, show a muted `metadata unavailable` label.
- Show grading/rank score only when provided by the artifact.
- Each item can expand into excerpt, payload metadata, and related node/event.
- Raw payload opens in `DetailDrawer`.

### Placeholder (Keep Explicit)

A `Related News` panel may exist only as a placeholder/empty state.

Required empty-state copy:

`Related market news is not available in this release.`

Supporting text:

`This page currently shows only evidence used by the attribution run. Full date-range news exploration is planned in a future release.`

No synthetic news cards should be shown.

## 6. Attribution Analysis Placement

Product decision:

- Primary entry: `Run Attribution` in `SelectedDayContext`.
- Secondary entry: `Run` / `Retry` in the `RuntimeConsole` header.
- Result display: `AttributionSummary`.
- Process transparency: `RuntimeConsole`.
- Artifact raw payloads and raw LLM response: `DetailDrawer` / `ArtifactTabs`, collapsed by default.

Rationale:

- Users first select market context, then ask for attribution.
- The chart and selected day stay stable while the run progresses.
- Runtime details are visible but not allowed to dominate the main analysis surface.
- Raw outputs are available for audit without overwhelming the default page.

## 7. Attribution Summary

`AttributionSummary` is the main answer surface. It should summarize rather than reproduce the full model response.

Required fields:

- status:
  - `SUCCEEDED`
  - `PARTIAL`
  - `INSUFFICIENT`
  - `FAILED_SYSTEM`
  - `FAILED_REQUEST`
- one-sentence conclusion
- top causes
- evidence count
- grounding / quality hints when available
- actions:
  - retry
  - view details

Default summary rules:

- Maximum `3-5` bullets.
- One sentence conclusion should be `<= 180` characters when possible.
- Top causes should show cause label, confidence/quality hint if available, and evidence count.
- Raw response remains collapsed.
- If no `judge_summary` exists, derive a cautious summary from `judge_causes`, `validator_decision`, or status, and label it as limited.

Suggested card structure:

```text
Attribution Summary
[Status Chip] Evidence: 12 chunks | Quality: limited/adequate/high

Conclusion sentence.

Top Causes
1. Cause A - 4 evidence items
2. Cause B - 3 evidence items
3. Cause C - 2 evidence items

[Retry] [View Details]
```

## 8. Runtime Console

The right console shows MCJ process transparency.

Nodes:

- `miner`
- `critic`
- `decision_router`
- `judge`
- `validator`
- `finalizer`
- `insufficient_handler`
- `system_error_handler`

Important backend limitation:

The backend does not expose a true `current_node`. UI must use:

- `last_completed_node`
- `predicted_next_node`

Copy must avoid implying live in-flight certainty. Use labels like:

- `Last completed`
- `Predicted next`
- `Waiting for next event`

Avoid:

- `Currently running judge`
- `Active node`
- `Live executing node`

### Node Card

Each `NodeCard` displays:

- node name
- status derived from run events
- latency
- input/output tokens
- cost
- short reasoning or decision summary if available from artifacts
- expand/collapse details

Default expansion:

- Expand the latest meaningful completed node.
- If failed, expand the failing node or `error_snapshot`.
- If insufficient, expand `critic`, `judge`, or `insufficient_handler`, depending on artifacts.
- Keep other nodes collapsed.

### Artifact Tabs

Tabs:

- Retrieval top-k
- Reranked list
- Graded evidence
- Critic decision
- Judge causes
- Summary
- Raw LLM response
- State snapshot
- Error snapshot

Artifact mapping:

| Tab | Artifact types |
| --- | --- |
| Retrieval top-k | `retrieved_chunks` |
| Reranked list | `reranked_chunks` |
| Graded evidence | `graded_evidence`, `all_graded_chunks` |
| Critic decision | `critic_decision` |
| Judge causes | `judge_causes` |
| Summary | `judge_summary`, `validator_decision` |
| Raw LLM response | `raw_llm_response` |
| State snapshot | `state_snapshot` |
| Error snapshot | `error_snapshot` |

Raw output rules:

- Use monospace viewer.
- Height limit: `280-420px`.
- Add copy action.
- Add wrap toggle.
- Add JSON tree view where feasible.
- Never allow raw output to occupy the main chart space by default.

## 9. Failure UX

All failure states must retain the chart and selected day context. Users should never lose the selected ticker/date after a failed run.

| Status | Headline | sub_reason display | Retry | Artifacts | Keep chart/context |
| --- | --- | --- | --- | --- | --- |
| `FAILED_REQUEST` | `This ticker/date cannot be analyzed.` | Inline explanation near selected context and summary banner. Include unsupported ticker/date or invalid query field when available. | Usually no, unless backend `retryable=true`. | Show request/failure artifacts if returned. | Yes |
| `FAILED_SYSTEM` | `Runtime failed before attribution completed.` | Show system/model/timeout/dependency reason in `FailureBanner`, with source/node if present. | Yes when `retryable=true`. | Show `error_snapshot`, `state_snapshot`, partial node events. | Yes |
| `INSUFFICIENT` | `Evidence was insufficient for reliable attribution.` | Explain which evidence or validation gate was insufficient when available. | Optional; allow retry with different model/query if retryable. | Show retrieval, critic, judge, validator artifacts. | Yes |
| `PARTIAL` | `Attribution completed with limited confidence.` | Show missing/weak areas as quality hints. | Yes as secondary. | Show all available artifacts and clearly mark gaps. | Yes |
| `SUCCEEDED` | `Attribution completed.` | No failure reason; show quality/grounding hints if available. | Retry as secondary only. | Show all artifacts. | Yes |

Failure banner rules:

- Use semantic color plus icon/text; never color alone.
- Headline should be human-readable.
- Technical details should be expandable.
- `sub_reason` appears as muted metadata or a detail row, not as the only message.
- For failed/partial runs, `View Details` opens the relevant artifact tab.

## 10. Catalyst UI Spec: Density and Typography Rules

Text alignment and wrapping:

- Long titles wrap by default.
- Narrow columns use ellipsis plus tooltip or expand action.
- News/evidence titles max `2-3` lines.
- Metadata values use tabular figures where numeric.
- Raw JSON wraps only when user enables wrap.

Hierarchy:

- Page title: `24-28px`, `600-700`.
- Panel title: `16-18px`, `600`.
- Section title: `14-15px`, `600`.
- Body: `14-15px`, `400-500`.
- Metadata label: `12px`, muted, mono or tabular.

Actions:

- Only primary button: `Run Attribution`.
- Secondary actions: `Retry`, `Reset`, `View Details`, `Copy`.
- Destructive or reset actions stay visually separate from attribution action.

Radius:

- outer panel: `16px`.
- inner card: `12px`.
- chip: `8px`.
- pill: `999px`.

Shadows:

- Main data regions prefer border and surface contrast.
- Drawers, popovers, and floating overlays may use soft elevation.
- Avoid large dirty black shadows.

Spacing:

- Scale: `4 / 8 / 12 / 16 / 24 / 32`.
- Panel gutter: `16px` at desktop, `12px` at tablet.
- Dense but not cramped: list rows should keep `12-16px` vertical padding.
- Nested panels need clear background contrast, not stacked heavy shadows.

Accessibility:

- Normal text contrast `>= 4.5:1`.
- Secondary text contrast `>= 3:1`.
- Focus ring visible on all interactive elements.
- Icon-only controls require accessible labels.
- Status must use text and shape/icon, not color alone.
- Runtime updates should use polite live regions, not focus stealing.

## 11. Visual Direction (Finance-Focused)

Recommended direction from `ui-ux-pro-max`:

- Product profile: `Financial Dashboard` + `Analytics Dashboard`
- Style profile: `Dark Mode (OLED) + Data-Dense` with `Minimalism`
- Color focus: `Dark background + red/green alerts + trust blue`

Design intent:

- Institutional research workbench, not crypto marketing page.
- Dense but readable; hierarchy comes from spacing and typography first.
- No purple/pink AI gradients, no heavy glassmorphism, no decorative glow.

### Color Tokens

```css
:root {
  --bg-app: #020617;
  --bg-panel: #0f172a;
  --bg-panel-raised: #111c33;
  --bg-subtle: #0b1324;
  --surface-hover: #16233d;
  --border-subtle: #243247;
  --border-strong: #36506f;

  --text-primary: #f8fafc;
  --text-secondary: #cbd5e1;
  --text-muted: #94a3b8;
  --text-inverse: #081018;

  --primary: #3b82f6;
  --primary-hover: #60a5fa;
  --accent: #22c55e;
  --focus-ring: #60a5fa;

  --status-success: #22c55e;
  --status-partial: #f59e0b;
  --status-insufficient: #94a3b8;
  --status-request-failed: #ef4444;
  --status-system-failed: #dc2626;
  --status-running: #3b82f6;
  --status-queued: #94a3b8;
}
```

### Typography Scale

- Font family:
  - UI/body: `IBM Plex Sans`, `Fira Sans`, system sans.
  - Data/metadata/raw: `IBM Plex Mono`, `Fira Code`, monospace.
- Scale:
  - `12px` metadata
  - `13px` dense table cell
  - `14px` body compact
  - `16px` default body/panel title
  - `20px` section headline
  - `24px` page headline
  - `28px` top-level page title if needed
- Line height:
  - body: `1.45-1.6`
  - metadata: `1.35`
  - raw: `1.5`

### Spacing Scale

- `--space-1: 4px`
- `--space-2: 8px`
- `--space-3: 12px`
- `--space-4: 16px`
- `--space-6: 24px`
- `--space-8: 32px`

### Radius Scale

- `--radius-chip: 8px`
- `--radius-card: 12px`
- `--radius-panel: 16px`
- `--radius-pill: 999px`

### Border and Surface Rules

- Panels use `1px solid --border-subtle`.
- Selected/active panel uses `--border-strong` plus subtle background lift.
- Avoid nested card shadows. Use spacing and border color to separate layers.
- Use alternating row backgrounds only for dense tables, with low contrast.

### Shadow / Elevation Scale

- `elevation-0`: none, default for panels.
- `elevation-1`: `0 6px 18px rgba(2, 6, 23, 0.28)` for popovers.
- `elevation-2`: `0 14px 40px rgba(2, 6, 23, 0.36)` for drawers/modals.
- No heavy opaque black card shadows in the main layout.

### Status Colors

| Status | Token | UI treatment |
| --- | --- | --- |
| `QUEUED` | `--status-queued` | muted pill, clock icon |
| `RUNNING` | `--status-running` | blue pill, progress indicator |
| `SUCCEEDED` | `--status-success` | green pill, check icon |
| `PARTIAL` | `--status-partial` | amber pill, split/limited icon |
| `INSUFFICIENT` | `--status-insufficient` | gray-blue pill, evidence icon |
| `FAILED_REQUEST` | `--status-request-failed` | red outline pill, validation icon |
| `FAILED_SYSTEM` | `--status-system-failed` | red filled/strong pill, alert icon |

### Chart Colors

```css
--chart-up: #22c55e;
--chart-down: #ef4444;
--chart-up-muted: rgba(34, 197, 94, 0.42);
--chart-down-muted: rgba(239, 68, 68, 0.42);
--chart-grid: #223046;
--chart-axis: #94a3b8;
--chart-crosshair: #cbd5e1;
--chart-selected: #60a5fa;
--chart-marker-run: #3b82f6;
--chart-marker-failure: #dc2626;
--chart-marker-insufficient: #94a3b8;
```

### Evidence / Artifact Badge Colors

| Badge | Color |
| --- | --- |
| retrieved | blue outline |
| reranked | cyan outline |
| graded | green/amber based on grade |
| critic | amber outline |
| judge | violet-free indigo-blue outline |
| validator | green/gray outline |
| raw | neutral mono gray |
| error | red outline |

Avoid purple/pink AI gradients. Blue is for trust/action, green/red is for market direction and risk states.

## 12. API-to-UI Mapping

| API endpoint | UI component | Loading state | Empty state | Error state | Refresh / polling |
| --- | --- | --- | --- | --- | --- |
| `GET /api/tickers` | `TickerSelect`, `ContextBar` | skeleton input / disabled select | `No tickers available` | inline error with retry | Fetch on app load; manual retry |
| `GET /api/ohlcv/{ticker}?start_date=&end_date=` | `KLinePanel`, `CandleTooltip`, `SelectedDayContext` | chart skeleton with reserved height | `No OHLCV data for this range` | chart error panel with retry | Fetch on ticker/range change; no polling |
| `GET /api/range-local` | `DatePicker`, `RangePicker`, default chart range | muted loading chip | `Local data range unavailable` | non-blocking warning | Fetch on app load; manual refresh |
| `POST /api/live-runs` | `RunAttributionButton`, `AttributionSummary`, `RuntimeConsole` | button spinner, optimistic queued state | not applicable | request error banner; preserve selection | Triggered by user only |
| `GET /api/live-runs/{run_id}` | `AttributionSummary`, `RuntimeConsole`, `FailureBanner` | summary skeleton / status shimmer | `Run not found` | status fetch error with retry | Poll every `1-2s` while queued/running; stop on terminal status |
| `GET /api/live-runs/{run_id}/events` | `NodeTimeline`, `NodeCard` | node skeleton rows | `No node events yet` | console warning, keep run summary visible | Poll every `1-2s` while active, use `after_seq` when possible |
| `GET /api/live-runs/{run_id}/artifacts` | `EvidencePreview`, `ArtifactTabs`, `DetailDrawer` | artifact tab skeleton | `No artifacts available for this run` | artifact error panel with retry | Fetch after new events and terminal status; optionally filter by `event_seq`/`artifact_type` |
| `POST /api/live-runs/{run_id}/retry` | `RuntimeConsole`, `AttributionSummary` | secondary button spinner | retry disabled if not retryable | retry error banner | Triggered by user; then poll new run id |
| `GET /api/health/runtime` | `HealthBadge`, `ContextBar` | small badge skeleton | `Runtime health unavailable` | degraded/failed badge with details popover | Poll every `15-30s`; manual refresh |

## 13. Component Inventory

- `AppShell`: owns the single-page responsive workbench grid.
- `ContextBar`: ticker/date/query/runtime health and current run status.
- `TickerSelect`: searchable ticker selector from `GET /api/tickers`.
- `DatePicker / RangePicker`: date/range input constrained by `GET /api/range-local`.
- `KLinePanel`: TradingView-style OHLCV chart.
- `CandleTooltip`: OHLCV hover/tap details.
- `SelectedDayContext`: selected ticker/date metrics and primary run entry.
- `RunAttributionButton`: primary CTA, loading, disabled, and validation states.
- `AttributionSummary`: concise final/partial/insufficient/failure summary.
- `EvidencePreview`: evidence used by current run, sourced only from artifacts in P1.
- `NewsPlaceholder`: explicit pending-state panel for future news filtering API.
- `RuntimeConsole`: right-side run transparency area.
- `NodeTimeline`: compact node progression and status visualization.
- `NodeCard`: expandable node details.
- `ArtifactTabs`: grouped artifact navigation.
- `RetrievalList`: retrieved chunk preview.
- `RerankList`: reranked chunk preview.
- `GradedEvidenceTable`: graded evidence, quality, and reason snippets.
- `RawResponseViewer`: monospace, bounded, copyable raw payload viewer.
- `FailureBanner`: status-specific recovery and explanation surface.
- `HealthBadge`: runtime health summary with component popover.
- `DetailDrawer`: artifact, raw JSON, and long reasoning viewer.

## 14. Scope Boundary (P1 Only)

### P1

- ticker list
- K-line/OHLCV chart
- candle click selection
- selected day context
- run attribution
- polling run/events/artifacts
- attribution summary
- runtime console
- evidence from attribution artifacts
- retry
- failure states

P1 must be honest about limitations:

- no real full news feed
- no fundamentals/earnings panel unless backed by data
- no category/sentiment/source filtering
- no guarantee that cloud GPU/LLM runtime succeeds
- failed, partial, and insufficient are first-class states

## 15. Final Recommendations

### Recommended Page Layout

Use a dark, high-contrast, three-column Attribution Workbench:

- left: K-line chart plus selected day context
- middle: attribution summary plus evidence used by the run
- right: runtime console with node cards and artifact tabs
- drawer: raw artifacts and long-form reasoning

This layout keeps ticker/date as the core context, preserves the TradingView-style product idea, and avoids turning the page into a dense log viewer.

### P1 Page Priority

1. `ContextBar`, ticker loading, range loading, runtime health.
2. `KLinePanel` with hover tooltip and candle selection.
3. `SelectedDayContext` with OHLCV metrics and `Run Attribution`.
4. Live run creation and polling.
5. `AttributionSummary` for terminal and partial states.
6. `RuntimeConsole` with node events and predicted-next wording.
7. `EvidencePreview` sourced from artifacts.
8. `DetailDrawer` for raw payloads.
9. Failure banners and retry UX.
10. `NewsPlaceholder` with explicit pending API copy.

### Development Order

1. Build the shell and responsive grid with stable panel sizes.
2. Wire stable read APIs: tickers, range, OHLCV.
3. Implement K-line selection and selected day context.
4. Add run creation, status polling, event polling, and health polling.
5. Add attribution summary mapping from artifacts/status.
6. Add runtime console node timeline and node cards.
7. Add artifact tabs, evidence preview, raw response viewer, and detail drawer.
8. Add failure-specific banners and retry flows.
9. Add accessibility pass: keyboard nav, focus rings, ARIA labels, live regions, contrast.
10. Add final visual polish: spacing, wrapping, chart markers, loading/empty/error states.

### P1 API Contract (Current)

This design is constrained to currently implemented backend APIs:

- `GET /api/tickers`
- `GET /api/range-local`
- `GET /api/ohlcv/{ticker}`
- `POST /api/live-runs`
- `GET /api/live-runs/{run_id}`
- `GET /api/live-runs/{run_id}/events`
- `GET /api/live-runs/{run_id}/artifacts`
- `POST /api/live-runs/{run_id}/retry`
- `GET /api/health/runtime`

No additional endpoints are assumed in this document.

## 16. UI/UX Review Checklist (P1 Go/No-Go)

Use this list for final design/code review:

- Accessibility:
  - Body text contrast >= 4.5:1; secondary text >= 3:1.
  - Keyboard focus ring visible on all interactive controls.
  - Status chips include icon/text, not color-only.
- Interaction:
  - Only one primary CTA (`Run Attribution`) on the page.
  - Buttons/interactive rows meet >= 44x44px hit target.
  - Loading/disabled/error states are explicit for run actions.
- Layout:
  - No horizontal scroll at 375/768/1024/1440.
  - Three-zone desktop layout degrades to stacked/tabbed layout below 1440.
  - Runtime console does not displace chart as primary surface.
- Chart:
  - OHLCV tooltip values visible and readable.
  - Selected candle has non-color-only marker.
  - Selected candle text summary exists outside chart canvas.
- Runtime transparency:
  - UI wording uses `Last completed` and `Predicted next`.
  - No `current node` wording appears in labels or helper text.
- API consistency:
  - UI uses only current 9 P1 endpoints.
  - No `/api/news*`, fundamentals, or category/sentiment API calls.
  - Artifact fixture set includes at least two payload shapes: `retrieved_chunks` and `error_snapshot`.
