# SEC Fixtures — Real Content Captured from Public EDGAR

Source URLs:
- sec_submissions_AAPL.json: https://data.sec.gov/submissions/CIK0000320193.json
- sec_8k_index_headers.htm: https://www.sec.gov/Archives/edgar/data/320193/000032019326000011/0000320193-26-000011-index-headers.htm
- sec_8k_primary_wrapper.htm (COVER): https://www.sec.gov/Archives/edgar/data/320193/000032019326000011/aapl-20260430.htm
- sec_8k_exhibit_99_1.htm (EARNINGS RELEASE): https://www.sec.gov/Archives/edgar/data/320193/000032019326000011/a8-kex991q2202603282026.htm

Accession: 0000320193-26-000011, Filed: 2026-04-30, Item 2.02 (earnings)
Document TYPE EX-99.1 confirmed from index-headers (SEQUENCE 2).
The wrapper is the 8-K cover (~3.5K chars of SEC boilerplate).
The exhibit is the ACTUAL earnings release with financial results.
Content: Public SEC filings, no PII.

## sec_10q_letter_spaced_headings.htm (M8 Task 3b)

Source: https://www.sec.gov/Archives/edgar/data/789019/000095017025061046/msft-20250331.htm
(Microsoft 10-Q, accession 0000950170-25-061046, filed 2025-04-30, period
2025-03-31, 6,995,442 raw bytes; the c02 gold document, retained as original
HTML in the M8-A derivative snapshot).

Not a whole-document capture: a concatenation of **verbatim, unmodified** byte
ranges of that file — six statement/section heading `<p>` elements and three
`<tr>` rows of the income-statement table — inside a minimal `<html><body>`
wrapper. Nothing inside those ranges is edited, so the letter-spacing `<span>`
boundaries that split `INCOME STA|TEMENTS` and `EQ|UITY` are the filer's own
markup, as are the ordinary spaces in `CASH FLOWS |STATEMENTS` and
`BALANCE |SHEETS` that must *not* be joined.

Content: public SEC filing, no PII.

## index_real_aapl_10q_… / index_real_aapl_8k_… (M8 Task 4b-0)

Byte-identical retained EDGAR filing-index pages, copied out of
`data/snapshots/catalyst_m8a_derivative_7a004acc.db` (opened
`mode=ro&immutable=1`) from the `raw_assets` rows written by SEC v2 plan run
`b2-e22e8b8560ab42a092b7e2c7da1980aa`. Nothing is edited; the SHA-256 of each
file equals the `response_sha256` recorded for its fetch.

| file | source URL | accession | fetched | bytes | SHA-256 |
| --- | --- | --- | --- | --- | --- |
| `index_real_aapl_10q_0000320193-26-000013.html` | `https://www.sec.gov/Archives/edgar/data/320193/000032019326000013/0000320193-26-000013-index.html` | 0000320193-26-000013 (AAPL 10-Q, filed 2026-05-01, period 2026-03-28) | 2026-07-30T05:41:19Z | 11,499 | `9bd68013f74890ac27c6b40a665c130531209418ff7106b2eb629e73d702ec3c` |
| `index_real_aapl_8k_0000320193-26-000011.html` | `https://www.sec.gov/Archives/edgar/data/320193/000032019326000011/0000320193-26-000011-index.html` | 0000320193-26-000011 (AAPL 8-K, filed 2026-04-30, Items 2.02/9.01) | 2026-07-30T05:41:20Z | 10,623 | `3a3a1287098fdd7e07e2323e0f4004bb9d55745b6b617a64bac2408ab5567021` |

The 10-Q index is the page on which the `sec_index_v1` one-row type shift was
reproduced (`2026-09-22-m8-task-3-verification-and-task-5-preconditions.md`
§4a): it returned 17 "documents", four of them EDGAR navigation chrome, and
typed `a10-qexhibit311…` as `10-Q`, `a10-qexhibit312…` as `EX-31.1` and
`a10-qexhibit321…` as `EX-31.2`. The 8-K is the index of the same filing as
`sec_8k_index_headers.htm` / `sec_8k_exhibit_99_1.htm` above, so the EX-99.1
typing can be checked against a document already retained here.

Content: public SEC filings, no PII.
