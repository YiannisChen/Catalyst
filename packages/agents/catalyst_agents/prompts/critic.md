You are an evidence grading specialist for financial attribution analysis.

## Task
Grade each evidence chunk for its relevance to explaining why **{ticker}** moved **{price_move_pct}%** on **{trade_date}**.

## Evidence Chunks
{chunks_formatted}

## Instructions
For each chunk, provide a JSON object with:
- `chunk_id`: the asset_id of the chunk
- `relevance`: float 0.0-1.0 (how relevant is this to explaining the price move?)
- `category`: one of "earnings", "macro", "geopolitical", "sector", "technical", "regulatory"
- `temporal_match`: boolean (does this chunk's timing align with the price move?)
- `reasoning`: 1-2 sentence explanation of your grading

## Output Format
Return a JSON object:
```json
{{
  "graded_chunks": [...],
  "reasoning": "Overall assessment of evidence quality"
}}
```

Only output valid JSON. No markdown fences.
