"""Canonical temporal/materiality/source audit gates (M3-7).

Execution-lock §H: one shared serving predicate
``S = serving_status IN ('body_candidate','lead_candidate')`` in every ratio's
numerator and denominator; every ratio denominator must be > 0 or the audit
fails closed; structured-fact reconciliation and provenance integrity are part
of the no-orphan gate.
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass

SERVING_PREDICATE = "serving_status IN ('body_candidate','lead_candidate')"


@dataclass(frozen=True)
class CanonicalAudit:
    total_assets: int
    assets_by_content_state: Mapping[str, int]
    eligible_assets: int
    material_capable_evidence: int
    empty_failed_excluded: int
    searchable_news: int
    searchable_serving_evidence: int
    source_class_present: int
    publisher_present: int
    dedup_identity_covered: int
    no_orphan_count: int
    missing_provenance_coverage: int
    structured_fact_certified: int
    structured_fact_excluded: int
    structured_fact_ref_per_source_row_shortfall: int
    structured_fact_certified_plus_excluded_shortfall: int
    structured_fact_populated_tables_without_certified: tuple[str, ...]
    source_class_gate: bool
    publisher_gate: bool
    dedup_identity_gate: bool
    no_orphan_gate: bool
    empty_failed_gate: bool
    structured_reconciliation_gate: bool
    passed: bool
    failures: tuple[str, ...]


def _count(conn: sqlite3.Connection, sql: str) -> int:
    return conn.execute(sql).fetchone()[0]


def _no_orphan_count(conn: sqlite3.Connection) -> int:
    """Sum of execution-lock §H no-orphan violation dimensions (a)-(h)."""
    return sum(
        [
            # (a) ticker rows without their asset
            _count(
                conn,
                "SELECT COUNT(*) FROM canonical_asset_tickers t "
                "LEFT JOIN canonical_assets a ON a.asset_id = t.asset_id "
                "WHERE a.asset_id IS NULL",
            ),
            # (b) content versions without their asset
            _count(
                conn,
                "SELECT COUNT(*) FROM canonical_content_versions v "
                "LEFT JOIN canonical_assets a ON a.asset_id = v.asset_id "
                "WHERE a.asset_id IS NULL",
            ),
            # (c) associations without their asset
            _count(
                conn,
                "SELECT COUNT(*) FROM canonical_subtype_assoc s "
                "LEFT JOIN canonical_assets a ON a.asset_id = s.asset_id "
                "WHERE a.asset_id IS NULL",
            ),
            # (d) associations whose subtype row does not resolve
            _count(
                conn,
                "SELECT COUNT(*) FROM canonical_subtype_assoc s WHERE NOT ("
                "(s.subtype_table='articles' AND EXISTS "
                "(SELECT 1 FROM articles a WHERE a.article_id = s.subtype_pk_value))"
                " OR (s.subtype_table='filings' AND EXISTS "
                "(SELECT 1 FROM filings f WHERE f.filing_id = s.subtype_pk_value))"
                " OR (s.subtype_table='filing_documents' AND EXISTS "
                "(SELECT 1 FROM filing_documents d WHERE d.document_id = s.subtype_pk_value))"
                ")",
            ),
            # (e) association content version missing or owned by a different asset
            _count(
                conn,
                "SELECT COUNT(*) FROM canonical_subtype_assoc s "
                "LEFT JOIN canonical_content_versions v "
                "ON v.canonical_content_version_id = s.canonical_content_version_id "
                "WHERE s.canonical_content_version_id IS NOT NULL "
                "AND (v.canonical_content_version_id IS NULL OR v.asset_id != s.asset_id)",
            ),
            # (f) fact refs that do not resolve to a source row
            _count(
                conn,
                "SELECT COUNT(*) FROM canonical_structured_fact_refs f WHERE NOT ("
                "(f.source_table='ohlcv' AND EXISTS (SELECT 1 FROM ohlcv o WHERE "
                "json_extract(f.source_pk_json,'$.symbol') = o.symbol AND "
                "json_extract(f.source_pk_json,'$.date') = o.date))"
                " OR (f.source_table='macro_observations' AND EXISTS "
                "(SELECT 1 FROM macro_observations m WHERE "
                "json_extract(f.source_pk_json,'$.series_id') = m.series_id AND "
                "json_extract(f.source_pk_json,'$.observation_date') = m.observation_date))"
                " OR (f.source_table='fundamental_statements' AND EXISTS "
                "(SELECT 1 FROM fundamental_statements fs WHERE "
                "json_extract(f.source_pk_json,'$.statement_id') = fs.statement_id))"
                ")",
            ),
            # (g) provenance canonical_asset_id without the asset
            _count(
                conn,
                "SELECT COUNT(*) FROM normalized_provenance p "
                "LEFT JOIN canonical_assets a ON a.asset_id = p.canonical_asset_id "
                "WHERE p.canonical_asset_id IS NOT NULL AND a.asset_id IS NULL",
            ),
            # (h) provenance canonical_content_version_id missing or owned by a
            #     different asset (when both are non-null)
            _count(
                conn,
                "SELECT COUNT(*) FROM normalized_provenance p "
                "LEFT JOIN canonical_content_versions v "
                "ON v.canonical_content_version_id = p.canonical_content_version_id "
                "WHERE p.canonical_content_version_id IS NOT NULL "
                "AND (v.canonical_content_version_id IS NULL "
                "OR (p.canonical_asset_id IS NOT NULL "
                "AND v.asset_id != p.canonical_asset_id))",
            ),
        ]
    )


def _missing_provenance_coverage(conn: sqlite3.Connection) -> int:
    """Serving-eligible assets and content versions without provenance rows."""
    return _count(
        conn,
        "SELECT COUNT(*) FROM canonical_assets a WHERE " + SERVING_PREDICATE.replace(
            "serving_status", "a.serving_status"
        ) + " AND NOT EXISTS (SELECT 1 FROM normalized_provenance p "
        "WHERE p.canonical_asset_id = a.asset_id)",
    ) + _count(
        conn,
        "SELECT COUNT(*) FROM canonical_content_versions v "
        "WHERE NOT EXISTS (SELECT 1 FROM normalized_provenance p "
        "WHERE p.canonical_content_version_id = v.canonical_content_version_id)",
    )


def _filing_document_provenance_missing(conn: sqlite3.Connection) -> int:
    """Filing-document associations must keep entity_type='filing',
    entity_id=document_id provenance (execution-lock §C)."""
    return _count(
        conn,
        "SELECT COUNT(*) FROM canonical_subtype_assoc s "
        "WHERE s.subtype_table='filing_documents' AND NOT EXISTS ("
        "SELECT 1 FROM normalized_provenance p "
        "WHERE p.entity_type='filing' AND p.entity_id = s.subtype_pk_value)",
    )


def audit_canonical(
    conn: sqlite3.Connection, *, cutoff_at: str | None = None
) -> CanonicalAudit:
    failures: list[str] = []

    total_assets = _count(conn, "SELECT COUNT(*) FROM canonical_assets")
    state_counts = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT content_state, COUNT(*) FROM canonical_assets "
            "GROUP BY content_state"
        ).fetchall()
    }
    eligible_sql = "SELECT COUNT(*) FROM canonical_assets WHERE eligible_at IS NOT NULL"
    eligible_params: tuple = ()
    if cutoff_at is not None:
        eligible_sql += " AND eligible_at <= ?"
        eligible_params = (cutoff_at,)
    eligible_assets = conn.execute(eligible_sql, eligible_params).fetchone()[0]
    material_capable_evidence = _count(
        conn,
        "SELECT COUNT(*) FROM canonical_assets WHERE content_state='FULL_TEXT' "
        "AND " + SERVING_PREDICATE,
    )
    empty_failed_excluded = _count(
        conn,
        "SELECT COUNT(*) FROM canonical_assets "
        "WHERE content_state IN ('EMPTY','FAILED') AND " + SERVING_PREDICATE,
    )
    searchable_news = _count(
        conn,
        "SELECT COUNT(DISTINCT asset_id) FROM canonical_assets "
        "WHERE asset_type='NEWS' AND " + SERVING_PREDICATE,
    )
    searchable_serving_evidence = _count(
        conn,
        "SELECT COUNT(DISTINCT asset_id) FROM canonical_assets WHERE "
        + SERVING_PREDICATE,
    )
    source_class_present = _count(
        conn,
        "SELECT COUNT(DISTINCT asset_id) FROM canonical_assets "
        "WHERE asset_type='NEWS' AND " + SERVING_PREDICATE
        + " AND source_class IS NOT NULL AND trim(source_class) <> ''",
    )
    publisher_present = _count(
        conn,
        "SELECT COUNT(DISTINCT asset_id) FROM canonical_assets "
        "WHERE asset_type='NEWS' AND " + SERVING_PREDICATE
        + " AND publisher IS NOT NULL AND trim(publisher) <> ''",
    )
    dedup_identity_covered = _count(
        conn,
        "SELECT COUNT(DISTINCT asset_id) FROM canonical_assets WHERE "
        + SERVING_PREDICATE + " AND dedup_cluster_id IS NOT NULL",
    )

    if searchable_news == 0:
        failures.append("source_class_denominator_zero")
        failures.append("publisher_denominator_zero")
        source_class_gate = False
        publisher_gate = False
    else:
        source_class_gate = source_class_present * 100 >= searchable_news * 95
        publisher_gate = publisher_present * 100 >= searchable_news * 95
        if not source_class_gate:
            failures.append("source_class_gate")
        if not publisher_gate:
            failures.append("publisher_gate")

    if searchable_serving_evidence == 0:
        failures.append("dedup_denominator_zero")
        dedup_identity_gate = False
    else:
        dedup_identity_gate = (
            dedup_identity_covered * 100 >= searchable_serving_evidence * 95
        )
        if not dedup_identity_gate:
            failures.append("dedup_identity_gate")

    missing_provenance_coverage = _missing_provenance_coverage(conn)
    filing_doc_provenance = _filing_document_provenance_missing(conn)
    no_orphan_count = (
        _no_orphan_count(conn)
        + missing_provenance_coverage
        + filing_doc_provenance
    )
    no_orphan_gate = no_orphan_count == 0
    if not no_orphan_gate:
        failures.append("no_orphan_gate")

    empty_failed_gate = empty_failed_excluded == 0
    if not empty_failed_gate:
        failures.append("empty_failed_gate")

    # --- structured-fact reconciliation (execution-lock §C/§H) ---------------
    structured_fact_certified = _count(
        conn,
        "SELECT COUNT(*) FROM canonical_structured_fact_refs "
        "WHERE certification_status='certified'",
    )
    structured_fact_excluded = _count(
        conn,
        "SELECT COUNT(*) FROM canonical_structured_fact_refs "
        "WHERE certification_status IN ('excluded_missing_time','excluded_invalid')",
    )
    ref_per_source_row_shortfall = 0
    certified_plus_excluded_shortfall = 0
    tables_without_certified: list[str] = []
    unrun_populated_tables: list[str] = []
    for source_table, pk_sql in (
        ("ohlcv", "symbol=? AND date=?"),
        ("macro_observations", "series_id=? AND observation_date=?"),
        ("fundamental_statements", "statement_id=?"),
    ):
        source_rows = conn.execute(
            f"SELECT COUNT(*) FROM {source_table}"
        ).fetchone()[0]
        refs = conn.execute(
            "SELECT COUNT(*), "
            "SUM(CASE WHEN certification_status='certified' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN certification_status IN "
            "('excluded_missing_time','excluded_invalid') THEN 1 ELSE 0 END) "
            "FROM canonical_structured_fact_refs WHERE source_table=?",
            (source_table,),
        ).fetchone()
        ref_count, certified, excluded = refs[0], refs[1] or 0, refs[2] or 0
        # exactly one ref per source row (UNIQUE enforces; audit asserts)
        if source_rows and ref_count != source_rows:
            ref_per_source_row_shortfall += abs(source_rows - ref_count)
        if source_rows and certified + excluded != source_rows:
            certified_plus_excluded_shortfall += abs(
                source_rows - (certified + excluded)
            )
        if source_rows and certified == 0:
            tables_without_certified.append(source_table)
        if source_rows and ref_count == 0:
            unrun_populated_tables.append(source_table)
    populated_without_certified = tuple(tables_without_certified)
    # Certifier-ran liveness: a populated table with zero refs means the
    # certifier never ran and fails closed. Complete all-excluded coverage
    # (one ref per source row, certified+excluded == source_rows, certified==0)
    # is NOT a reconciliation failure; it is recorded for M4 exclusion only.
    structured_reconciliation_gate = (
        ref_per_source_row_shortfall == 0
        and certified_plus_excluded_shortfall == 0
        and not unrun_populated_tables
    )
    if not structured_reconciliation_gate:
        failures.append("structured_reconciliation_gate")

    passed = not failures
    return CanonicalAudit(
        total_assets=total_assets,
        assets_by_content_state=dict(state_counts),
        eligible_assets=eligible_assets,
        material_capable_evidence=material_capable_evidence,
        empty_failed_excluded=empty_failed_excluded,
        searchable_news=searchable_news,
        searchable_serving_evidence=searchable_serving_evidence,
        source_class_present=source_class_present,
        publisher_present=publisher_present,
        dedup_identity_covered=dedup_identity_covered,
        no_orphan_count=no_orphan_count,
        missing_provenance_coverage=missing_provenance_coverage,
        structured_fact_certified=structured_fact_certified,
        structured_fact_excluded=structured_fact_excluded,
        structured_fact_ref_per_source_row_shortfall=ref_per_source_row_shortfall,
        structured_fact_certified_plus_excluded_shortfall=certified_plus_excluded_shortfall,
        structured_fact_populated_tables_without_certified=populated_without_certified,
        source_class_gate=source_class_gate,
        publisher_gate=publisher_gate,
        dedup_identity_gate=dedup_identity_gate,
        no_orphan_gate=no_orphan_gate,
        empty_failed_gate=empty_failed_gate,
        structured_reconciliation_gate=structured_reconciliation_gate,
        passed=passed,
        failures=tuple(failures),
    )


__all__ = ["CanonicalAudit", "SERVING_PREDICATE", "audit_canonical"]
