# T-03 Inspection Note: Ingestion Runs + Checkpoints + Quality Flags

## Config Location

Quality thresholds and knobs live in [packages/data-core/catalyst_data/config.py](/Users/yiannischen/Desktop/Catalyst/packages/data-core/catalyst_data/config.py):

- `RAG_MIN_CHAR_COUNT` from `CATALYST_RAG_MIN_CHAR_COUNT` (default `200`)
- `TARGET_LANGUAGE` from `CATALYST_TARGET_LANGUAGE` (default `"en"`)
- `TEMPLATE_SPAM_DUPLICATE_THRESHOLD` from `CATALYST_TEMPLATE_SPAM_DUPLICATE_THRESHOLD` (default `5`)

## Template/Spam Heuristic

The full heuristic shipped, not a stub.

- Implementation path: [quality.py](/Users/yiannischen/Desktop/Catalyst/packages/data-core/catalyst_data/quality.py)
- Rule: `normalize_title(title)` count across all existing `clean_assets` rows, with `template_spam` when the normalized title frequency is `>= TEMPLATE_SPAM_DUPLICATE_THRESHOLD`.
- Current storage constraint: the heuristic operates on the leading title parsed from Silver markdown because the current `clean_assets` schema does not store title as a first-class column.

## Post-Migration Row Counts

From the migration run on this branch:

- `data/catalyst_dev.db`: `clean_assets=12`, `asset_quality_flags=12`, `eligible=3`
- `data/catalyst_eval_frozen.db`: `clean_assets=0`, `asset_quality_flags=0`, `eligible=0`
- `data/catalyst_demo.db`: `clean_assets=0`, `asset_quality_flags=0`, `eligible=0`

Dev DB quality-reason breakdown:

- `eligible=3`
- `missing_fields=8`
- `short_text=1`

## Schema Drift Surprises

- `clean_assets` does not currently have explicit `title`, `published_utc`, `source`, or `detected_language` columns. Those fields had to be derived from the existing Markdown contract instead of read from dedicated Silver columns.
- The existing `polygon_news` Silver rows are not always one-article-per-row. Some rows contain multiple rendered article sections in a single `content_md`, so the migration evaluates the leading title and leading metadata block for the per-asset quality gate.
- No cross-DB schema conflict occurred when applying the new side tables. `asset_quality_flags` remained a side table keyed by `asset_id`; `clean_assets` was not altered.
