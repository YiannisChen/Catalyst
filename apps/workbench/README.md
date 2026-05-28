# Catalyst Attribution Workbench

React/TypeScript frontend for interactive financial event attribution analysis.

Connects to the `catalyst-app` backend to display live attribution runs,
evidence previews, node-level pipeline traces, and OHLCV charts.

---

## Quick Start

```bash
cd apps/workbench
npm install
npm run dev
# → http://localhost:5173
```

Requires the backend running on `http://localhost:8000`:

```bash
# In a separate terminal, from the repo root:
uvicorn catalyst_app.main:app --reload --port 8000
```

---

## Scripts

| Command | Description |
|---------|-------------|
| `npm run dev` | Start Vite dev server with HMR |
| `npm run build` | TypeScript check + production bundle |
| `npm run preview` | Preview production build locally |

---

## Tech Stack

| Layer | Choice |
|-------|--------|
| Framework | React 19 + TypeScript |
| Bundler | Vite 7 |
| Charts | D3.js (custom candlestick rendering) |
| Styling | CSS custom properties (dark finance theme) |

---

## Layout

Three-zone layout at desktop (≥1440px):

```
┌──────────────┬──────────────────┬─────────────────┐
│  K-Line      │  Attribution     │  Runtime        │
│  Chart       │  Summary         │  Console        │
│              │                  │                 │
│  Date picker │  Evidence        │  Node timeline  │
│  Context bar │  preview tabs    │  Artifact viewer│
│              │                  │                 │
│  [Run →]     │                  │                 │
└──────────────┴──────────────────┴─────────────────┘
```

Responsive breakpoints: 1440 / 1024 / 768 / 375px.

---

## Backend API Contract

```
GET  /api/tickers
GET  /api/range-local
GET  /api/ohlcv/{ticker}
POST /api/live-runs
GET  /api/live-runs/{run_id}
GET  /api/live-runs/{run_id}/events
GET  /api/live-runs/{run_id}/artifacts
POST /api/live-runs/{run_id}/retry
GET  /api/health/runtime
```

Mock data for offline development: `src/dev/mockRunData.ts`

---

## Project Structure

```
src/
├── api/
│   ├── client.ts               # Typed fetch wrappers
│   └── types.ts                # Shared API response types
├── components/
│   ├── chart/                  # CandlestickChart (D3)
│   ├── context/                # StockSelector, ModelSelector, FundamentalsCard
│   ├── attribution/            # AttributionSummary
│   ├── artifacts/              # ArtifactTabs, EvidencePreview, RawResponseViewer
│   └── runtime/                # NodeTimeline, NodeCard, PipelinePanel
├── hooks/
│   └── useLiveRunPolling.ts    # Polling for live run state
├── state/
│   └── workbench-state.ts      # Global workbench state
└── dev/
    └── mockRunData.ts          # Offline mock fixtures
```
