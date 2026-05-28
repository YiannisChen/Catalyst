# T-04 Inspection Note: Conditional Fallback + Cross-Source Dedup

## Config Constants

The new T-04 constants live in [packages/data-core/catalyst_data/config.py](/Users/yiannischen/Desktop/Catalyst/packages/data-core/catalyst_data/config.py):

- `FALLBACK_PRICE_MOVE_THRESHOLD` from `CATALYST_FALLBACK_PRICE_MOVE_THRESHOLD` (default `0.03`)
- `FALLBACK_RETRY_THRESHOLD` from `CATALYST_FALLBACK_RETRY_THRESHOLD` (default `3`)
- `CROSS_SOURCE_PRIORITY` from `CATALYST_CROSS_SOURCE_PRIORITY` (comma-separated; default `polygon_news,fmp_news,finnhub_company_news,gdelt_news`)

## Persistence Boundary

No persistence side table was added in T-04.

- `asset_canonical(...)`: **NO**
- `clean_assets` schema mutation: **NO**

This task ships pure functions only. Canonical persistence remains deferred to T-06 per the plan.

## canonical_url Edge Cases

Observed behavior from local spot checks:

- Bare query flags such as `https://foo.com/?flag#x` normalize to `https://foo.com?flag=` because `parse_qsl(..., keep_blank_values=True)` plus `urlencode(...)` preserves the empty value explicitly.
- Punycode/IDN hosts are preserved as provided, but the host casing is normalized to lowercase.
- A one-segment trailing slash such as `/path/` is collapsed to `/path`; deeper paths are left alone.

## W-11 Status

`W-11` status: **shipped**, not partial.

- URL canonicalization shipped.
- Cross-source hard dedup shipped.
- `select_canonical(...)` shipped with configured source priority and the required tiebreak ladder.
- Cross-source semantic dedup remains deferred to P1 as originally planned; the T-04 fallback path was **not** activated.
