"""FilingInventoryManifest builder and requiredness policy."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from catalyst_data.manifests.universe import (
    RATIFIED_TICKERS,
    load_universe_spec,
    sha256_identity,
)

CANONICAL_START = "2025-08-01"
CANONICAL_END = "2026-07-23"
CARRY_IN_FLOOR = "2024-01-01"
CARRY_IN_RULE_VERSION = "v1"
FORM_POLICY_VERSION = "v1"
DOCUMENT_SELECTION_POLICY_VERSION = "v1"
SCHEMA_VERSION = "1.0.0"

US_EVENT = frozenset({"8-K", "8-K/A"})
US_PERIODIC_10Q = frozenset({"10-Q", "10-Q/A"})
US_PERIODIC_10K = frozenset({"10-K", "10-K/A"})
FPI_EVENT = frozenset({"6-K"})
FPI_PERIODIC_20F = frozenset({"20-F", "20-F/A"})


def requiredness_for_descriptor(
    *,
    is_primary: bool,
    content_extension: str,
    form_type: str,
    document_type: str,
) -> tuple[str, str]:
    """Return (requiredness, requiredness_reason) frozen at S3."""
    ext = (content_extension or "").lower().lstrip(".")
    if is_primary:
        if ext == "pdf":
            return "mandatory", "primary_pdf"  # still mandatory; S4 marks mandatory_failed
        return "mandatory", "primary"
    # exhibits
    is_ex99 = "EX-99" in (document_type or "").upper() or "ex-99" in (
        document_type or ""
    ).lower()
    if not is_ex99:
        return "mandatory", "non_ex99_secondary"
    if ext in {"html", "htm", "txt"}:
        return "mandatory", f"ex99_{ext}"
    if ext == "pdf":
        return "optional_degraded", "ex99_pdf"
    return "mandatory", "ex99_unknown_ext"


def _sort_documents(docs: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    primary = [dict(d) for d in docs if d.get("is_primary")]
    rest = [dict(d) for d in docs if not d.get("is_primary")]

    def rest_key(d: Mapping[str, Any]) -> tuple:
        seq = d.get("sequence")
        if seq is None:
            return (1, 10**9, str(d.get("document_type") or ""), str(d.get("filename") or d.get("document_file") or ""), str(d.get("document_url") or ""))
        return (0, int(seq), str(d.get("document_type") or ""), str(d.get("filename") or d.get("document_file") or ""), str(d.get("document_url") or ""))

    rest_sorted = sorted(rest, key=rest_key)
    ordered = primary + rest_sorted
    # Assign EX-99 roles
    ex_n = 0
    for d in ordered:
        dtype = str(d.get("document_type") or "").upper()
        if d.get("is_primary"):
            d["document_role"] = "primary"
        elif "EX-99" in dtype:
            ex_n += 1
            d["document_role"] = f"exhibit_99_{ex_n}"
        else:
            d["document_role"] = str(d.get("document_type") or "other").lower().replace(
                "-", "_"
            )
        req, reason = requiredness_for_descriptor(
            is_primary=bool(d.get("is_primary")),
            content_extension=str(d.get("content_extension") or ""),
            form_type=str(d.get("form_type") or ""),
            document_type=str(d.get("document_type") or ""),
        )
        d["requiredness"] = req
        d["requiredness_reason"] = reason
    return ordered


def compute_inventory_id(manifest_body: Mapping[str, Any]) -> str:
    # ── Schema validation ──
    entries = manifest_body.get("sorted_filing_entries") or []
    for i, entry in enumerate(entries):
        docs = entry.get("documents") or []
        has_top_level_doc = bool(
            entry.get("document_type") and entry.get("document_url")
        )
        if has_top_level_doc and not docs:
            raise ValueError(
                f"sorted_filing_entries[{i}]: carries top-level document_type "
                f"/document_url but documents=[] — schema violation"
            )
    identity = {
        "schema_version": manifest_body["schema_version"],
        "source_snapshot_id": manifest_body["source_snapshot_id"],
        "universe_manifest_id": manifest_body["universe_manifest_id"],
        "canonical_start": manifest_body["canonical_start"],
        "canonical_end": manifest_body["canonical_end"],
        "carry_in_rule_version": manifest_body["carry_in_rule_version"],
        "form_policy_version": manifest_body["form_policy_version"],
        "document_selection_policy_version": manifest_body[
            "document_selection_policy_version"
        ],
        "sorted_filing_entries": manifest_body["sorted_filing_entries"],
        "universe_tickers": list(manifest_body["universe_tickers"]),
        "issuer_class_by_ticker": dict(manifest_body["issuer_class_by_ticker"]),
        # Deterministic carry-in gaps enter inventory identity (design D.1 / C)
        "missing_carry_in_slots": list(
            manifest_body.get("missing_carry_in_slots") or []
        ),
    }
    if "document_plan_hash" in identity or "plan_hash" in manifest_body:
        pass
    return sha256_identity(identity)


def compute_missing_carry_in_slots(
    *,
    tickers: Sequence[str],
    filing_entries: Sequence[Mapping[str, Any]],
    issuer_class_by_ticker: Mapping[str, str] | None = None,
) -> list[str]:
    """Mandatory periodic carry-in slots missing for the 40-ticker universe.

    Slot identity: ``{ticker}:{form_class}`` for each required periodic class.
    US issuers require 10-Q and 10-K carry-in coverage when no in-window filing;
    FPI require 20-F. Event forms (8-K/6-K) do not create carry-in slots.
    """
    issuer_map = dict(issuer_class_by_ticker or {})
    # Group filings by ticker
    by_ticker: dict[str, list[Mapping[str, Any]]] = {}
    for e in filing_entries:
        t = str(e.get("ticker") or "")
        by_ticker.setdefault(t, []).append(e)

    missing: list[str] = []
    for ticker in sorted(tickers):
        klass = issuer_map.get(ticker, "")
        filings = by_ticker.get(ticker, [])
        if klass.lower() in {"fpi", "foreign_private_issuer"}:
            classes = [("20-F", FPI_PERIODIC_20F)]
        else:
            classes = [("10-Q", US_PERIODIC_10Q), ("10-K", US_PERIODIC_10K)]
        for label, form_set in classes:
            # In-window periodic present?
            in_window = False
            for f in filings:
                form = str(f.get("form_type") or "")
                fd = str(f.get("filed_date") or f.get("filed_at") or "")[:10]
                if form in form_set and CANONICAL_START <= fd <= CANONICAL_END:
                    in_window = True
                    break
            if in_window:
                continue
            # Carry-in available?
            carried = select_carry_in(filings, form_class=form_set)
            if carried is None:
                missing.append(f"{ticker}:{label}")
    return sorted(missing)


def build_filing_inventory_manifest(
    *,
    source_snapshot_id: str,
    universe_manifest_id: str,
    filing_entries: Iterable[Mapping[str, Any]],
    tickers: Sequence[str],
    issuer_class_by_ticker: Mapping[str, str],
) -> dict[str, Any]:
    ratified = list(RATIFIED_TICKERS)
    if list(tickers) != ratified:
        raise ValueError("filing inventory requires exact ordered ratified 40 universe")
    spec_path = (
        Path(__file__).resolve().parents[1]
        / "manifests"
        / "universe_v1_2025_08.spec.json"
    )
    spec = load_universe_spec(spec_path)
    expected_issuer_classes = {
        ticker: str(spec.companies[ticker]["issuer_class"])
        for ticker in spec.tickers
    }
    supplied_issuer_classes = {
        str(ticker): str(issuer_class)
        for ticker, issuer_class in issuer_class_by_ticker.items()
    }
    if supplied_issuer_classes != expected_issuer_classes:
        raise ValueError("filing inventory issuer class map differs from ratified 40 contract")
    entries: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    for raw in filing_entries:
        entry = dict(raw)
        docs = list(entry.get("documents") or [])

        # ── Schema migration: promote top-level document fields into documents[]
        #    when the entry carries document identity at entry level (legacy flat shape).
        has_top_level_doc = bool(
            entry.get("document_type")
            and entry.get("document_url")
        )
        if has_top_level_doc and not docs:
            # Determine if this is the primary document by matching against
            # the filings.primary_document field.
            prim_doc = (
                entry.get("primary_document")
                or entry.get("document_file")
                or ""
            )
            doc_file = str(entry.get("document_file") or "")
            doc_url_end = Path(entry.get("document_url", "")).name if "document_url" in entry else ""
            is_primary = bool(
                prim_doc
                and doc_file
                and (
                    doc_file == prim_doc
                    or doc_url_end == prim_doc
                )
            )
            dd = {
                "is_primary": is_primary,
                "document_type": str(entry["document_type"]),
                "document_url": str(entry["document_url"]),
                "document_file": str(entry.get("document_file") or entry.get("primary_document") or ""),
                "content_extension": str(entry.get("content_extension") or ""),
                "filing_id": str(entry.get("filing_id") or entry.get("accession_number") or ""),
                "form_type": str(entry.get("form_type") or ""),
            }
            docs = [dd]
            for k in ("document_type", "document_url", "document_file",
                       "document_role", "requiredness", "requiredness_reason"):
                entry.pop(k, None)
        elif has_top_level_doc and docs:
            raise ValueError(
                "filing entry carries both top-level document fields AND "
                "documents[] — ambiguous schema"
            )
        elif not has_top_level_doc and not docs:
            pass

        docs = _sort_documents(docs)
        # Compute deterministic document_id for each document
        from catalyst_data.sec.document_cells import compute_document_id

        acc = str(entry.get("accession_number") or "")
        for doc in docs:
            role = str(doc.get("document_role") or "primary")
            dfile = str(doc.get("document_file") or "")
            durl = str(doc.get("document_url") or "")
            fid = str(doc.get("filing_id") or entry.get("filing_id") or entry.get("accession_number") or "")
            doc["document_id"] = compute_document_id(
                filing_id=fid,
                accession_number=acc,
                document_role=role,
                document_file=dfile,
                document_url=durl,
            )
        entry["documents"] = docs
        entries.append(entry)

    entries.sort(
        key=lambda e: (
            str(e.get("ticker") or ""),
            str(e.get("accession_number") or ""),
            str(e.get("filed_at") or ""),
        )
    )
    carry = compute_missing_carry_in_slots(
        tickers=ratified,
        filing_entries=entries,
        issuer_class_by_ticker=supplied_issuer_classes,
    )
    body = {
        "schema_version": SCHEMA_VERSION,
        "source_snapshot_id": source_snapshot_id,
        "universe_manifest_id": universe_manifest_id,
        "canonical_start": CANONICAL_START,
        "canonical_end": CANONICAL_END,
        "carry_in_rule_version": CARRY_IN_RULE_VERSION,
        "form_policy_version": FORM_POLICY_VERSION,
        "document_selection_policy_version": DOCUMENT_SELECTION_POLICY_VERSION,
        "sorted_filing_entries": entries,
        "universe_tickers": ratified,
        "issuer_class_by_ticker": supplied_issuer_classes,
        "missing_carry_in_slots": carry,
    }
    inventory_id = compute_inventory_id(body)
    return {**body, "inventory_id": inventory_id}


def select_carry_in(
    filings: Sequence[Mapping[str, Any]],
    *,
    form_class: frozenset[str],
) -> Mapping[str, Any] | None:
    """Most recent filing in class with floor <= filed_date < canonical_start."""
    candidates = []
    for f in filings:
        form = str(f.get("form_type") or "")
        fd = str(f.get("filed_date") or f.get("filed_at") or "")[:10]
        if form not in form_class:
            continue
        if CARRY_IN_FLOOR <= fd < CANONICAL_START:
            candidates.append((fd, f))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]
