# Frozen DB v2 Manifest

This note records the provenance anchor for the P1 consumption artifact **`data/catalyst_eval_frozen_v2.db`**.

- Source lineage anchor: **`data/catalyst_eval_frozen.db`** (immutable; not modified)
- P1 consumption artifact: **`data/catalyst_eval_frozen_v2.db`**
- Canonical machine-readable provenance artifact:
  - [frozen_db_v2_manifest_20260505_164525.json](/Users/yiannischen/Desktop/Catalyst/data/eval_reports/frozen_db_v2_manifest_20260505_164525.json)

Current auditable claim from that JSON artifact:

- `source_db_sha256 = 3d8a1ee3a86b216779dfd9cc2e6ccd45090326c65b08e4a19e7b89d25935e491`
- `target_db_sha256 = 14231853db606cf5c422ba333a0c1fdf599ffb3b845f15bd3de9456694b7223f`
- `source_clean_assets = 10092`
- `target_clean_assets = 13474`
- `source_raw_assets = 10092`
- `target_raw_assets = 13474`
- `clean_assets_superset = true`
- `raw_assets_superset = true`
- `ohlcv_superset = true`

This document is intentionally brief. The JSON artifact remains the source of record for:

- row-count deltas
- backfill window
- provider assumptions
- rate-limit assumptions
- key-fingerprint provenance notes
