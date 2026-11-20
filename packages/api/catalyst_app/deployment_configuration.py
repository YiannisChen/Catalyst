from __future__ import annotations

import os
from collections.abc import Mapping

PUBLIC_DEPLOYMENT_PINNED_ENV = {
    "CATALYST_DB_PATH": "/opt/catalyst/repo/data/m9/catalyst_corpus.sqlite3",
    "CATALYST_RUNTIME_DB_PATH": "/var/lib/catalyst-api/live_runtime.sqlite3",
    "CATALYST_V2_RULES": "all",
    "CATALYST_EVIDENCE_WINDOW_POLICY": "desk_cascade_v1",
    "CATALYST_RETRIEVAL_PROFILE": "lexical_display_v1",
    "CATALYST_ALLOW_NON_LOOPBACK": "1",
    "CATALYST_TRUSTED_PROXY_CIDRS": "127.0.0.1/32",
    "CATALYST_DATA_REFRESH": "0",
    "CATALYST_WEB_EVIDENCE": "discovery_v2",
    "CATALYST_PUBLIC_TAVILY_ENABLED": "0",
    "CATALYST_SQLITE_LIVE_READ": "1",
    "CATALYST_FILING_UNITS": "",
}

# Also forced on the public unit's command line, but not checked at startup: tests run public mode with
# other quotas. Visitors without their own key only replay featured cases (owner, 2026-11-18).
PUBLIC_DEPLOYMENT_COMMAND_LINE_ONLY = {
    "CATALYST_ANONYMOUS_DAILY_RUN_LIMIT": "0",
}


def validate_public_deployment_environment(env: Mapping[str, str] | None = None) -> None:
    current = os.environ if env is None else env
    mismatches = [
        name
        for name, expected in PUBLIC_DEPLOYMENT_PINNED_ENV.items()
        if current.get(name) != expected
    ]
    if mismatches:
        names = ", ".join(sorted(mismatches))
        raise RuntimeError(f"deployment_configuration: invalid public deployment pins: {names}")
