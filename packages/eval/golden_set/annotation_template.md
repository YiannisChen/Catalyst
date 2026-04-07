# Golden Set Annotation Template for Gemini

This file is retained as a historical prompt and validation reference for how `v1_2.jsonl` was assembled during the midterm annotation pass.

Current status:
- `v1_2.jsonl` is the current midterm golden set.
- The original candidate-selection TSV used during annotation is no longer tracked in this repo.
- Do not treat this file as an instruction to regenerate `v1_2.jsonl` on the current branch without a new source table and an explicit review pass.

## Output Target

Append JSONL lines to:
- `packages/eval/golden_set/v1_2.jsonl`

Historical note: during the original annotation flow, `g001` was finalized first and `g002` through `g050` were then added in order.

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
      "category": "earnings"
    }
  ]
}
```

## Rules

1. **ID mapping**: `idx -> gNNN` with zero padding to 3 digits.
2. **Causes per event**: 2-3 causes (1 primary + 1-2 secondary).
3. **Category** must be one of:
   - `earnings`, `macro`, `geopolitical`, `sector`, `technical`, `regulatory`
4. **Text quality**:
   - Specific and verifiable (entities, figures, concrete events).
   - No hindsight.
5. **evidence_ids**:
   - Optional for now. You can omit this field entirely.

## Finalized Demo (Already in v1_2.jsonl)

```json
{"id":"g001","ticker":"TSLA","trade_date":"2025-01-02","price_move_pct":-6.08,"causes":[{"text":"Tesla’s Q4 2024 deliveries of 495,570 missed the Reuters-cited analyst consensus of 503,269, disappointing investors despite setting a quarterly delivery record.","category":"earnings"},{"text":"Full-year 2024 deliveries declined 1.1% versus 2023, marking Tesla’s first annual delivery decline since 2015 and reinforcing concerns about slowing EV demand.","category":"sector"}]}
```

## Historical Source Table

The original annotation pass used an external candidate-selection table with columns such as:
- `idx, ticker, date, return_pct, ar_pct, ar_tier, gemini_primary_hypothesis, verification_note`

That source table is not tracked in the current repo state. If a future revision needs to extend or rebuild `v1_2.jsonl`, create or restore an explicit source table first and document the new provenance in the golden-set README.

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
        assert len(event.causes) >= 1, f"{event.id}: empty causes"
```
