# Time corrections

`time_corrections_v1.csv` (2026-10-22): stored `eligible_at` values found to be wrong in the time-zone review
(docs/data/provider_survey.md on v2/implementation, and the review report of 2026-10-22). Existing stores are never
rewritten; a reader applies this table by URL.

| column | meaning |
|---|---|
| `url` | canonical URL of the stored body |
| `publisher` | `CNBC` or `Benzinga`. Both are company bodies whose time came from Finnhub, which gives these two sources the New York clock labelled UTC (the Benzinga rows were fetched 2026-09-30 to 10-08; 279 of 336 carry exactly Finnhub's time, checked 2026-10-22) |
| `stored_eligible_at` | the value in the store |
| `corrected_at` | CNBC: the stored clock read as America/New_York; Benzinga: Alpaca `created_at`, matched by article id |
| `offset_h` | stored minus corrected, hours (negative = stored too early) |
| `basis` | how the correction was established |
| `stores` | `eval` (the dev/test/T6/new-sample snapshots), `server` (the data server's store), or both |

Whether the table is used to re-evaluate the dev set and T6 is a B-class decision after the evaluation (2026-11-13).
