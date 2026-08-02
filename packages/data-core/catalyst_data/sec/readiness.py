"""sec_source_ready / sec_evidence_ready — Pre-B6 production readiness.

Implements design D.1/D.2 with explicit S1/S2/S4 lineage, per-document
checkpoint oracle, and frozen inventory carry-in binding.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from catalyst_data.config import RAG_MIN_CHAR_COUNT
from catalyst_data.corpus.streaming_publication import served_chunks_relation

SEARCHABLE = ("active", "pending_embedding", "embedded", "metadata_only")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class SecReadinessError(ValueError):
    """Typed readiness contract failure."""


def compute_pre_b6_gate_states(
    *,
    b2o_source_ready: bool,
    required_provenance_ready: bool,
    comparable_gate_ready: bool,
    sec_source_ready: bool,
    sec_evidence_ready: bool,
) -> dict[str, bool]:
    """Compute independent source and post-corpus Pre-B6 gates."""
    prebuild_source_ready = all(
        (
            b2o_source_ready,
            required_provenance_ready,
            comparable_gate_ready,
            sec_source_ready,
        )
    )
    return {
        "prebuild_source_ready": prebuild_source_ready,
        "postbuild_evidence_ready": prebuild_source_ready and sec_evidence_ready,
    }


@dataclass
class SecReadinessReport:
    sec_source_ready: bool
    sec_evidence_ready: bool
    expected_mandatory_count: int
    fetched_count: int
    extracted_count: int
    provenance_valid_count: int
    chunked_count: int
    missing_document_ids: list[str] = field(default_factory=list)
    failed_document_ids: list[str] = field(default_factory=list)
    optional_degraded_ids: list[str] = field(default_factory=list)
    missing_carry_in_slots: list[str] = field(default_factory=list)
    inventory_id: str | None = None
    s1_complete: bool = False
    s2_complete: bool = False
    s1_lineage_run_ids: list[str] = field(default_factory=list)
    s2_lineage_run_ids: list[str] = field(default_factory=list)
    s4_lineage_run_ids: list[str] = field(default_factory=list)
    checkpoint_failed_document_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _doc_text_ok(text: str | None) -> bool:
    return bool(text) and len(text.strip()) >= RAG_MIN_CHAR_COUNT


def require_hex64(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise SecReadinessError(f"{label} must be 64-char lowercase hex")
    return value


def mandatory_document_ids_from_inventory(
    inventory: Mapping[str, Any],
) -> tuple[list[str], list[str]]:
    """Extract mandatory and optional_degraded document_ids from frozen inventory."""
    mandatory: list[str] = []
    optional: list[str] = []
    for entry in inventory.get("sorted_filing_entries") or []:
        for doc in entry.get("documents") or []:
            doc_id = doc.get("document_id")
            if not doc_id:
                continue
            req = doc.get("requiredness") or "mandatory"
            if req == "optional_degraded":
                optional.append(str(doc_id))
            else:
                mandatory.append(str(doc_id))
    return mandatory, optional


def load_frozen_filing_inventory(
    path: str | Path,
    *,
    expected_inventory_id: str | None = None,
) -> dict[str, Any]:
    """Load and verify frozen FilingInventoryManifest artifact."""
    p = Path(path)
    if not p.is_file():
        raise SecReadinessError(f"filing inventory artifact missing: {p}")
    try:
        body = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SecReadinessError(f"invalid filing inventory artifact: {exc}") from exc
    if not isinstance(body, dict):
        raise SecReadinessError("filing inventory must be a JSON object")
    inv_id = body.get("inventory_id")
    if not inv_id or not isinstance(inv_id, str):
        raise SecReadinessError("filing inventory missing inventory_id")
    require_hex64(inv_id, label="inventory_id")
    from catalyst_data.sec.inventory import compute_inventory_id

    recomputed = compute_inventory_id(body)
    if recomputed != inv_id:
        raise SecReadinessError(
            f"inventory_id mismatch: stored={inv_id[:12]}… expected={recomputed[:12]}…"
        )
    if expected_inventory_id is not None:
        require_hex64(expected_inventory_id, label="expected_inventory_id")
        if inv_id != expected_inventory_id:
            raise SecReadinessError(
                f"inventory_id does not match expected: {expected_inventory_id[:12]}…"
            )
    if "missing_carry_in_slots" not in body:
        raise SecReadinessError("frozen inventory missing missing_carry_in_slots")
    from catalyst_data.manifests.universe import RATIFIED_TICKERS, load_universe_spec
    from catalyst_data.sec.inventory import compute_missing_carry_in_slots

    tickers = list(body.get("universe_tickers") or [])
    if tickers != list(RATIFIED_TICKERS):
        raise SecReadinessError("frozen inventory does not bind exact ratified 40 universe")
    spec_path = (
        Path(__file__).resolve().parents[1]
        / "manifests"
        / "universe_v1_2025_08.spec.json"
    )
    spec = load_universe_spec(spec_path)
    expected_issuer_map = {
        ticker: str(spec.companies[ticker]["issuer_class"])
        for ticker in spec.tickers
    }
    if body.get("issuer_class_by_ticker") != expected_issuer_map:
        raise SecReadinessError("frozen inventory issuer class map drift")
    recomputed_missing = compute_missing_carry_in_slots(
        tickers=tickers,
        filing_entries=list(body.get("sorted_filing_entries") or []),
        issuer_class_by_ticker=expected_issuer_map,
    )
    if list(body["missing_carry_in_slots"]) != recomputed_missing:
        raise SecReadinessError("frozen inventory missing_carry_in_slots drift")
    return body


def validate_inventory_runtime_identity(
    inventory: Mapping[str, Any],
    *,
    universe_manifest_id: str,
    source_snapshot_id: str,
) -> None:
    """Require the frozen inventory to bind the runtime audit identities."""
    if inventory.get("universe_manifest_id") != universe_manifest_id:
        raise SecReadinessError(
            "filing inventory universe_manifest_id does not match runtime universe"
        )
    if inventory.get("source_snapshot_id") != source_snapshot_id:
        raise SecReadinessError(
            "filing inventory source_snapshot_id does not match baseline source snapshot"
        )


def resolve_run_lineage(
    conn: sqlite3.Connection,
    terminal_run_id: str,
    *,
    expected_plan_hash: str | None = None,
) -> list[str]:
    """Resolve [terminal, …, root]. Validates plan_hash chain when expected given."""
    if not terminal_run_id or not str(terminal_run_id).strip():
        raise SecReadinessError("terminal_run_id required")
    row = conn.execute(
        "SELECT plan_hash, expected_plan_hash, parent_run_id FROM ingestion_runs "
        "WHERE run_id=?",
        (terminal_run_id,),
    ).fetchone()
    if row is None:
        raise SecReadinessError(f"terminal run not found: {terminal_run_id}")
    plan_h, exp_h, parent = row[0], row[1], row[2]
    if expected_plan_hash is not None:
        require_hex64(expected_plan_hash, label="expected_plan_hash")
        if plan_h != expected_plan_hash:
            raise SecReadinessError(
                f"terminal plan_hash drift for {terminal_run_id}: "
                f"stored={plan_h} expected={expected_plan_hash}"
            )
        if exp_h is not None and exp_h != expected_plan_hash:
            raise SecReadinessError(
                f"terminal expected_plan_hash drift for {terminal_run_id}"
            )
    lineage = [terminal_run_id]
    seen = {terminal_run_id}
    current = parent
    while current:
        if current in seen:
            raise SecReadinessError(f"cycle in lineage at {current}")
        seen.add(current)
        prow = conn.execute(
            "SELECT plan_hash, expected_plan_hash, parent_run_id FROM ingestion_runs "
            "WHERE run_id=?",
            (current,),
        ).fetchone()
        if prow is None:
            raise SecReadinessError(f"parent run not found: {current}")
        if expected_plan_hash is not None:
            if prow[0] != expected_plan_hash:
                raise SecReadinessError(
                    f"ancestor {current} plan_hash drift vs {expected_plan_hash}"
                )
            if prow[1] is not None and prow[1] != expected_plan_hash:
                raise SecReadinessError(
                    f"ancestor {current} expected_plan_hash drift"
                )
        lineage.append(current)
        current = prow[2]
    return lineage


def _lineage_placeholders(lineage: Sequence[str]) -> str:
    if not lineage:
        raise SecReadinessError("empty lineage")
    return ",".join("?" for _ in lineage)


def verify_s1_complete_40(
    conn: sqlite3.Connection,
    *,
    lineage_run_ids: Sequence[str],
    tickers: Sequence[str],
) -> bool:
    """True iff exactly the provided 40 tickers each have terminal-complete S1."""
    if len(tickers) != 40:
        return False
    ph = _lineage_placeholders(lineage_run_ids)
    for ticker in tickers:
        row = conn.execute(
            f"""SELECT status, COALESCE(is_complete,0) FROM source_checkpoints
                WHERE run_id IN ({ph})
                  AND endpoint_name='sec_submissions'
                  AND ticker=?
                ORDER BY rowid DESC LIMIT 1""",
            list(lineage_run_ids) + [ticker],
        ).fetchone()
        if not row or row[0] not in ("success", "success_empty") or int(row[1]) != 1:
            return False
    return True


def verify_s2_index_complete(
    conn: sqlite3.Connection,
    *,
    lineage_run_ids: Sequence[str],
    index_cell_ids: Sequence[str],
) -> bool:
    """Every selected sec_filing_index cell_id terminal-complete on S2 lineage."""
    if not index_cell_ids:
        return False
    ph = _lineage_placeholders(lineage_run_ids)
    for cell_id in index_cell_ids:
        row = conn.execute(
            f"""SELECT status, COALESCE(is_complete,0), endpoint_name
                FROM source_checkpoints
                WHERE run_id IN ({ph}) AND cell_id=?
                ORDER BY rowid DESC LIMIT 1""",
            list(lineage_run_ids) + [cell_id],
        ).fetchone()
        if not row:
            return False
        if row[2] not in (None, "sec_filing_index") and row[2] != "sec_filing_index":
            # require endpoint when present
            if row[2] != "sec_filing_index":
                return False
        if row[0] not in ("success", "success_empty") or int(row[1]) != 1:
            return False
        if row[2] is not None and row[2] != "sec_filing_index":
            return False
    return True


def _document_cells_from_inventory(
    inventory: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Map document_id → descriptor from frozen inventory (unique)."""
    out: dict[str, dict[str, Any]] = {}
    for entry in inventory.get("sorted_filing_entries") or []:
        for doc in entry.get("documents") or []:
            doc_id = doc.get("document_id")
            if not doc_id:
                continue
            if doc_id in out:
                raise SecReadinessError(f"duplicate document_id in inventory: {doc_id[:12]}…")
            out[str(doc_id)] = {
                **dict(doc),
                "inventory_id": inventory.get("inventory_id"),
                "ticker": entry.get("ticker"),
                "accession_number": entry.get("accession_number")
                or doc.get("accession_number"),
                "filed_date": (
                    entry.get("filed_date")
                    or entry.get("filed_at")
                    or doc.get("filed_date")
                    or doc.get("filed_at")
                ),
                "filing_id": entry.get("filing_id") or doc.get("filing_id"),
                "provider_profile_version": (
                    entry.get("provider_profile_version")
                    or doc.get("provider_profile_version")
                    or "v1"
                ),
            }
    return out


def verify_mandatory_document_checkpoint(
    conn: sqlite3.Connection,
    *,
    document_id: str,
    s4_lineage_run_ids: Sequence[str],
    inventory_id: str,
    document_descriptor: Mapping[str, Any] | None = None,
) -> tuple[bool, str | None]:
    """Per-document S4 checkpoint + extraction + provenance oracle.

    Returns (ok, failure_reason).
    """
    if document_descriptor is None:
        return False, "missing_document_descriptor"
    from catalyst_data.ingestion.request_ledger import compute_logical_fetch_id
    from catalyst_data.sec.document_cells import build_document_cell

    required_descriptor = (
        "ticker",
        "filed_date",
        "accession_number",
        "document_role",
        "document_file",
        "document_url",
        "filing_id",
        "requiredness",
        "requiredness_reason",
    )
    if any(not document_descriptor.get(key) for key in required_descriptor):
        return False, "incomplete_document_descriptor"
    expected_cell = build_document_cell(
        ticker=str(document_descriptor["ticker"]),
        filed_date=str(document_descriptor["filed_date"])[:10],
        inventory_id=inventory_id,
        accession_number=str(document_descriptor["accession_number"]),
        document_role=str(document_descriptor["document_role"]),
        document_file=str(document_descriptor["document_file"]),
        document_url=str(document_descriptor["document_url"]),
        filing_id=str(document_descriptor["filing_id"]),
        requiredness=str(document_descriptor["requiredness"]),
        requiredness_reason=str(document_descriptor["requiredness_reason"]),
        provider_profile_version=str(
            document_descriptor.get("provider_profile_version") or "v1"
        ),
    )
    if expected_cell["identity_extensions"]["document_id"] != document_id:
        return False, "inventory_document_id_mismatch"
    ph = _lineage_placeholders(s4_lineage_run_ids)
    lineage_checkpoints = conn.execute(
        f"""SELECT run_id FROM source_checkpoints
            WHERE run_id IN ({ph}) AND cell_id=? AND endpoint_name='sec_document'""",
        [*s4_lineage_run_ids, expected_cell["cell_id"]],
    ).fetchall()
    if len(lineage_checkpoints) > 1:
        return False, "duplicate_checkpoint"

    prov = conn.execute(
        """SELECT raw_asset_id FROM normalized_provenance
           WHERE entity_type='filing' AND entity_id=?""",
        (document_id,),
    ).fetchall()
    if len(prov) != 1:
        if not prov:
            return False, "missing_provenance"
        return False, "duplicate_provenance"
    raw_id = prov[0][0]
    if not raw_id:
        return False, "empty_raw_asset_id"

    # filing_documents quality
    documents = conn.execute(
        """SELECT extraction_status, text, document_id FROM filing_documents
           WHERE document_id=?""",
        (document_id,),
    ).fetchall()
    if len(documents) != 1:
        if not documents:
            return False, "missing_filing_document"
        return False, "duplicate_filing_document"
    fd = documents[0]
    if fd[0] != "success" or not _doc_text_ok(fd[1]):
        return False, "extraction_or_quality_failed"

    ra_cols = {r[1] for r in conn.execute("PRAGMA table_info(raw_assets)").fetchall()}
    if "request_id" not in ra_cols:
        return False, "raw_assets_missing_request_id"
    raw_rows = conn.execute(
        "SELECT request_id, response_sha256 FROM raw_assets WHERE asset_id=?",
        (raw_id,),
    ).fetchall()
    if len(raw_rows) != 1 or not raw_rows[0][0]:
        return False, "raw_asset_missing"
    request_id, raw_sha = raw_rows[0]
    if not _HEX64.fullmatch(str(raw_sha or "")):
        return False, "invalid_raw_response_sha256"

    attempts = conn.execute(
        """SELECT run_id, logical_fetch_id, endpoint_name, status, raw_asset_id,
                  response_sha256, provider, ticker_or_series, window_start,
                  window_end, request_params_redacted
           FROM provider_request_attempts WHERE request_id=?""",
        (request_id,),
    ).fetchall()
    if len(attempts) != 1:
        return False, "duplicate_or_missing_attempt"
    (
        run_id,
        lfid,
        endpoint,
        status,
        att_raw,
        att_sha,
        provider,
        attempt_ticker,
        attempt_start,
        attempt_end,
        params_json,
    ) = attempts[0]
    if run_id not in set(s4_lineage_run_ids):
        return False, "wrong_s4_lineage"
    if endpoint != "sec_document" or provider != "sec":
        return False, "wrong_endpoint_or_provider"
    if status != "SUCCEEDED":
        return False, "attempt_not_succeeded"
    if not att_raw or att_raw != raw_id:
        return False, "attempt_raw_asset_mismatch"
    if not _HEX64.fullmatch(str(att_sha or "")) or att_sha != raw_sha:
        return False, "response_sha256_mismatch"
    expected_lfid = compute_logical_fetch_id(run_id, expected_cell["cell_id"])
    if lfid != expected_lfid:
        return False, "logical_fetch_id_mismatch"
    succeeded_attempts = conn.execute(
        """SELECT request_id, raw_asset_id, response_sha256
           FROM provider_request_attempts
           WHERE run_id=? AND logical_fetch_id=? AND endpoint_name='sec_document'
             AND status='SUCCEEDED'""",
        (run_id, expected_lfid),
    ).fetchall()
    if succeeded_attempts != [(request_id, raw_id, raw_sha)]:
        return False, "duplicate_or_mismatched_successful_attempt"
    expected_date = expected_cell["window_start"]
    if (
        attempt_ticker != expected_cell["subject"]
        or attempt_start != expected_date
        or attempt_end != expected_cell["window_end"]
    ):
        return False, "attempt_cell_identity_mismatch"

    try:
        params = json.loads(params_json)
    except (TypeError, json.JSONDecodeError):
        return False, "invalid_request_params_redacted"
    if not isinstance(params, dict):
        return False, "invalid_request_params_redacted"
    expected_params = {
        "ticker": expected_cell["subject"],
        "window_start": expected_cell["window_start"],
        "window_end": expected_cell["window_end"],
        "page_no": 1,
        "endpoint_name": "sec_document",
        "cell_id": expected_cell["cell_id"],
        "identity_schema_version": expected_cell["identity_schema_version"],
        **expected_cell["identity_extensions"],
    }
    missing_param_keys = set(expected_params) - set(params)
    if missing_param_keys:
        return False, "missing_request_identity_keys"
    for key, expected in expected_params.items():
        if params.get(key) != expected:
            return False, f"{key}_identity_mismatch"

    cp_rows = conn.execute(
        """SELECT status, COALESCE(is_complete,0), request_count, logical_fetch_id,
                  raw_asset_id, cell_id, endpoint_name, run_id, source_type,
                  ticker, window_start, window_end, provider_profile_version
           FROM source_checkpoints
           WHERE run_id=? AND cell_id=? AND endpoint_name='sec_document'""",
        (run_id, expected_cell["cell_id"]),
    ).fetchall()
    if len(cp_rows) != 1:
        if not cp_rows:
            return False, "missing_checkpoint"
        return False, "duplicate_checkpoint"
    cp = cp_rows[0]
    if cp[0] != "success" or int(cp[1]) != 1:
        return False, "checkpoint_not_terminal_success"
    if cp[3] != expected_lfid:
        return False, "logical_fetch_id_mismatch"
    if not cp[4] or cp[4] != raw_id:
        return False, "checkpoint_raw_mismatch"
    if (
        cp[5] != expected_cell["cell_id"]
        or cp[6] != "sec_document"
        or cp[7] != run_id
        or cp[8] != "sec_filings"
        or cp[9] != expected_cell["subject"]
        or cp[10] != expected_cell["window_start"]
        or cp[11] != expected_cell["window_end"]
        or cp[12] != expected_cell["provider_profile_version"]
    ):
        return False, "checkpoint_cell_identity_mismatch"
    att_count = conn.execute(
        """SELECT COUNT(*) FROM provider_request_attempts
           WHERE run_id=? AND logical_fetch_id=? AND endpoint_name='sec_document'""",
        (run_id, expected_lfid),
    ).fetchone()[0]
    if int(cp[2] or 0) != int(att_count) or int(att_count) < 1:
        return False, "request_count_mismatch"
    return True, None


def evaluate_sec_readiness(
    conn: sqlite3.Connection,
    *,
    mandatory_document_ids: Sequence[str],
    optional_degraded_ids: Sequence[str] = (),
    missing_carry_in_slots: Sequence[str] | None = None,
    submissions_complete: bool | None = None,
    index_complete: bool | None = None,
    corpus_manifest_id: str | None = None,
    s4_lineage_run_ids: Sequence[str] | None = None,
    s1_lineage_run_ids: Sequence[str] | None = None,
    s2_lineage_run_ids: Sequence[str] | None = None,
    inventory_id: str | None = None,
    inventory: Mapping[str, Any] | None = None,
    require_nonempty_mandatory: bool = True,
    s1_tickers: Sequence[str] | None = None,
    s2_index_cell_ids: Sequence[str] | None = None,
    enforce_checkpoint_oracle: bool = True,
) -> SecReadinessReport:
    """Per-document SEC readiness (source before evidence).

    Production Pre-B6 must pass lineages and frozen inventory; boolean
    submissions_complete/index_complete defaults are None (not assumed True).
    """
    mandatory = list(mandatory_document_ids)
    # Carry-in only from frozen inventory when provided
    if inventory is not None:
        carry = list(inventory.get("missing_carry_in_slots") or [])
        inventory_id = str(inventory.get("inventory_id") or inventory_id or "")
    elif missing_carry_in_slots is not None:
        carry = list(missing_carry_in_slots)
    else:
        carry = []

    if require_nonempty_mandatory and len(mandatory) == 0:
        return SecReadinessReport(
            sec_source_ready=False,
            sec_evidence_ready=False,
            expected_mandatory_count=0,
            fetched_count=0,
            extracted_count=0,
            provenance_valid_count=0,
            chunked_count=0,
            optional_degraded_ids=list(optional_degraded_ids),
            missing_carry_in_slots=carry,
            inventory_id=inventory_id,
        )

    # S1 / S2 completion: never default True in production
    s1_ok = False
    s2_ok = False
    if submissions_complete is not None:
        s1_ok = bool(submissions_complete)
    elif s1_lineage_run_ids is not None and s1_tickers is not None:
        s1_ok = verify_s1_complete_40(
            conn, lineage_run_ids=s1_lineage_run_ids, tickers=s1_tickers
        )
    elif s1_lineage_run_ids is not None:
        # without ticker list cannot claim 40/40
        s1_ok = False

    if index_complete is not None:
        s2_ok = bool(index_complete)
    elif s2_lineage_run_ids is not None and s2_index_cell_ids is not None:
        s2_ok = verify_s2_index_complete(
            conn,
            lineage_run_ids=s2_lineage_run_ids,
            index_cell_ids=s2_index_cell_ids,
        )
    elif s2_lineage_run_ids is not None:
        s2_ok = False

    missing: list[str] = []
    failed: list[str] = []
    checkpoint_failed: list[str] = []
    extracted = 0
    provenance_valid = 0
    chunked = 0
    fetched = 0

    descriptors = (
        _document_cells_from_inventory(inventory) if inventory is not None else {}
    )
    s4_lineage = list(s4_lineage_run_ids or [])

    for doc_id in mandatory:
        fd = conn.execute(
            """SELECT extraction_status, text, document_id FROM filing_documents
               WHERE document_id=?""",
            (doc_id,),
        ).fetchone()
        if fd is None:
            missing.append(doc_id)
            continue
        fetched += 1
        if fd[0] != "success" or not _doc_text_ok(fd[1]):
            failed.append(doc_id)
            continue
        extracted += 1

        if enforce_checkpoint_oracle and s4_lineage:
            ok, reason = verify_mandatory_document_checkpoint(
                conn,
                document_id=doc_id,
                s4_lineage_run_ids=s4_lineage,
                inventory_id=inventory_id or "",
                document_descriptor=descriptors.get(doc_id),
            )
            if not ok:
                failed.append(doc_id)
                checkpoint_failed.append(doc_id)
                continue
            provenance_valid += 1
        else:
            # Non-production unit path: provenance/lineage only. Production
            # Pre-B6 always sets enforce_checkpoint_oracle=True.
            if s4_lineage:
                ph = _lineage_placeholders(s4_lineage)
                basic = conn.execute(
                    f"""SELECT COUNT(*)
                        FROM normalized_provenance np
                        JOIN raw_assets ra ON ra.asset_id=np.raw_asset_id
                        JOIN provider_request_attempts pa
                          ON pa.request_id=ra.request_id
                        WHERE np.entity_type='filing' AND np.entity_id=?
                          AND pa.run_id IN ({ph})
                          AND pa.endpoint_name='sec_document'
                          AND pa.status='SUCCEEDED'""",
                    [doc_id, *s4_lineage],
                ).fetchone()[0]
                if int(basic) != 1:
                    failed.append(doc_id)
                    continue
                provenance_valid += 1
            else:
                # Without S4 lineage production cannot pass
                failed.append(doc_id)
                continue

        chunks_relation = served_chunks_relation(conn)
        has_chunks = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name=?", (chunks_relation,)
        ).fetchone()
        if has_chunks:
            q = f"""
                SELECT COUNT(*) FROM {chunks_relation}
                WHERE document_id=? AND chunk_profile_version='filing_v3'
                  AND eligibility='eligible'
                  AND status IN ({",".join("?" for _ in SEARCHABLE)})
            """
            params: list[Any] = [doc_id, *SEARCHABLE]
            if corpus_manifest_id:
                q += " AND manifest_id=?"
                params.append(corpus_manifest_id)
            if conn.execute(q, params).fetchone()[0] >= 1:
                chunked += 1

    source_ok = (
        s1_ok
        and s2_ok
        and not missing
        and not failed
        and not carry
        and len(mandatory) > 0
        and extracted == len(mandatory)
        and provenance_valid == len(mandatory)
        and fetched == len(mandatory)
    )
    evidence_ok = source_ok and chunked == len(mandatory) and len(mandatory) > 0

    return SecReadinessReport(
        sec_source_ready=source_ok,
        sec_evidence_ready=evidence_ok,
        expected_mandatory_count=len(mandatory),
        fetched_count=fetched,
        extracted_count=extracted,
        provenance_valid_count=provenance_valid,
        chunked_count=chunked,
        missing_document_ids=missing,
        failed_document_ids=failed,
        optional_degraded_ids=list(optional_degraded_ids),
        missing_carry_in_slots=carry,
        inventory_id=inventory_id,
        s1_complete=s1_ok,
        s2_complete=s2_ok,
        s1_lineage_run_ids=list(s1_lineage_run_ids or []),
        s2_lineage_run_ids=list(s2_lineage_run_ids or []),
        s4_lineage_run_ids=s4_lineage,
        checkpoint_failed_document_ids=checkpoint_failed,
    )


def load_and_verify_convergence_evidence(
    path: str | Path,
    *,
    expected_inventory_id: str | None = None,
    expected_universe_manifest_id: str | None = None,
    expected_baseline_snapshot_id: str | None = None,
    expected_db_user_version: int | None = None,
    expected_readiness_policy_version: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Always recompute convergence_plan_hash; never trust stored hash alone."""
    p = Path(path)
    if not p.is_file():
        raise SecReadinessError(f"convergence evidence missing: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SecReadinessError("convergence evidence must be object")
    required = (
        "baseline_snapshot_id",
        "universe_manifest_id",
        "s1_plan_hash",
        "s2_plan_hash",
        "inventory_id",
        "s4_plan_hash",
        "reconciliation_evidence_hash",
        "db_user_version",
        "readiness_policy_version",
    )
    missing = [k for k in required if k not in data]
    if missing:
        raise SecReadinessError(f"convergence evidence missing keys: {missing}")
    for key in (
        "baseline_snapshot_id",
        "universe_manifest_id",
        "s1_plan_hash",
        "s2_plan_hash",
        "inventory_id",
        "s4_plan_hash",
        "reconciliation_evidence_hash",
    ):
        require_hex64(str(data[key]), label=key)
    if expected_inventory_id is not None:
        require_hex64(expected_inventory_id, label="expected_inventory_id")
        if data["inventory_id"] != expected_inventory_id:
            raise SecReadinessError("convergence evidence inventory_id mismatch")
    for expected, key in (
        (expected_universe_manifest_id, "universe_manifest_id"),
        (expected_baseline_snapshot_id, "baseline_snapshot_id"),
    ):
        if expected is not None:
            require_hex64(expected, label=f"expected_{key}")
            if data[key] != expected:
                raise SecReadinessError(f"convergence evidence {key} mismatch")
    if (
        expected_db_user_version is not None
        and int(data["db_user_version"]) != int(expected_db_user_version)
    ):
        raise SecReadinessError("convergence evidence db_user_version mismatch")
    if (
        expected_readiness_policy_version is not None
        and data["readiness_policy_version"] != expected_readiness_policy_version
    ):
        raise SecReadinessError(
            "convergence evidence readiness_policy_version mismatch"
        )
    from catalyst_data.sec.convergence_identity import compute_convergence_plan_hash

    recomputed = compute_convergence_plan_hash(
        baseline_snapshot_id=str(data["baseline_snapshot_id"]),
        universe_manifest_id=str(data["universe_manifest_id"]),
        s1_plan_hash=str(data["s1_plan_hash"]),
        s2_plan_hash=str(data["s2_plan_hash"]),
        inventory_id=str(data["inventory_id"]),
        s4_plan_hash=str(data["s4_plan_hash"]),
        reconciliation_evidence_hash=str(data["reconciliation_evidence_hash"]),
        db_user_version=int(data["db_user_version"]),
        readiness_policy_version=str(data["readiness_policy_version"]),
    )
    require_hex64(recomputed, label="recomputed_convergence_plan_hash")
    stored = data.get("convergence_plan_hash")
    if stored is not None:
        require_hex64(str(stored), label="stored_convergence_plan_hash")
        if str(stored) != recomputed:
            raise SecReadinessError(
                "forged or stale stored convergence_plan_hash "
                f"(stored={str(stored)[:12]}… recomputed={recomputed[:12]}…)"
            )
    return recomputed, data
