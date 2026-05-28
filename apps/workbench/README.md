# Catalyst Attribution Workbench (P1)

Single-page React/Vite workbench for financial event attribution analysis.

## Quick Start

```bash
cd apps/workbench
npm install
npm run dev
```

## Scripts

| Command | Description |
| --- | --- |
| `npm run dev` | Start dev server |
| `npm run build` | Production build (typecheck + bundle) |
| `npm run typecheck` | TypeScript strict check |
| `npm run test` | Run Vitest in watch mode |
| `npm run test -- --run` | Run all tests once |

## P1 Scope

The workbench consumes 9 backend API endpoints:

- `GET /api/tickers` — available ticker symbols
- `GET /api/range-local` — local OHLCV date range
- `GET /api/ohlcv/{ticker}` — candlestick data
- `POST /api/live-runs` — create attribution run
- `GET /api/live-runs/{run_id}` — run summary/status
- `GET /api/live-runs/{run_id}/events` — node-level events
- `GET /api/live-runs/{run_id}/artifacts` — run artifacts
- `POST /api/live-runs/{run_id}/retry` — retry failed run
- `GET /api/health/runtime` — runtime health check

## Non-Goals (P1)

- No news feed or article filtering (`/api/news*` endpoints)
- No fundamentals, categories, or sentiment endpoints
- No price prediction or trading advice features
- No user authentication or multi-tenancy

## Architecture

Three-zone layout at desktop (1440px+):

- **Left**: K-line chart + selected day context + Run Attribution CTA
- **Middle**: Attribution summary + evidence preview
- **Right**: Runtime console (node timeline, node cards, artifact tabs)

Responsive breakpoints: 1440 / 1024 / 768 / 375.

## Tech Stack

- React 18 + TypeScript + Vite
- TradingView Lightweight Charts (stub in P1)
- Vitest + React Testing Library + MSW
- CSS custom properties (finance dark theme)
