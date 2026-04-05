# Golden Set — Annotation Guidelines

The golden set is the ground-truth reference for evaluating attribution agents. Each entry represents a significant stock price movement and its verified causal factors.

## Format

JSONL (one JSON object per line) conforming to `catalyst_eval.schema.golden_event.GoldenEvent`.

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Unique ID, e.g. `g001` |
| `ticker` | string | Stock ticker symbol |
| `trade_date` | string | ISO-8601 date (YYYY-MM-DD) of the move |
| `price_move_pct` | float | Percentage move (negative = drop) |
| `causes` | list[Cause] | Ordered list of contributing causes |

### Cause Fields

| Field | Type | Description |
|-------|------|-------------|
| `text` | string | Clear description of the cause |
| `category` | string | One of: `earnings`, `macro`, `geopolitical`, `sector`, `technical`, `regulatory` |
| `weight` | float | Contribution weight [0, 1], all causes should sum to ~1.0 |
| `temporal_anchor` | string | When it became known: `pre-market`, `intraday`, `after-hours` |
| `evidence_ids` | list[str] | Chunk IDs from the evidence corpus (populated during retrieval eval) |

## Annotation Rules

1. **Verify with multiple sources.** Each cause must be corroborated by at least 2 independent news sources.
2. **Weight precision.** Use increments of 0.1. Primary cause >= 0.5, secondary >= 0.2, tertiary >= 0.1.
3. **Temporal accuracy.** Anchor to when the market first reacted, not when the news was published.
4. **Category discipline.** Use the most specific applicable category. "Macro" is for economy-wide events (rates, CPI), not sector rotation.
5. **No hindsight.** Causes should reflect what was known at the time, not post-hoc analysis.

## Versioning

- `v1.jsonl` — Initial 5 events (Jan-Feb 2025), needs human verification
- Target: expand to 15+ events covering all 6 cause categories
