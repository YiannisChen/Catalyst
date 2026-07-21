"""B2 — normalized provenance tests."""
from __future__ import annotations

import hashlib


class TestProvenance:
    def test_record_provenance(self):
        """Single provenance record is persisted."""
        from conftest import _fresh_db_at_version, _seed_v2_raw_row
        from catalyst_data.ingestion.provenance import record_provenance

        db = _fresh_db_at_version(8)
        raw_id = _seed_v2_raw_row(db, raw_asset_id="raw:req-p1", request_id="req-p1")
        entity_version = hashlib.sha256(b"v1").hexdigest()

        record_provenance(db, entity_type="article", entity_id="poly:article123",
                          entity_version=entity_version, raw_asset_id=raw_id)

        rows = db.execute(
            "SELECT * FROM normalized_provenance WHERE entity_id = ?",
            ("poly:article123",),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["entity_type"] == "article"
        db.close()

    def test_provenance_multi_source(self):
        """Same entity from two raw sources records both."""
        from conftest import _fresh_db_at_version, _seed_v2_raw_row
        from catalyst_data.ingestion.provenance import record_provenance

        db = _fresh_db_at_version(8)
        raw_a = _seed_v2_raw_row(db, raw_asset_id="raw:req-a", request_id="req-a")
        raw_b = _seed_v2_raw_row(db, raw_asset_id="raw:req-b", request_id="req-b")
        entity_version = hashlib.sha256(b"v1").hexdigest()

        record_provenance(db, entity_type="article", entity_id="poly:multi",
                          entity_version=entity_version, raw_asset_id=raw_a)
        record_provenance(db, entity_type="article", entity_id="poly:multi",
                          entity_version=entity_version, raw_asset_id=raw_b)

        rows = db.execute(
            "SELECT raw_asset_id FROM normalized_provenance WHERE entity_id = ?",
            ("poly:multi",),
        ).fetchall()
        assert len(rows) == 2
        db.close()

    def test_provenance_idempotent(self):
        """Duplicate provenance record is a no-op (INSERT OR IGNORE)."""
        from conftest import _fresh_db_at_version, _seed_v2_raw_row
        from catalyst_data.ingestion.provenance import record_provenance

        db = _fresh_db_at_version(8)
        raw_id = _seed_v2_raw_row(db, raw_asset_id="raw:req-dup", request_id="req-dup")
        entity_version = hashlib.sha256(b"v1").hexdigest()

        record_provenance(db, entity_type="article", entity_id="poly:dup",
                          entity_version=entity_version, raw_asset_id=raw_id)
        record_provenance(db, entity_type="article", entity_id="poly:dup",
                          entity_version=entity_version, raw_asset_id=raw_id)

        count = db.execute(
            "SELECT COUNT(*) FROM normalized_provenance WHERE entity_id = 'poly:dup'"
        ).fetchone()[0]
        assert count == 1
        db.close()
