#!/bin/sh
set -eu

RUNTIME_DATA_DIR=${CATALYST_RUNTIME_DATA_DIR:-/opt/catalyst/repo/data/m9}
ARCHIVE_DB=${CATALYST_HEADLINE_ARCHIVE_DB:-${RUNTIME_DATA_DIR%/}/headline_archive.sqlite3}
END=$(date -u -d yesterday +%F)
START=$(date -u -d "$END - 7 days" +%F)

exec /opt/catalyst/venv/bin/python -m catalyst_data.ingestion.collectors.headline_archive \
  --store "$ARCHIVE_DB" --start "$START" --end "$END"
