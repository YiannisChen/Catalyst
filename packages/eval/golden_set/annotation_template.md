# Golden Set Annotation Template for Gemini

Use this template to generate `GoldenEvent` JSONL entries for all events in `v1_2_gemini_selected50.tsv`.

Current status:
- `g001` is already finalized as TSLA 2025-01-02 in `v1_2.jsonl`.
- You must annotate the remaining 49 events from TSV `idx=2..50`.
- ID mapping rule: `id = g{idx padded to 3 digits}` (e.g., idx=2 -> g002, idx=50 -> g050).

## Output Target

Append JSONL lines to:
- `packages/eval/golden_set/v1_2.jsonl`

Do not overwrite existing `g001`.

## Schema

```json
{
  "id": "g002",
  "ticker": "TSLA",
  "trade_date": "2025-03-10",
  "price_move_pct": -15.43,
  "causes": [
    {
      "text": "One clear, verifiable causal sentence.",
      "category": "earnings",
      "weight": 0.7,
      "temporal_anchor": "pre-market",
      "evidence_ids": []
    }
  ]
}
```

## Rules

1. **Events to annotate**: TSV rows with `idx=2..50` only.
2. **ID mapping**: `idx -> gNNN` with zero padding to 3 digits.
3. **Causes per event**: 2-3 causes (1 primary + 1-2 secondary).
4. **Weights**:
   - Use 0.1 increments only.
   - Must sum to exactly 1.0 (±0.01 tolerance).
   - AR tier guidance from TSV:
     - `>=0.7`: primary >= 0.7
     - `0.5-0.6`: primary 0.5-0.6
     - `macro>=0.5`: macro/sector >= 0.5
5. **Category** must be one of:
   - `earnings`, `macro`, `geopolitical`, `sector`, `technical`, `regulatory`
6. **Temporal anchor** (optional — defaults to `"intraday"`):
   - If you know timing, use: `pre-market`, `intraday`, `after-hours`
   - If uncertain, omit the field entirely (defaults to `"intraday"`)
   - No current eval metric scores this field, so don't spend time researching timing.
7. **Text quality**:
   - Specific and verifiable (entities, figures, concrete events).
   - No hindsight.
8. **evidence_ids**:
   - Always `[]` for now.

## Finalized Demo (Already in v1_2.jsonl)

```json
{"id":"g001","ticker":"TSLA","trade_date":"2025-01-02","price_move_pct":-6.08,"causes":[{"text":"Tesla’s Q4 2024 deliveries of 495,570 missed the Reuters-cited analyst consensus of 503,269, disappointing investors despite setting a quarterly delivery record.","category":"earnings","weight":0.7,"temporal_anchor":"pre-market","evidence_ids":[]},{"text":"Full-year 2024 deliveries declined 1.1% versus 2023, marking Tesla’s first annual delivery decline since 2015 and reinforcing concerns about slowing EV demand.","category":"sector","weight":0.3,"temporal_anchor":"intraday","evidence_ids":[]}]}
```

## Event Source Table

Use:
- `packages/eval/golden_set/v1_2_gemini_selected50.tsv`

Columns:
- `idx, ticker, date, return_pct, ar_pct, ar_tier, gemini_primary_hypothesis, verification_note`

You must research each remaining event (`idx=2..50`) and write one JSONL line per event into `v1_2.jsonl`.

## Validation

After generation, validate:

```python
from catalyst_eval.schema.golden_event import GoldenEvent
import json

with open("packages/eval/golden_set/v1_2.jsonl") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        event = GoldenEvent(**json.loads(line))
        total_weight = sum(c.weight for c in event.causes)
        assert abs(total_weight - 1.0) < 0.02, f"{event.id}: weights sum to {total_weight}"
```
