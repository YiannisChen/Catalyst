# 2026-05-17 h_refusal v2 Results

- RUN_TAG: `r1_h_refusal_guardrail_20260517_080355`
- Mode: `query_embedder_mode=deterministic`, `reranker_mode=off`
- SYSTEM_ERROR: `0`
- INSUFFICIENT: `7`
- pass_rate: `7/8`

| case_id | expected_status | output_status | query_source |
|---|---|---|---|
| h001 | INSUFFICIENT | INSUFFICIENT | case_override |
| h002 | INSUFFICIENT | INSUFFICIENT | case_override |
| h003 | INSUFFICIENT | INSUFFICIENT | case_override |
| h004 | INSUFFICIENT | INSUFFICIENT | case_override |
| h005 | INSUFFICIENT | INSUFFICIENT | case_override |
| h006 | INSUFFICIENT | SUFFICIENT | case_override |
| h007 | INSUFFICIENT | INSUFFICIENT | case_override |
| h008 | INSUFFICIENT | INSUFFICIENT | case_override |

## Status Distribution
- INSUFFICIENT: 7
- SUFFICIENT: 1

## Failure Criteria Notes
- h006: expected `INSUFFICIENT` but got `SUFFICIENT`; requires guardrail/prompt follow-up.

