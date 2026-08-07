"""S3 Phase 4: D3 Watermark Derivation from source_checkpoints.

Design refs: D3, §0.7.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

DEV_DB = Path(__file__).resolve().parents[3] / "data" / "catalyst_dev_ws4b.db"


def _open_dev():
    conn = sqlite3.connect(f"file:{DEV_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _run_report_module():
    from catalyst_data import run_report as mod
    return mod


class TestWatermarkDerivation:
    """Watermarks derive from source_checkpoints, not a new table."""

    @pytest.mark.protected_artifact("data/catalyst_dev_ws4b.db")
    def test_source_checkpoints_has_watermarkable_data(self):
        """source_checkpoints table must exist with provider/date columns."""
        conn = _open_dev()
        try:
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(source_checkpoints)"
            )}
            assert "source_type" in cols, "source_checkpoints missing source_type column"
            assert "date" in cols, "source_checkpoints missing date column"
        finally:
            conn.close()

    def test_run_report_has_watermark_fields(self):
        """RunReport dataclass must include watermarks dict (per plan §0.7)."""
        mod = _run_report_module()
        report = mod.RunReport()
        d = report.to_dict()
        assert "watermarks" in d, "RunReport must have watermarks field"

    def test_run_report_has_dedup_fields(self):
        """RunReport must have dedup dict (nullable at CORE)."""
        mod = _run_report_module()
        report = mod.RunReport()
        d = report.to_dict()
        assert "dedup" in d, "RunReport must have dedup field"

    def test_run_report_has_embedded_queued_counts(self):
        """RunReport must include embedded_count, queued_count, pending_after, skipped_ineligible."""
        mod = _run_report_module()
        report = mod.RunReport()
        d = report.to_dict()
        for field in ("embedded_count", "queued_count", "pending_after", "skipped_ineligible"):
            assert field in d, f"RunReport missing {field} field"
