"""RunReport + RunConfig — H3 observability contracts.

H3 owns this module.  run_update(RunConfig) -> RunReport is the single
button-callable entrypoint.  Reports persisted to data/run_reports/.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class RunConfig:
    db_path: str = "data/catalyst_dev_ws4b.db"
    tickers: list[str] | None = None
    sources: list[str] | None = None
    from_date: str | None = None
    to_date: str | None = None
    limit: int | None = None
    dry_run: bool = False
    resume_from: str | None = None
    enable_fallback: bool = True
    skip_doctor: bool = False
    notes: str | None = None
    fetch_fn: Any | None = None
    report_dir: str = "data/run_reports"


@dataclass
class RunReport:
    run_id: str = ""
    mode: str = "update"
    resume_from: str | None = None
    config: dict[str, Any] = field(default_factory=dict)
    started_at: str = ""
    ended_at: str = ""
    elapsed_sec: float = 0.0
    providers: dict[str, dict[str, int]] = field(default_factory=dict)
    retry_histogram: dict[str, Any] = field(default_factory=dict)
    top_error_classes: dict[str, int] = field(default_factory=dict)
    fallbacks: dict[str, Any] = field(default_factory=dict)
    rows_changed: dict[str, int] = field(default_factory=dict)
    index_state: dict[str, int] = field(default_factory=dict)
    doctor: dict[str, Any] | None = None
    report_path: str = ""
    # S3 Data Belt §0.7 — observability fields
    embedded_count: int = 0
    queued_count: int = 0
    pending_after: int = 0
    skipped_ineligible: int = 0
    watermarks: dict[str, Any] = field(default_factory=dict)
    dedup: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def save_run_report(report: RunReport, report_dir: str = "data/run_reports") -> str:
    """Persist a RunReport to disk as JSON.  Returns the absolute report path."""
    os.makedirs(report_dir, exist_ok=True)
    path = os.path.join(report_dir, f"{report.run_id}.json")
    with open(path, "w") as f:
        json.dump(report.to_dict(), f, indent=2, default=str)
    return os.path.abspath(path)
