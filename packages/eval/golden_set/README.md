# Golden Set — Annotation Guidelines

The golden set is the ground-truth reference for evaluating attribution agents. Each entry represents a significant stock price movement and its verified causal factors.

## Frozen P0 Subset

`v1_2_p0_set.jsonl` is the Day-9 frozen P0 subset used for the direct-vs-MCJ evaluation protocol.

- Distribution is fixed at `5 sufficient / 2 partial / 3 should_refuse`.
- `v1_2.jsonl` remains the unfrozen 50-row source file and is not rewritten by the freeze step.
- The refusal bucket uses the minimal P0 row contract: two temporal out-of-corpus cases sourced from `v1_2.jsonl` plus one fresh-authored out-of-corpus entity case (`p0r001`).

## Immutability

After T-13a/T-13b freeze capture, the following are protocol-bound and must not change without re-freezing the eval protocol:

- `packages/eval/golden_set/v1_2_p0_set.jsonl`
- `data/catalyst_eval_frozen.db` SHA-256
- `data/eval_reports/preflight_*.json` / freeze-header artifacts that pin the frozen sample

This is a hard sample-protocol immutability rule for P0, not a preference.

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
| `text` | string | Clear description of the cause with specific, verifiable facts |
| `category` | string | One of: `earnings`, `macro`, `geopolitical`, `sector`, `technical`, `regulatory` |
| `evidence_ids` | list[str] | Optional. Chunk IDs from the evidence corpus (can be omitted for now) |

## Annotation Rules

1. **Verify with multiple sources.** Each cause must be corroborated by at least 2 independent news sources.
2. **Cause clarity.** Keep causes specific and non-overlapping.
3. **Date accuracy.** Ensure causes are consistent with the event trade date.
4. **Category discipline.** Use the most specific applicable category. "Macro" is for economy-wide events (rates, CPI), not sector rotation.
5. **No hindsight.** Causes should reflect what was known at the time, not post-hoc analysis.
6. **2-3 causes per event.** Every event has one primary cause and 1-2 secondary causes.

## AR Tiers (Event Selection Context)

**AR formula:** `AR = R_stock - (alpha + beta * R_market)` where alpha, beta are estimated via OLS on a 60-trading-day window before the event, using SPY as market proxy.

| AR Magnitude | Interpretation | Annotation Hint |
|---|---|---|
| \|AR\| > 5% | Strong idiosyncratic driver | Expect a dominant company/sector catalyst |
| 2% < \|AR\| <= 5% | Mixed idiosyncratic + market | Often multi-causal with broader market context |
| \|AR\| <= 2% | Mostly market-driven | Macro/sector framing is more common |

## Category Definitions

| Category | Scope | Examples |
|----------|-------|---------|
| `earnings` | Company financial results, guidance, analyst day | Q4 miss, guidance cut, revenue beat |
| `macro` | Economy-wide events affecting all sectors | Rate decisions, CPI, Jackson Hole, tariff pause |
| `geopolitical` | Cross-border political/trade events | Tariff shock, sanctions, trade war |
| `sector` | Industry-wide dynamics | AI capex cycle, EV competition, DeepSeek |
| `technical` | Price-action driven, no fundamental catalyst | Short squeeze, insider buy, $4T milestone |
| `regulatory` | Legal/regulatory actions targeting specific firms | DOJ probe, antitrust ruling, export controls |

## Coverage Requirements

A valid golden set must cover:
- At least 4 of 6 categories with >= 2 events each
- Both positive and negative price moves
- At least 5 different tickers
- Mix of high-AR (idiosyncratic) and low-AR (market-driven) events

## Files

- `v1_2.jsonl` — Current midterm golden set file (`g001` through `g050`)
- `annotation_template.md` — Historical annotation prompt and validation notes retained for provenance of `v1_2.jsonl`

## S1 v1.3 Golden Sets (Phase A)

S1 evaluation uses 65 cases across two files:

- `v1_3_answerable.jsonl` — 50 cases expected to produce an attribution
- `v1_3_unanswerable.jsonl` — 15 cases expected to trigger a refusal

These supersede the v1.2 lineage for S1 metric evaluation. The S1 ruler is driven by
`test_s1_rebuild_eval_ruler.py` and `test_s1_three_arm.py`.
