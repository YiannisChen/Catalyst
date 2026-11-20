# Frontend API contract

`openapi.json` is generated from `catalyst_app.main.create_app().openapi()`.
After changing an API request or response, regenerate it with:

```bash
python scripts/export_api_contract.py
```

The frontend generates TypeScript types from the committed contract with
[`openapi-typescript`](https://www.npmjs.com/package/openapi-typescript):

```bash
npx openapi-typescript contracts/openapi.json -o src/api/generated.ts
```

Use the JSON files under `fixtures/` as Storybook data and as MSW response
bodies. Each fixture records its request, rendered page state, source and API
response. An MSW handler should return the fixture's `response` for its
recorded request; keeping mocks byte-for-byte compatible with the committed
response model is API contract parity. The two `.jsonl` files are persisted
SSE event sequences and should be replayed as `text/event-stream` frames. See
[`fixtures/INDEX.md`](fixtures/INDEX.md) for state and provenance mapping.
