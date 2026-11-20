# SEC acceptance-time correction table (`sec_acceptance_v1.csv`)

- **What.** Every 8-K / 8-K/A of the 415 + 276 issuers in the SEC submissions snapshots (server `subs_all/`, the v2.1
  checkout's `data/m9/sec_submissions/`) accepted from 2024-07-01, and every 8-K whose exhibits are held by a body
  store (dev, T6 and new-earnings snapshots on the Mac; the server's daily store and its T4 snapshot): 11,200
  accessions.
- **Columns.** `json_acceptance_utc` is the submissions JSON's `acceptanceDateTime`; `edgar_accepted_et` is the
  filing index page's "Accepted" (New York time), fetched 2026-10-22 18:15–20:23 UTC; `edgar_accepted_utc` is that
  time in UTC and is the right one; `json_minus_edgar_h` is 0, 4 or 5 (empty for the 35 accessions known only
  from a store); `in_stores` names the body stores that hold the filing.
- **Result.** Of 11,165 comparable accessions: 4,122 equal, 4,955 late by exactly 4 h, 2,088 late by exactly 5 h,
  none otherwise. Per issuer, either all filings are late or none.
- **Use.** Stores are not rewritten. Readers (sensitivity analyses of both prospective rounds, read-only impact
  checks, the data contract's read layer) take `edgar_accepted_utc` by accession. New intake already uses the index
  page (`m9/sec.py`, `c2ad27d`).
