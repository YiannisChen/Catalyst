#!/bin/sh
set -eu

# After the daily job: restart the API only when the job published a new served corpus build
# (the API caches the served build for its lifetime). Any other day leaves in-flight runs alone.

data_dir=${CATALYST_DATA_DIR:-${CATALYST_RUNTIME_DATA_DIR:-/opt/catalyst/repo/data/m9}}
status_file=${CATALYST_DAILY_PIPELINE_STATUS:-$data_dir/daily_pipeline_status.json}
published_build=$(python3 - "$status_file" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as stream:
        status = json.load(stream)
except (OSError, json.JSONDecodeError):
    raise SystemExit(0)

corpus = status.get("corpus_fts_attempt", {}) if isinstance(status, dict) else {}
if (isinstance(status, dict) and status.get("daily_status") == "COMPLETED"
        and isinstance(corpus, dict) and corpus.get("status") == "ok"
        and corpus.get("build_id") and corpus.get("build_id") != corpus.get("previous_build_id")):
    print(corpus["build_id"])
PY
)

if [ -n "$published_build" ]; then
    systemctl --no-block try-restart catalyst-api.service
fi
