# Frontend Redesign on DemoUI Base — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the broken workbench frontend with a professional finance dashboard rebuilt on top of the midterm DemoUI (PokieTicker fork), integrating live MCJ pipeline controls, transparent intermediate outputs, and password-gated access.

**Architecture:** Copy DemoUI into `apps/workbench/`, adapt its axios-based API layer to use Catalyst's fetch-based client against `/api/tickers`, `/api/ohlcv/{ticker}`, `/api/live-runs/*`, `/api/models`, and `/api/health/runtime`. Replace the non-functional panels (News, NewsCategoryPanel, company-info section) with Catalyst-specific components: RuntimeConsole, ArtifactTabs, EvidencePreview, and AttributionSummary. Keep the proven D3 CandlestickChart, StockSelector, dark theme CSS, and 100vh grid layout.

**Tech Stack:** React 19, D3 7.9, TypeScript 5.9, Vite 7, fetch API (no axios)

---

## File Structure

### Files to copy from DemoUI (then modify)
- `DemoUI/frontend/src/App.tsx` → `apps/workbench/src/App.tsx` (heavy rewrite)
- `DemoUI/frontend/src/App.css` → `apps/workbench/src/App.css` (extend with new panel styles)
- `DemoUI/frontend/src/components/CandlestickChart.tsx` → `apps/workbench/src/components/chart/CandlestickChart.tsx` (adapt API)
- `DemoUI/frontend/src/components/StockSelector.tsx` → `apps/workbench/src/components/context/StockSelector.tsx` (adapt API)
- `DemoUI/frontend/vite.config.ts` → `apps/workbench/vite.config.ts` (adapt proxy paths)

### Files to port from current workbench (keep as-is or minor adapt)
- `api/client.ts` — fetch-based Catalyst API client (keep verbatim)
- `api/types.ts` — all TypeScript interfaces (keep verbatim)
- `state/workbench-state.ts` — terminal status logic (keep verbatim)
- `hooks/useLiveRunPolling.ts` — polling hook (keep verbatim)
- `components/auth/PasswordGate.tsx` — password gate (keep verbatim)
- `components/runtime/RuntimeConsole.tsx` — run status + failure banners (keep verbatim)
- `components/runtime/NodeTimeline.tsx` — expandable node event list (keep verbatim)
- `components/runtime/NodeCard.tsx` — per-node metrics (keep verbatim)
- `components/artifacts/ArtifactTabs.tsx` — tabbed artifact viewer (keep verbatim)
- `components/artifacts/RawResponseViewer.tsx` — JSON payload display (keep verbatim)
- `components/artifacts/EvidencePreview.tsx` — evidence list display (keep verbatim)
- `components/attribution/AttributionSummary.tsx` — terminal status + retry (keep verbatim)
- `components/attribution/SelectedDayContext.tsx` — day context + run trigger (keep verbatim)

### Files to delete (DemoUI panels with no backing API)
- `components/NewsPanel.tsx` — requires `/api/stocks/{symbol}/articles` (does not exist)
- `components/NewsCategoryPanel.tsx` — requires `/api/stocks/{symbol}/categories` (does not exist)
- `components/AttributionSidePanel.tsx` — company info uses `/api/stocks` metadata (not available in Catalyst's `/api/tickers`)

### Files to create fresh
- `apps/workbench/src/components/context/ModelSelector.tsx` — model dropdown calling `GET /api/models`
- `apps/workbench/src/components/context/RunControlBar.tsx` — unified bar: ticker + date + model + run button
- `apps/workbench/src/components/runtime/PipelinePanel.tsx` — combines RuntimeConsole + ArtifactTabs in a tabbed lower-right panel
- `apps/workbench/src/main.tsx` — app entry point with PasswordGate wrapper
- `apps/workbench/src/index.html` — HTML shell
- `apps/workbench/tsconfig.json` — TypeScript config
- `apps/workbench/package.json` — dependencies (react, d3, no axios)

### Layout Design

```
┌──────────────────────────────────────────────────────────────────┐
│  Header: Catalyst logo │ nav (Attribution · Pipeline · About) │  │
│          ModelSelector dropdown │ health indicator              │
├──────────────────────────────────────────────────────────────────┤
│  RunControlBar: [NVDA ▾] │ 2025-10-28 │ Run Attribution        │
│  OHLC hover data: O $xxx  H $xxx  L $xxx  C $xxx  +x.xx%      │
├────────────────────────────────┬─────────────────────────────────┤
│                                │                                 │
│  D3 CandlestickChart           │  Attribution Result Panel       │
│  (click day → select date)     │  ┌─ AttributionSummary ──────┐ │
│  (hover → OHLC in toolbar)     │  │  Status: SUCCEEDED         │ │
│                                │  │  Causes: [...]             │ │
│  ~48vh                         │  │  Grounding: 1.0            │ │
│                                │  └────────────────────────────┘ │
│                                │  ┌─ EvidencePreview ─────────┐ │
│                                │  │  top 5 evidence items      │ │
│                                │  └────────────────────────────┘ │
├────────────────────────────────┴─────────────────────────────────┤
│  Pipeline Transparency Panel (tabbed)                            │
│  [Runtime Console] [Artifacts] [Raw Response]                    │
│  ┌─ NodeTimeline ─────────────────────────────────────────────┐  │
│  │  miner → critic → judge → finalizer  (cards with metrics)  │  │
│  └────────────────────────────────────────────────────────────┘  │
│  ~1fr (remaining viewport)                                       │
└──────────────────────────────────────────────────────────────────┘
```

Grid: `grid-template-rows: auto auto 48vh 1fr` (header, control bar, chart row, pipeline row)
Chart row: `grid-template-columns: 1fr 360px` (chart left, attribution right)

---

## Task 1: Scaffold — Clean Slate and Copy DemoUI Base

**Files:**
- Delete: all files in `apps/workbench/src/`
- Copy: `DemoUI/frontend/src/App.tsx`, `DemoUI/frontend/src/App.css`, `DemoUI/frontend/src/main.tsx`, `DemoUI/frontend/src/vite-env.d.ts`
- Copy: `DemoUI/frontend/src/components/CandlestickChart.tsx`
- Copy: `DemoUI/frontend/src/components/StockSelector.tsx`
- Modify: `apps/workbench/package.json`
- Modify: `apps/workbench/vite.config.ts`
- Create: `apps/workbench/index.html`
- Create: `apps/workbench/tsconfig.json`, `apps/workbench/tsconfig.app.json`

- [ ] **Step 1: Delete existing workbench src contents**

```bash
rm -rf apps/workbench/src/*
```

- [ ] **Step 2: Copy DemoUI source files**

```bash
# Copy base files
cp DemoUI/frontend/src/App.tsx apps/workbench/src/App.tsx
cp DemoUI/frontend/src/App.css apps/workbench/src/App.css
cp DemoUI/frontend/src/main.tsx apps/workbench/src/main.tsx
cp DemoUI/frontend/src/vite-env.d.ts apps/workbench/src/vite-env.d.ts

# Create component dirs
mkdir -p apps/workbench/src/components/chart
mkdir -p apps/workbench/src/components/context
mkdir -p apps/workbench/src/components/runtime
mkdir -p apps/workbench/src/components/artifacts
mkdir -p apps/workbench/src/components/attribution
mkdir -p apps/workbench/src/components/auth
mkdir -p apps/workbench/src/api
mkdir -p apps/workbench/src/state
mkdir -p apps/workbench/src/hooks

# Copy chart and selector
cp DemoUI/frontend/src/components/CandlestickChart.tsx apps/workbench/src/components/chart/CandlestickChart.tsx
cp DemoUI/frontend/src/components/StockSelector.tsx apps/workbench/src/components/context/StockSelector.tsx
```

- [ ] **Step 3: Create package.json (remove axios, keep d3)**

```json
{
  "name": "catalyst-workbench",
  "private": true,
  "version": "1.0.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "tsc -b && vite build",
    "preview": "vite preview"
  },
  "dependencies": {
    "d3": "^7.9.0",
    "react": "^19.2.0",
    "react-dom": "^19.2.0"
  },
  "devDependencies": {
    "@types/d3": "^7.4.3",
    "@types/react": "^19.2.5",
    "@types/react-dom": "^19.2.3",
    "@vitejs/plugin-react": "^5.1.1",
    "typescript": "~5.9.3",
    "vite": "^7.2.4"
  }
}
```

- [ ] **Step 4: Create vite.config.ts with Catalyst proxy**

```typescript
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.VITE_API_TARGET || 'http://127.0.0.1:8000',
        changeOrigin: true,
        secure: false,
      },
    },
  },
})
```

Note: Removed `base: '/eventus/'` — Catalyst serves from root. Proxy rewrites `/api` directly to FastAPI backend.

- [ ] **Step 5: Create index.html**

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Catalyst — Attribution Workbench</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
```

- [ ] **Step 6: Create tsconfig.json and tsconfig.app.json**

`tsconfig.json`:
```json
{
  "files": [],
  "references": [{ "path": "./tsconfig.app.json" }]
}
```

`tsconfig.app.json`:
```json
{
  "compilerOptions": {
    "target": "ES2020",
    "useDefineForClassFields": true,
    "lib": ["ES2020", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "skipLibCheck": true,
    "moduleResolution": "bundler",
    "allowImportingTsExtensions": true,
    "isolatedModules": true,
    "moduleDetection": "force",
    "noEmit": true,
    "jsx": "react-jsx",
    "strict": true,
    "noUnusedLocals": true,
    "noUnusedParameters": true,
    "noFallthroughCasesInSwitch": true,
    "noUncheckedSideEffectImports": true
  },
  "include": ["src"]
}
```

- [ ] **Step 7: Run npm install to verify dependencies resolve**

```bash
cd apps/workbench && npm install
```

Expected: Clean install, no errors.

- [ ] **Step 8: Stage scaffold**

```bash
git add apps/workbench/
```

---

## Task 2: Port API Layer, Types, and State Management

**Files:**
- Create: `apps/workbench/src/api/client.ts` (copy from current workbench)
- Create: `apps/workbench/src/api/types.ts` (copy from current workbench)
- Create: `apps/workbench/src/state/workbench-state.ts` (copy from current workbench)
- Create: `apps/workbench/src/hooks/useLiveRunPolling.ts` (copy from current workbench)

These files are production-quality and fully tested. They use fetch (not axios) and have zero dependencies on the deleted UI components.

- [ ] **Step 1: Copy API client verbatim**

Copy from the current workbench git history (or the worktree backup). The file at `apps/workbench/src/api/client.ts` contains:
- `request<T>()` — generic fetch wrapper using `/api` prefix
- `getTickers()`, `getOhlcv()`, `getRangeLocal()` — market data
- `createLiveRun()`, `getLiveRun()`, `getLiveRunEvents()`, `getLiveRunArtifacts()` — live run lifecycle
- `retryLiveRun()`, `getRuntimeHealth()`, `getModels()` — utilities

- [ ] **Step 2: Copy types.ts verbatim**

All TypeScript interfaces: `RuntimeStatus`, `ArtifactType`, `CreateRunRequest`, `RunSummaryResponse`, `RunEventResponse`, `ArtifactResponse`, `ModelOption`, `ModelsResponse`, `OhlcvCandle`, `OhlcvResponse`, `TickersResponse`, `RangeLocalResponse`, `FailurePayload`, `RetryMetadata`, `RuntimeHealthResponse`, etc.

- [ ] **Step 3: Copy workbench-state.ts verbatim**

Contains `TERMINAL_STATUSES`, `isTerminalStatus()`, `WorkbenchState`, `createInitialState()`, `reduceRunSummary()`, `reduceLastEventSeq()`, `selectMarketContext()`, `isPollingRequired()`.

- [ ] **Step 4: Copy useLiveRunPolling.ts verbatim**

Polling hook: 1.5s interval, stops on terminal status, uses ref-based callbacks to avoid stale closures.

- [ ] **Step 5: Verify TypeScript compilation**

```bash
cd apps/workbench && npx tsc --noEmit --pretty 2>&1 | head -30
```

Expected: No errors from api/, state/, hooks/ files.

- [ ] **Step 6: Stage**

```bash
git add apps/workbench/src/api/ apps/workbench/src/state/ apps/workbench/src/hooks/
```

---

## Task 3: Adapt CandlestickChart to Catalyst API

**Files:**
- Modify: `apps/workbench/src/components/chart/CandlestickChart.tsx`

The DemoUI CandlestickChart uses `axios.get('/api/stocks/${symbol}/ohlc')`. Catalyst's backend serves OHLCV at `GET /api/ohlcv/{ticker}` with response shape `{ symbol, candles: OhlcvCandle[], count }`. The candle fields are the same (`date`, `open`, `high`, `low`, `close`, `volume`) but wrapped in a `candles` array.

- [ ] **Step 1: Replace axios import with fetch-based getOhlcv**

Remove:
```typescript
import axios from 'axios';
```

Add:
```typescript
import { getOhlcv } from '../../api/client';
import type { OhlcvCandle } from '../../api/types';
```

- [ ] **Step 2: Replace the data-fetch useEffect**

Find the `axios.get(\`/api/stocks/${symbol}/ohlc\`)` call in the useEffect. Replace with:

```typescript
getOhlcv(symbol)
  .then((res) => {
    const rows: OHLCRow[] = res.candles
      .filter((c: OhlcvCandle) => c.open != null && c.close != null)
      .map((c: OhlcvCandle) => ({
        date: c.date,
        open: c.open!,
        high: c.high!,
        low: c.low!,
        close: c.close!,
        volume: c.volume ?? 0,
      }));
    // ... continue with existing D3 rendering using `rows`
  })
  .catch(() => {
    // Use mock fallback data (keep existing fallback)
  });
```

- [ ] **Step 3: Verify chart renders with mock data fallback**

```bash
cd apps/workbench && npm run dev
```

Navigate to `http://localhost:5173`. Chart should render with fallback data even if backend is not running.

- [ ] **Step 4: Stage**

```bash
git add apps/workbench/src/components/chart/CandlestickChart.tsx
```

---

## Task 4: Adapt StockSelector to Catalyst API

**Files:**
- Modify: `apps/workbench/src/components/context/StockSelector.tsx`

No API change needed in StockSelector itself — it receives `activeTickers` as props. The parent (App.tsx) is responsible for fetching tickers. The StockSelector component is purely presentational with its group-based dropdown. Keep it verbatim.

However, move the file to the correct path:

- [ ] **Step 1: Verify StockSelector has no axios imports**

StockSelector.tsx has no direct API calls — it only uses props. No changes needed.

- [ ] **Step 2: Stage**

```bash
git add apps/workbench/src/components/context/StockSelector.tsx
```

---

## Task 5: Port Auth and Runtime Components

**Files:**
- Create: `apps/workbench/src/components/auth/PasswordGate.tsx` (copy verbatim)
- Create: `apps/workbench/src/components/runtime/RuntimeConsole.tsx` (copy verbatim)
- Create: `apps/workbench/src/components/runtime/NodeTimeline.tsx` (copy verbatim)
- Create: `apps/workbench/src/components/runtime/NodeCard.tsx` (copy verbatim)
- Create: `apps/workbench/src/components/artifacts/ArtifactTabs.tsx` (copy verbatim)
- Create: `apps/workbench/src/components/artifacts/RawResponseViewer.tsx` (copy verbatim)
- Create: `apps/workbench/src/components/artifacts/EvidencePreview.tsx` (copy verbatim)
- Create: `apps/workbench/src/components/attribution/AttributionSummary.tsx` (copy verbatim)
- Create: `apps/workbench/src/components/attribution/SelectedDayContext.tsx` (copy verbatim)

All these files are self-contained, depend only on `../../api/types`, `../../api/client`, and `../../state/workbench-state` — all of which were set up in Task 2. Copy them verbatim from the current workbench worktree.

- [ ] **Step 1: Copy all auth, runtime, artifacts, attribution components**

```bash
# Auth
cp .../components/auth/PasswordGate.tsx apps/workbench/src/components/auth/

# Runtime
cp .../components/runtime/RuntimeConsole.tsx apps/workbench/src/components/runtime/
cp .../components/runtime/NodeTimeline.tsx apps/workbench/src/components/runtime/
cp .../components/runtime/NodeCard.tsx apps/workbench/src/components/runtime/

# Artifacts
cp .../components/artifacts/ArtifactTabs.tsx apps/workbench/src/components/artifacts/
cp .../components/artifacts/RawResponseViewer.tsx apps/workbench/src/components/artifacts/
cp .../components/artifacts/EvidencePreview.tsx apps/workbench/src/components/artifacts/

# Attribution
cp .../components/attribution/AttributionSummary.tsx apps/workbench/src/components/attribution/
cp .../components/attribution/SelectedDayContext.tsx apps/workbench/src/components/attribution/
```

- [ ] **Step 2: Verify TypeScript compilation of all ported components**

```bash
cd apps/workbench && npx tsc --noEmit --pretty 2>&1 | head -30
```

Expected: Zero errors.

- [ ] **Step 3: Stage**

```bash
git add apps/workbench/src/components/auth/ apps/workbench/src/components/runtime/ apps/workbench/src/components/artifacts/ apps/workbench/src/components/attribution/
```

---

## Task 6: Create ModelSelector Component

**Files:**
- Create: `apps/workbench/src/components/context/ModelSelector.tsx`

This component calls `GET /api/models` on mount to fetch available models, renders a `<select>` dropdown styled to match the dark theme. It surfaces the `model_id` to the parent for use in `createLiveRun`.

- [ ] **Step 1: Write ModelSelector component**

```typescript
import { useEffect, useState } from 'react';

import { getModels } from '../../api/client';
import type { ModelOption } from '../../api/types';

interface ModelSelectorProps {
  selectedModel: string;
  onModelChange: (modelId: string) => void;
}

export function ModelSelector({ selectedModel, onModelChange }: ModelSelectorProps) {
  const [models, setModels] = useState<ModelOption[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let isActive = true;

    getModels()
      .then((res) => {
        if (!isActive) return;
        setModels(res.models);
        if (!selectedModel && res.default_model_id) {
          onModelChange(res.default_model_id);
        }
      })
      .catch(() => {
        if (!isActive) return;
        setModels([]);
      })
      .finally(() => {
        if (isActive) setLoading(false);
      });

    return () => { isActive = false; };
  }, []);

  if (loading) {
    return <span className="model-selector-loading">Loading models...</span>;
  }

  if (models.length === 0) {
    return <span className="model-selector-empty">No models available</span>;
  }

  return (
    <select
      className="model-selector"
      value={selectedModel}
      onChange={(e) => onModelChange(e.target.value)}
      aria-label="Select LLM model"
    >
      {models.map((m) => (
        <option key={m.model_id} value={m.model_id}>
          {m.label}{m.is_default ? ' (default)' : ''}
        </option>
      ))}
    </select>
  );
}
```

- [ ] **Step 2: Stage**

```bash
git add apps/workbench/src/components/context/ModelSelector.tsx
```

---

## Task 7: Create PipelinePanel Component

**Files:**
- Create: `apps/workbench/src/components/runtime/PipelinePanel.tsx`

A tabbed container that switches between RuntimeConsole view and ArtifactTabs view. This fills the bottom section of the layout to provide "pipeline transparency" — the core value proposition.

- [ ] **Step 1: Write PipelinePanel component**

```typescript
import { useState } from 'react';

import type { ArtifactResponse, RunEventResponse, RunSummaryResponse } from '../../api/types';
import { RuntimeConsole } from './RuntimeConsole';
import { ArtifactTabs } from '../artifacts/ArtifactTabs';
import { EvidencePreview } from '../artifacts/EvidencePreview';

interface PipelinePanelProps {
  summary: RunSummaryResponse | null;
  events: RunEventResponse[];
  runId: string | null;
  artifacts: ArtifactResponse[];
}

type PipelineTab = 'console' | 'artifacts' | 'evidence';

export function PipelinePanel({ summary, events, runId, artifacts }: PipelinePanelProps) {
  const [activeTab, setActiveTab] = useState<PipelineTab>('console');

  if (!summary || !runId) {
    return (
      <section className="pipeline-panel" aria-label="pipeline transparency">
        <div className="pipeline-empty">
          <p>Run an attribution to see pipeline details here.</p>
        </div>
      </section>
    );
  }

  return (
    <section className="pipeline-panel" aria-label="pipeline transparency">
      <div className="pipeline-tabs" role="tablist">
        <button
          role="tab"
          className={`pipeline-tab ${activeTab === 'console' ? 'active' : ''}`}
          aria-selected={activeTab === 'console'}
          onClick={() => setActiveTab('console')}
        >
          Runtime Console
        </button>
        <button
          role="tab"
          className={`pipeline-tab ${activeTab === 'artifacts' ? 'active' : ''}`}
          aria-selected={activeTab === 'artifacts'}
          onClick={() => setActiveTab('artifacts')}
        >
          Artifacts
        </button>
        <button
          role="tab"
          className={`pipeline-tab ${activeTab === 'evidence' ? 'active' : ''}`}
          aria-selected={activeTab === 'evidence'}
          onClick={() => setActiveTab('evidence')}
        >
          Evidence
        </button>
      </div>

      <div className="pipeline-content" role="tabpanel">
        {activeTab === 'console' && (
          <RuntimeConsole summary={summary} events={events} />
        )}
        {activeTab === 'artifacts' && (
          <ArtifactTabs runId={runId} />
        )}
        {activeTab === 'evidence' && (
          <EvidencePreview artifacts={artifacts} />
        )}
      </div>
    </section>
  );
}
```

- [ ] **Step 2: Stage**

```bash
git add apps/workbench/src/components/runtime/PipelinePanel.tsx
```

---

## Task 8: Rewrite App.tsx — Full Layout Integration

**Files:**
- Modify: `apps/workbench/src/App.tsx`

This is the core integration task. Rewrite the DemoUI App.tsx to:
1. Remove all DemoUI-specific imports (NewsPanel, NewsCategoryPanel, AttributionSidePanel, RangeQueryPopup, axios)
2. Add Catalyst imports (PasswordGate, ModelSelector, CandlestickChart, StockSelector, RuntimeConsole, ArtifactTabs, AttributionSummary, SelectedDayContext, PipelinePanel)
3. Wire up state: tickers from `getTickers()`, OHLCV from `getOhlcv()`, live run lifecycle
4. Implement the 4-row grid layout (header → control bar → chart+attribution → pipeline)
5. Wrap everything in `<PasswordGate>`

- [ ] **Step 1: Write the complete App.tsx**

```typescript
import { useCallback, useEffect, useRef, useState } from 'react';

import { PasswordGate } from './components/auth/PasswordGate';
import CandlestickChart from './components/chart/CandlestickChart';
import { StockSelector } from './components/context/StockSelector';
import { ModelSelector } from './components/context/ModelSelector';
import { SelectedDayContext } from './components/attribution/SelectedDayContext';
import { AttributionSummary } from './components/attribution/AttributionSummary';
import { PipelinePanel } from './components/runtime/PipelinePanel';
import { useLiveRunPolling } from './hooks/useLiveRunPolling';
import {
  createLiveRun,
  getTickers,
  getLiveRunArtifacts,
  retryLiveRun,
  getRuntimeHealth,
} from './api/client';
import type {
  ArtifactResponse,
  RunEventResponse,
  RunSummaryResponse,
  HealthStatus,
} from './api/types';
import {
  createInitialState,
  reduceRunSummary,
  reduceLastEventSeq,
  isTerminalStatus,
} from './state/workbench-state';
import type { WorkbenchState } from './state/workbench-state';
import './App.css';

const FALLBACK_TICKERS = ['AAPL', 'NVDA', 'MSFT', 'TSLA'];

function App() {
  // Market context
  const [activeTickers, setActiveTickers] = useState<string[]>([]);
  const [selectedSymbol, setSelectedSymbol] = useState('');
  const [selectedDate, setSelectedDate] = useState<string | null>(null);
  const [hoveredOhlc, setHoveredOhlc] = useState<{
    date: string; open: number; high: number; low: number; close: number; change: number;
  } | null>(null);

  // Model
  const [selectedModel, setSelectedModel] = useState('');

  // Run state
  const [wbState, setWbState] = useState<WorkbenchState>(createInitialState);
  const [events, setEvents] = useState<RunEventResponse[]>([]);
  const [summary, setSummary] = useState<RunSummaryResponse | null>(null);
  const [artifacts, setArtifacts] = useState<ArtifactResponse[]>([]);
  const [isCreatingRun, setIsCreatingRun] = useState(false);
  const [createRunError, setCreateRunError] = useState<string | null>(null);
  const [isRetrying, setIsRetrying] = useState(false);

  // Health
  const [healthStatus, setHealthStatus] = useState<HealthStatus | null>(null);

  // Bootstrap: fetch tickers
  useEffect(() => {
    getTickers()
      .then((res) => {
        if (res.symbols.length > 0) {
          setActiveTickers(res.symbols);
          setSelectedSymbol(res.symbols[0]);
        } else {
          setActiveTickers(FALLBACK_TICKERS);
          setSelectedSymbol(FALLBACK_TICKERS[0]);
        }
      })
      .catch(() => {
        setActiveTickers(FALLBACK_TICKERS);
        setSelectedSymbol(FALLBACK_TICKERS[0]);
      });

    getRuntimeHealth()
      .then((h) => setHealthStatus(h.status))
      .catch(() => setHealthStatus('failed'));
  }, []);

  // Fetch artifacts when run reaches terminal status
  useEffect(() => {
    if (!wbState.activeRunId || !isTerminalStatus(wbState.runStatus)) return;
    getLiveRunArtifacts(wbState.activeRunId)
      .then(setArtifacts)
      .catch(() => setArtifacts([]));
  }, [wbState.activeRunId, wbState.runStatus]);

  // Polling
  useLiveRunPolling({
    runId: wbState.activeRunId,
    status: wbState.runStatus,
    lastEventSeq: wbState.lastEventSeq,
    onSummary: useCallback((s: RunSummaryResponse) => {
      setSummary(s);
      setWbState((prev) => reduceRunSummary(prev, s));
    }, []),
    onEvents: useCallback((newEvents: RunEventResponse[]) => {
      setEvents((prev) => [...prev, ...newEvents]);
    }, []),
    onLastEventSeqChange: useCallback((seq: number) => {
      setWbState((prev) => reduceLastEventSeq(prev, seq));
    }, []),
  });

  function handleSelectSymbol(symbol: string) {
    setSelectedSymbol(symbol);
    setSelectedDate(null);
    setHoveredOhlc(null);
  }

  const handleDayClick = useCallback((date: string) => {
    setSelectedDate(date);
  }, []);

  const handleHover = useCallback(
    (_date: string | null, ohlc?: { date: string; open: number; high: number; low: number; close: number; change: number }) => {
      setHoveredOhlc(ohlc ?? null);
    }, []
  );

  async function handleRunAttribution() {
    if (!selectedSymbol || !selectedDate) return;
    setIsCreatingRun(true);
    setCreateRunError(null);
    setEvents([]);
    setArtifacts([]);

    try {
      const res = await createLiveRun({
        ticker: selectedSymbol,
        trade_date: selectedDate,
        model_id: selectedModel || null,
      });
      setSummary({
        run_id: res.run_id,
        status: res.status,
        ticker: selectedSymbol,
        trade_date: selectedDate,
        model_id: res.model_id,
      });
      setWbState((prev) => reduceRunSummary(prev, {
        run_id: res.run_id,
        status: res.status,
        ticker: selectedSymbol,
        trade_date: selectedDate,
        last_completed_node: null,
        predicted_next_node: null,
      }));
    } catch (err) {
      setCreateRunError(err instanceof Error ? err.message : 'Failed to create run');
    } finally {
      setIsCreatingRun(false);
    }
  }

  async function handleRetry() {
    if (!wbState.activeRunId) return;
    setIsRetrying(true);
    try {
      const res = await retryLiveRun(wbState.activeRunId, {
        model_id: selectedModel || null,
      });
      if (res.ok && res.run_id) {
        setEvents([]);
        setArtifacts([]);
        setWbState((prev) => reduceRunSummary(prev, {
          run_id: res.run_id!,
          status: res.status ?? 'QUEUED',
          ticker: selectedSymbol,
          trade_date: selectedDate,
          last_completed_node: null,
          predicted_next_node: null,
        }));
      }
    } catch {
      // Retry failed silently
    } finally {
      setIsRetrying(false);
    }
  }

  return (
    <PasswordGate>
      <div className="app">
        <header className="app-header">
          <div className="header-left">
            <h1>Catalyst</h1>
            <span className="header-subtitle">Attribution Workbench</span>
          </div>
          <div className="header-right">
            <ModelSelector selectedModel={selectedModel} onModelChange={setSelectedModel} />
            <span className={`health-dot health-${healthStatus ?? 'unknown'}`}
                  title={`Runtime: ${healthStatus ?? 'checking...'}`} />
          </div>
        </header>

        <div className="control-bar">
          <StockSelector
            activeTickers={activeTickers}
            selectedSymbol={selectedSymbol}
            onSelect={handleSelectSymbol}
          />
          <SelectedDayContext
            ticker={selectedSymbol}
            tradeDate={selectedDate ?? ''}
            query=""
            runStatus={wbState.runStatus}
            isCreatingRun={isCreatingRun}
            createRunError={createRunError}
            onRunAttribution={handleRunAttribution}
          />
          {hoveredOhlc && (
            <div className="header-ohlc">
              <span className="ohlc-date">{hoveredOhlc.date}</span>
              <span className="ohlc-label">O</span>
              <span className="ohlc-val">${hoveredOhlc.open.toFixed(2)}</span>
              <span className="ohlc-label">H</span>
              <span className="ohlc-val">${hoveredOhlc.high.toFixed(2)}</span>
              <span className="ohlc-label">L</span>
              <span className="ohlc-val">${hoveredOhlc.low.toFixed(2)}</span>
              <span className="ohlc-label">C</span>
              <span className="ohlc-val">${hoveredOhlc.close.toFixed(2)}</span>
              <span className={`ohlc-change ${hoveredOhlc.change >= 0 ? 'up' : 'down'}`}>
                {hoveredOhlc.change >= 0 ? '+' : ''}{hoveredOhlc.change.toFixed(2)}%
              </span>
            </div>
          )}
        </div>

        <main className="app-main">
          <div className="chart-area">
            {selectedSymbol ? (
              <CandlestickChart
                symbol={selectedSymbol}
                onHover={handleHover}
                onDayClick={handleDayClick}
              />
            ) : (
              <div className="chart-placeholder">Select a ticker to view the chart</div>
            )}
          </div>

          <div className="attribution-area">
            {summary && isTerminalStatus(summary.status) ? (
              <AttributionSummary
                summary={summary}
                onRetry={handleRetry}
                isRetrying={isRetrying}
              />
            ) : (
              <div className="attribution-placeholder">
                <h3>Attribution Results</h3>
                <p>Click a candle on the chart to select a trade date, then click "Run Attribution" to analyze what moved the stock.</p>
              </div>
            )}
          </div>

          <div className="pipeline-area">
            <PipelinePanel
              summary={summary}
              events={events}
              runId={wbState.activeRunId}
              artifacts={artifacts}
            />
          </div>
        </main>
      </div>
    </PasswordGate>
  );
}

export default App;
```

- [ ] **Step 2: Update main.tsx to remove any DemoUI-specific routing**

```typescript
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import './App.css'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
```

- [ ] **Step 3: Verify TypeScript compilation**

```bash
cd apps/workbench && npx tsc --noEmit --pretty
```

Expected: Zero errors.

- [ ] **Step 4: Stage**

```bash
git add apps/workbench/src/App.tsx apps/workbench/src/main.tsx
```

---

## Task 9: Extend App.css — New Panel Styles

**Files:**
- Modify: `apps/workbench/src/App.css`

Keep the existing DemoUI dark theme CSS (header, OHLC, chart styles). Remove CSS rules for deleted panels (NewsPanel, NewsCategoryPanel, prediction panels). Add CSS for:
- `.control-bar` — horizontal bar below header
- `.app-main` new grid layout: `grid-template-columns: 1fr 360px`, `grid-template-rows: 48vh 1fr`
- `.attribution-area` — right sidebar
- `.pipeline-area` — full-width bottom section (`grid-column: 1 / -1`)
- `.pipeline-panel`, `.pipeline-tabs`, `.pipeline-tab` — tabbed panel
- `.password-gate`, `.password-gate-form` — login gate overlay
- `.model-selector` — styled select dropdown
- `.health-dot` — colored status indicator
- `.status-chip`, `.status-chip-*` — status badges
- `.node-card`, `.node-card-header`, `.node-card-details` — node cards
- `.artifact-tabs`, `.artifact-tab`, `.artifact-panel` — artifact viewer
- `.raw-viewer`, `.raw-viewer-code` — JSON viewer
- `.evidence-preview`, `.evidence-item` — evidence list
- `.attribution-summary`, `.summary-header`, `.summary-headline` — attribution result
- `.selected-day-context`, `.selected-day-header`, `.selected-day-grid` — day context bar
- `.failure-banner` — error display
- `.inline-feedback` — form validation messages

- [ ] **Step 1: Remove DemoUI-specific CSS rules**

Remove all rules for: `.news-area`, `.news-panel`, `.news-list`, `.news-item`, `.news-category`, `.pred-panel`, `.pred-header`, `.pred-details`, `.company-info-card`, `.company-info-row`, `.range-popup`, `.prediction-area`.

- [ ] **Step 2: Add new layout grid CSS**

```css
/* ===== Main Layout ===== */
.app-main {
  flex: 1;
  display: grid;
  grid-template-columns: 1fr 360px;
  grid-template-rows: 48vh 1fr;
  gap: 1px;
  background: #1a1d29;
  overflow: hidden;
}

.chart-area {
  grid-column: 1;
  grid-row: 1;
  background: #0f1117;
  position: relative;
  overflow: hidden;
}

.attribution-area {
  grid-column: 2;
  grid-row: 1;
  background: #0f1117;
  border-left: 1px solid #2a2d3a;
  padding: 12px;
  overflow-y: auto;
}

.pipeline-area {
  grid-column: 1 / -1;
  grid-row: 2;
  background: #0f1117;
  border-top: 1px solid #2a2d3a;
  overflow: hidden;
}
```

- [ ] **Step 3: Add control bar CSS**

```css
/* ===== Control Bar ===== */
.control-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 6px 12px;
  background: #161922;
  border-bottom: 1px solid #2a2d3a;
  flex-shrink: 0;
}
```

- [ ] **Step 4: Add PasswordGate CSS**

```css
/* ===== Password Gate ===== */
.password-gate {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100vh;
  background: #0f1117;
}

.password-gate-form {
  background: #1a1d29;
  border: 1px solid #2a2d3a;
  border-radius: 12px;
  padding: 40px;
  width: 360px;
  text-align: center;
}

.password-gate-title {
  font-size: 24px;
  font-weight: 700;
  color: #fff;
  margin-bottom: 4px;
}

.password-gate-subtitle {
  font-size: 14px;
  color: #888;
  margin-bottom: 24px;
}

.password-gate-label {
  display: block;
  font-size: 13px;
  color: #aaa;
  margin-bottom: 8px;
  text-align: left;
}

.password-gate-input {
  width: 100%;
  padding: 10px 12px;
  background: #0f1117;
  border: 1px solid #2a2d3a;
  border-radius: 8px;
  color: #e0e0e0;
  font-size: 14px;
  outline: none;
}

.password-gate-input:focus {
  border-color: #4a9eff;
}

.password-gate-error {
  color: #ef5350;
  font-size: 13px;
  margin-top: 8px;
}

.password-gate-submit {
  width: 100%;
  margin-top: 16px;
  padding: 10px;
  background: #4a9eff;
  color: #fff;
  border: none;
  border-radius: 8px;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
}

.password-gate-submit:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
```

- [ ] **Step 5: Add ModelSelector, health dot, status chip, panel styles**

```css
/* ===== Model Selector ===== */
.model-selector {
  background: #1a1d29;
  border: 1px solid #2a2d3a;
  color: #e0e0e0;
  padding: 5px 8px;
  border-radius: 6px;
  font-size: 12px;
  outline: none;
}

/* ===== Health Dot ===== */
.health-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  display: inline-block;
}
.health-ready { background: #4caf50; }
.health-degraded { background: #ff9800; }
.health-failed { background: #ef5350; }
.health-unknown { background: #666; }

/* ===== Status Chips ===== */
.status-chip {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
.status-chip-queued { background: #333a50; color: #8899bb; }
.status-chip-running { background: #1a3a5c; color: #4a9eff; }
.status-chip-succeeded { background: #1a3c2a; color: #4caf50; }
.status-chip-partial { background: #3a3520; color: #ff9800; }
.status-chip-insufficient { background: #3a2a20; color: #ff7043; }
.status-chip-failed_system { background: #3a1a1a; color: #ef5350; }
.status-chip-failed_request { background: #3a1a1a; color: #ef5350; }

/* ===== Panel Base ===== */
.panel {
  padding: 12px;
}

.panel h2 {
  font-size: 14px;
  font-weight: 600;
  color: #e0e0e0;
  margin-bottom: 10px;
}
```

- [ ] **Step 6: Add RuntimeConsole, NodeCard, NodeTimeline CSS**

```css
/* ===== Runtime Console ===== */
.runtime-console h2 {
  font-size: 13px;
  font-weight: 600;
  color: #aaa;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 8px;
}

.console-progress-fields {
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}

.console-field dt {
  font-size: 11px;
  color: #666;
  text-transform: uppercase;
}

.console-field dd {
  font-size: 13px;
  color: #ccc;
  font-family: 'SF Mono', 'Fira Code', monospace;
}

.console-empty {
  color: #555;
  font-size: 13px;
  font-style: italic;
}

/* ===== Failure Banner ===== */
.failure-banner {
  background: #2a1515;
  border: 1px solid #4a2020;
  border-radius: 6px;
  padding: 10px 12px;
  margin-bottom: 10px;
}

.failure-headline {
  color: #ef5350;
  font-size: 13px;
  font-weight: 600;
}

.failure-detail {
  color: #cc7777;
  font-size: 12px;
  margin-top: 4px;
}

/* ===== Node Cards ===== */
.node-timeline {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.node-card {
  background: #161922;
  border: 1px solid #2a2d3a;
  border-radius: 6px;
  overflow: hidden;
}

.node-card-header {
  display: flex;
  align-items: center;
  width: 100%;
  padding: 8px 10px;
  background: transparent;
  border: none;
  color: #e0e0e0;
  cursor: pointer;
  font-size: 13px;
  gap: 8px;
}

.node-card-name {
  font-weight: 600;
  flex: 1;
  text-align: left;
}

.node-card-status {
  font-size: 11px;
  color: #888;
  text-transform: uppercase;
}

.node-card-toggle {
  color: #555;
  font-size: 10px;
}

.node-card-details {
  padding: 8px 10px;
  border-top: 1px solid #2a2d3a;
}

.node-card-metrics {
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
}

.node-metric dt {
  font-size: 10px;
  color: #666;
  text-transform: uppercase;
}

.node-metric dd {
  font-size: 13px;
  color: #ccc;
  font-family: 'SF Mono', 'Fira Code', monospace;
}

.node-card-time {
  font-size: 11px;
  color: #555;
  margin-top: 6px;
}
```

- [ ] **Step 7: Add ArtifactTabs, RawResponseViewer, EvidencePreview CSS**

```css
/* ===== Artifact Tabs ===== */
.artifact-tabs [role="tablist"] {
  display: flex;
  gap: 2px;
  margin-bottom: 8px;
  overflow-x: auto;
}

.artifact-tab {
  background: #1a1d29;
  border: 1px solid #2a2d3a;
  color: #888;
  padding: 4px 10px;
  border-radius: 4px;
  font-size: 11px;
  cursor: pointer;
  white-space: nowrap;
}

.artifact-tab-active {
  background: #252a3a;
  color: #e0e0e0;
  border-color: #39425e;
}

.artifact-entry {
  margin-bottom: 8px;
}

.artifact-meta {
  display: flex;
  gap: 12px;
  font-size: 11px;
  color: #666;
  margin-bottom: 4px;
}

.artifact-error {
  color: #ef5350;
}

/* ===== Raw Viewer ===== */
.raw-viewer {
  background: #0d0f14;
  border: 1px solid #2a2d3a;
  border-radius: 6px;
  overflow: hidden;
}

.raw-viewer-toolbar {
  display: flex;
  gap: 6px;
  padding: 4px 8px;
  background: #161922;
  border-bottom: 1px solid #2a2d3a;
}

.raw-viewer-action {
  background: transparent;
  border: 1px solid #2a2d3a;
  color: #888;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 11px;
  cursor: pointer;
}

.raw-viewer-code {
  padding: 8px;
  font-size: 12px;
  color: #aaa;
  font-family: 'SF Mono', 'Fira Code', monospace;
  overflow-x: auto;
  max-height: 300px;
  overflow-y: auto;
}

.raw-viewer-wrap {
  white-space: pre-wrap;
  word-break: break-all;
}

/* ===== Evidence Preview ===== */
.evidence-preview h3 {
  font-size: 13px;
  color: #aaa;
  margin-bottom: 8px;
}

.evidence-list {
  list-style: none;
}

.evidence-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 8px 0;
  border-bottom: 1px solid #1a1d29;
}

.evidence-title {
  font-size: 13px;
  color: #e0e0e0;
  font-weight: 500;
}

.evidence-meta {
  font-size: 11px;
  color: #666;
}

.evidence-score {
  color: #4a9eff;
}

.evidence-badge {
  font-size: 10px;
  color: #555;
  text-transform: uppercase;
}

.evidence-muted {
  font-style: italic;
  color: #444;
}

.evidence-empty {
  color: #555;
  font-style: italic;
}
```

- [ ] **Step 8: Add Pipeline Panel, Attribution Summary, SelectedDayContext CSS**

```css
/* ===== Pipeline Panel ===== */
.pipeline-panel {
  height: 100%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.pipeline-tabs {
  display: flex;
  gap: 2px;
  padding: 6px 12px;
  background: #161922;
  border-bottom: 1px solid #2a2d3a;
  flex-shrink: 0;
}

.pipeline-tab {
  background: transparent;
  border: 1px solid transparent;
  color: #888;
  padding: 6px 12px;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
}

.pipeline-tab:hover {
  color: #ccc;
  background: rgba(255, 255, 255, 0.04);
}

.pipeline-tab.active {
  color: #e0e0e0;
  background: #252a3a;
  border-color: #39425e;
}

.pipeline-content {
  flex: 1;
  overflow-y: auto;
  padding: 12px;
}

.pipeline-empty {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: #555;
  font-size: 14px;
}

/* ===== Attribution Summary ===== */
.attribution-summary h2 {
  font-size: 13px;
  color: #aaa;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 8px;
}

.summary-header {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}

.summary-run-id {
  font-size: 11px;
  color: #555;
  font-family: 'SF Mono', 'Fira Code', monospace;
}

.summary-headline {
  font-size: 14px;
  color: #ccc;
  margin-bottom: 6px;
}

.summary-detail {
  font-size: 12px;
  color: #888;
  margin-bottom: 4px;
}

.summary-actions {
  margin-top: 10px;
}

.secondary-action {
  background: #252a3a;
  border: 1px solid #39425e;
  color: #ccc;
  padding: 6px 14px;
  border-radius: 6px;
  font-size: 12px;
  cursor: pointer;
}

.secondary-action:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

/* ===== Selected Day Context ===== */
.selected-day-context {
  padding: 0;
}

.selected-day-header {
  display: flex;
  align-items: center;
  gap: 12px;
}

.selected-day-header h2 {
  font-size: 12px;
  color: #888;
  margin: 0;
}

.selected-day-header p {
  font-size: 13px;
  color: #ccc;
  font-family: 'SF Mono', 'Fira Code', monospace;
}

.selected-day-grid {
  display: none; /* Compact mode in control bar */
}

.primary-action {
  background: #4a9eff;
  border: none;
  color: #fff;
  padding: 6px 14px;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  white-space: nowrap;
}

.primary-action:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.inline-feedback {
  font-size: 11px;
  color: #888;
}

.inline-feedback-error {
  color: #ef5350;
}

/* ===== Attribution Placeholder ===== */
.attribution-placeholder {
  padding: 20px 12px;
  text-align: center;
}

.attribution-placeholder h3 {
  font-size: 14px;
  color: #888;
  margin-bottom: 8px;
}

.attribution-placeholder p {
  font-size: 13px;
  color: #555;
  line-height: 1.5;
}
```

- [ ] **Step 9: Verify dev server renders correctly**

```bash
cd apps/workbench && npm run dev
```

Navigate to `http://localhost:5173`. Verify:
1. Password gate appears first
2. After entering `catalyst2026`, main dashboard loads
3. Header shows "Catalyst" with model selector
4. Chart area renders (with fallback data if backend off)
5. Right sidebar shows "Attribution Results" placeholder
6. Bottom pipeline panel shows "Run an attribution to see pipeline details"

- [ ] **Step 10: Stage**

```bash
git add apps/workbench/src/App.css
```

---

## Task 10: Fix StockSelector Export Compatibility

**Files:**
- Modify: `apps/workbench/src/components/context/StockSelector.tsx`

DemoUI's StockSelector uses `export default function StockSelector`. The new App.tsx imports it as a named import `{ StockSelector }`. Either make the import default or add a named export. Easiest: keep the default export and update the import in App.tsx to use default.

- [ ] **Step 1: Verify the export style and update App.tsx import accordingly**

If StockSelector uses `export default`, change App.tsx:
```typescript
// Change from:
import { StockSelector } from './components/context/StockSelector';
// To:
import StockSelector from './components/context/StockSelector';
```

Similarly for CandlestickChart (which is already a default export).

- [ ] **Step 2: Verify compilation**

```bash
cd apps/workbench && npx tsc --noEmit --pretty
```

- [ ] **Step 3: Stage**

```bash
git add apps/workbench/src/App.tsx
```

---

## Task 11: Remove DemoUI's Unused RangeQueryPopup Dependency

**Files:**
- Check and remove any remaining import of `RangeQueryPopup` in App.tsx
- Verify no DemoUI components are imported that we didn't copy

- [ ] **Step 1: Grep for dead imports**

```bash
grep -rn "import.*from.*NewsPanel\|NewsCategoryPanel\|AttributionSidePanel\|RangeQueryPopup\|axios" apps/workbench/src/
```

Expected: Zero results. If any found, remove the imports.

- [ ] **Step 2: Check CandlestickChart for axios dependency**

If `CandlestickChart.tsx` still imports `axios`, replace with the Catalyst fetch client as described in Task 3.

- [ ] **Step 3: Stage any fixes**

```bash
git add apps/workbench/src/
```

---

## Task 12: End-to-End Smoke Test

**Files:** None (testing only)

- [ ] **Step 1: Start backend**

```bash
cd packages/app && uvicorn catalyst_app.main:app --host 0.0.0.0 --port 8000
```

- [ ] **Step 2: Start frontend**

```bash
cd apps/workbench && npm run dev
```

- [ ] **Step 3: Verify full flow**

1. Open `http://localhost:5173` — see password gate
2. Enter `catalyst2026` — see dashboard
3. Ticker selector loads from `/api/tickers`
4. Chart renders candles for default ticker
5. Click a candle → date appears in control bar
6. Model selector dropdown loads from `/api/models`
7. Click "Run Attribution" → status chip shows QUEUED/RUNNING
8. Pipeline panel auto-switches to "Runtime Console" tab
9. Node cards appear as pipeline progresses (miner → critic → judge → finalizer)
10. On terminal status, Attribution Summary shows in right panel
11. ArtifactTabs populates with retrieved_chunks, critic_decision, etc.
12. EvidencePreview shows top evidence items with scores

- [ ] **Step 4: Verify fallback behavior when backend is down**

1. Stop backend
2. Refresh page — chart shows fallback tickers
3. Model selector shows "No models available"
4. Health dot shows red

- [ ] **Step 5: Build production bundle**

```bash
cd apps/workbench && npm run build
```

Expected: Build succeeds, output in `dist/`.

- [ ] **Step 6: Stage any remaining fixes from testing**

```bash
git add apps/workbench/
```

---

## Summary of Decisions

| DemoUI Panel | Catalyst Replacement | Rationale |
|---|---|---|
| NewsPanel | PipelinePanel (Runtime Console tab) | No news API; pipeline transparency is core value |
| NewsCategoryPanel | PipelinePanel (Artifacts tab) | No categories API; intermediate outputs are key |
| AttributionSidePanel (company info) | AttributionSummary + EvidencePreview | No company-info API; replace with run results |
| RangeQueryPopup (model select) | ModelSelector in header + day-click trigger | Model selection is global; trigger is day-click not range-select |
| PredictionPanel area | Attribution result area (right sidebar) | No prediction API; show attribution causes and evidence |

| Kept From DemoUI | Why |
|---|---|
| CandlestickChart.tsx | Proven D3 chart with hover/click, only needs API adapter |
| StockSelector.tsx | Group-based dropdown, receives tickers as props, no API dependency |
| App.css dark theme | Professional finance UI, tested in midterm defense |
| 100vh grid layout | PokieTicker pattern, fills viewport |
| vite proxy config | Required for dev, was missing in old workbench |

| Ported From Workbench | Why |
|---|---|
| api/client.ts + types.ts | Complete typed API client, fetch-based, zero deps on UI |
| workbench-state.ts | Immutable state reducers, terminal status logic |
| useLiveRunPolling.ts | Tested polling hook with ref-based callbacks |
| PasswordGate | Session-based auth, works standalone |
| RuntimeConsole + NodeTimeline + NodeCard | Pipeline transparency UI, fully tested |
| ArtifactTabs + RawResponseViewer + EvidencePreview | Intermediate output display, fully tested |
| AttributionSummary + SelectedDayContext | Attribution result + run trigger, fully tested |
