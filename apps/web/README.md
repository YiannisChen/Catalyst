# Catalyst Web

## Run

Start the local API on port 8000, then run `npm run dev` from `apps/web`. To use fixture-backed mocks instead, run `npm run dev:mock`. Choose a fixture scenario with `?scenario=<fixture-file-stem>`; the selected scenario is kept for the browser session. Set `VITE_MOCK_PASSTHROUGH=1` when starting `dev:mock` to pass API routes without a matching fixture through to the local API proxy, while keeping fixture-backed run endpoints mocked.

## Rules

- Design tokens come only from `design/tokens.json` through `npm run gen:tokens`; do not add one-off values or utilities.
- API types come only from `contracts/openapi.json` through `npm run gen:api`. A missing field means the backend contract needs to change.
- Review new dependency requests before adding packages.
- Phase 0 intentionally has no shadcn components; those arrive in phase 1.

## Layout

- `design/`: design rules, source tokens, reference bundle, and boards.
- `scripts/`: token generation.
- `src/api/`: generated API schema, client, queries, and event stream.
- `src/app/`: application shell.
- `src/lib/`: safe browser storage helpers.
- `src/mocks/`: contract fixture index, handlers, and worker setup.
- `src/pages/`: route screens.
- `src/styles/`: generated tokens and app base styles.

## Formatting

Run `npm run format` (Prettier 3.9.0, config in `.prettierrc.json`) and `npm run check:classes` before each commit. The second flags class names with no token behind them (spacing steps above 7, arbitrary `[...]` values), which Tailwind would silently drop.
