# T-02 Inspection Note: DB Triple Split + Path Config

## Findings

- `packages/data-core/catalyst_data/config.py` did not expose any DB-path helper before T-02.
- `data/dev_assets.db` is the active populated SQLite development database.
- `data/catalyst_dev.db` and `data/catalyst_eval_frozen.db` existed only as empty local placeholders before this task.

## Decision

Use a symlink for the development DB: `data/catalyst_dev.db -> dev_assets.db`.

## Rationale

- The existing `data/dev_assets.db` already contains the live Bronze/Silver schema and data.
- A symlink avoids creating two mutable development databases that can drift out of sync.
- Legacy scripts that still point at `data/dev_assets.db` keep working unchanged, while new code can standardize on `data/catalyst_dev.db`.

## T-02 Changes

- Added `catalyst_data.config.db_path()` with `CATALYST_DB_PATH` override support and a repo-root default of `data/catalyst_dev.db`.
- Recreated `data/catalyst_eval_frozen.db` and `data/catalyst_demo.db` as schema-only SQLite stubs via the existing `init_db(...)`.
- Created `data/traces/` and `notebooks/` with `.gitkeep` placeholders. `data/eval_reports/` and `docs/testing/` already existed, so no new placeholders were needed there.
