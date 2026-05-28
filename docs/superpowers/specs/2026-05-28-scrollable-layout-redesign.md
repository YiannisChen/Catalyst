# Scrollable Layout Redesign — Spec

**Goal:** Replace the fixed 100vh layout with a sticky-chart + scrollable-content design that gives every section room to breathe, especially the Attribution runtime area.

## Current Problem

The app is locked in `100vh` with `overflow: hidden`. The chart, attribution sidebar, and pipeline area fight for pixels. On a 1080p screen the pipeline gets ~300px — far too cramped for the core attribution feature. News/Fundamentals are squeezed into the pipeline area. No scrolling is possible.

## Design

### Sticky Zone (top ~45vh)

Fixed to the viewport, never scrolls away:

```
┌──────────────────────────────────────────────────────┐
│  Header: "Catalyst — Attribution Workbench"          │
│  Control Bar: [Ticker ▾] [Date] [Model ▾] [Run Btn] │
├─────────────────────────────────────────┬────────────┤
│  CandlestickChart (D3)                  │ DayContext │
│  occupies remaining height              │ ticker     │
│  (~calc(45vh - 96px))                   │ date       │
│                                         │ OHLC hover │
│                                         │ run status │
└─────────────────────────────────────────┴────────────┘
```

- Header: `flex-shrink: 0`, ~52px
- Control bar: `flex-shrink: 0`, ~44px
- Chart + DayContext: grid `1fr 280px`, fills remaining sticky height
- DayContext replaces the old `attribution-section` sidebar — shows only: selected ticker/date, OHLC hover data, run status chip. All other content (idle guide, running indicator, result summary) moves to the scroll area.

### Scroll Zone (below sticky, rest of viewport height, `overflow-y: auto`)

Contains three vertical sections, each full-width:

#### 1. Market Context (collapsible, default expanded)

Two-column grid `1fr 1fr`:
- Left: `NewsPanel` — cards for ticker/date news
- Right: `FundamentalsCard` — company financials

Collapse header: `▾ Market Context` / `▸ Market Context`. When collapsed, shows only the ~36px header row.

**Default date behavior:** When no `tradeDate` is selected, the app queries `RangeLocalResponse` on mount and sets `tradeDate` to `max_date`. This means News and Fundamentals always have data to show on initial load.

#### 2. Attribution Runtime (always visible)

Full-width section with internal tabs: `Timeline | Artifacts | Attribution`

When no run exists: shows the idle guide (current `attr-idle` content — pipeline flow diagram + hint).

When a run is active/terminal:
- **Timeline tab:** Vertical pipeline flow (Miner → Critic → Judge → Validator) with status, metrics, pulse animation on active node
- **Artifacts tab:** Node-grouped tabs (Miner | Critic | Judge | Validator) showing retrieval chunks, graded evidence, critic decisions, raw LLM responses
- **Attribution tab:** Judge summary + causes breakdown + validator verdict + referenced evidence

No fixed height — content expands naturally. Much more room than the old 300px pipeline area.

#### 3. Run Summary Footer (only when run is terminal)

Compact `AttributionSummary` with retry button for failed runs. Only renders when `isTerminal` is true.

### Removed Elements

- `attribution-section` sidebar (340px) — replaced by compact DayContext in chart area
- `pipeline-area` with `flex: 0 0 300px` — replaced by full-width scroll section
- `pipeline-cols-idle` / `pipeline-cols-run` dynamic grid switching — no longer needed since layout doesn't change between idle and run states
- `PipelinePanel` component's dual-mode layout logic — simplified to always render sections vertically

### CSS Architecture

```css
.app-container {
  height: 100vh;
  display: flex;
  flex-direction: column;
}

.sticky-zone {
  flex-shrink: 0;
  height: 45vh;
  display: flex;
  flex-direction: column;
}

.scroll-zone {
  flex: 1;
  overflow-y: auto;
  overflow-x: hidden;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.market-context-section { /* collapsible 2-col grid */ }
.attribution-runtime-section { /* full width, tabs */ }
.run-summary-footer { /* conditional render */ }
```

### Default Date Logic

In `App.tsx` `useEffect` for initialization:

1. Fetch tickers via `getTickers()`
2. Fetch date range via existing `RangeLocalResponse` endpoint (or call the OHLCV endpoint for the first ticker)
3. If `state.selected.tradeDate` is null, dispatch `SELECT_MARKET` with `{ tradeDate: max_date }`
4. This ensures News and Fundamentals show data immediately on page load

### Component Changes Summary

| Component | Change |
|---|---|
| `App.tsx` | Remove grid layout, replace with sticky-zone + scroll-zone. Move attribution sidebar content. Add default date initialization. |
| `App.css` | Replace `.app-container` / `.chart-area` / `.pipeline-area` layout rules. Add `.sticky-zone` / `.scroll-zone` / `.market-context-section`. |
| `PipelinePanel.tsx` | Simplify: remove idle/run dual-mode grid switching. Always render full-width with tabs. Move News/Fundamentals out to App.tsx scroll zone. |
| `App.tsx` (scroll zone) | Render `NewsPanel` + `FundamentalsCard` in collapsible Market Context section, followed by runtime section (PipelinePanel), followed by conditional AttributionSummary. |

### Constraints

- Do NOT modify backend or `packages/agents/` files
- All existing component logic (ArtifactTabs, AttributionResult, NodeTimeline, RuntimeConsole) stays the same — only layout wrappers change
- Dark theme palette unchanged
- Mobile/responsive is out of scope — desktop-first (≥1280px)
