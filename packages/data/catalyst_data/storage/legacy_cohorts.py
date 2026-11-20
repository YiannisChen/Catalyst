"""Validate explicit legacy membership without classifying or migrating a corpus.

The operator supplies the pinned membership and original rules. Validation proves
their identity and structure, not that a declared role matches an old policy or
that source rows have been reconciled; the migration must check those separately.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Sequence

from catalyst_data.ingestion import input_manifest
from catalyst_data.ingestion.time import timefields
from catalyst_data.storage.snapshots import VerifiedStores, validate_verified_stores


class CohortError(ValueError):
    """Invalid or inconsistent pinned legacy membership."""


def _fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        raise CohortError("invalid legacy manifest fields")


def _identifier(value):
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > 512 or any(ord(char) < 32 for char in value)):
        raise CohortError("legacy identity must be nonempty and trimmed")
    return value


def _digest(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise CohortError("invalid legacy SHA-256")
    return value


def _stamp(value):
    try:
        return timefields.stored(value).isoformat(timespec="microseconds")
    except (ValueError, TypeError, OverflowError):
        raise CohortError("legacy timestamps must include a time zone") from None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CohortError("duplicate legacy JSON field")
        result[key] = value
    return result


def _json(raw):
    try:
        return json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError):
        raise CohortError("invalid legacy JSON") from None


def _corrections(bindings):
    if not isinstance(bindings, (list, tuple)):
        raise CohortError("correction bindings must be explicit")
    result = []
    identities = set()
    for row in bindings:
        if isinstance(row, Mapping):
            row = dict(row)
        _fields(row, {"table_id", "sha256", "store_id"})
        entry = (_identifier(row["table_id"]), _digest(row["sha256"]), _identifier(row["store_id"]))
        identity = (entry[0], entry[2])
        if identity in identities:
            raise CohortError("duplicate correction binding")
        identities.add(identity)
        result.append(entry)
    return tuple(sorted(result))


@dataclass(frozen=True)
class LegacyRecord:
    source_id: str
    document_id: str
    evidence_id: str
    kind: str
    tickers: tuple[str, ...]
    evidence_role: str
    eligible_at: str
    representation: str
    text_sha256: str
    title_sha256: str
    asset_id: str | None = None
    chunk_id: str | None = None
    ordinal: int | None = None
    original_body_sha256: str | None = None


@dataclass(frozen=True)
class LegacyCohort:
    cohort_id: str
    manifest_sha256: str
    input_manifest_sha256: str
    source_store_id: str
    source_snapshot_sha256: str
    snapshot_captured_at: str
    source_tables: tuple[str, ...]
    eligibility_rule_id: str
    eligibility_rule_version: str
    membership_sha256: str
    records: tuple[LegacyRecord, ...]
    correction_bindings: tuple[tuple[str, str, str], ...]
    _index: Mapping[tuple[str, str], LegacyRecord] = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        index = {(record.source_id, record.document_id): record for record in self.records}
        if len(index) != len(self.records):
            raise CohortError("duplicate legacy record identity")
        object.__setattr__(self, "_index", MappingProxyType(index))

    def record(self, source_id: str, document_id: str) -> LegacyRecord:
        try:
            return self._index[(source_id, document_id)]
        except KeyError:
            raise CohortError("record is outside legacy membership") from None

    def require_corrections(self, bindings: Sequence[Mapping]) -> None:
        if _corrections(bindings) != self.correction_bindings:
            raise CohortError("legacy correction bindings differ from the pinned cohort")


def _record(row):
    required = {"source_id", "document_id", "evidence_id", "kind", "tickers", "evidence_role",
                "eligible_at", "representation", "text_sha256", "title_sha256"}
    chunk_fields = {"asset_id", "chunk_id", "ordinal", "original_body_sha256"}
    _fields(row, required, chunk_fields)
    if not isinstance(row["kind"], str) or row["kind"] not in {"body", "filing"}:
        raise CohortError("legacy body membership requires body or filing kind")
    if not isinstance(row["evidence_role"], str) or row["evidence_role"] not in {"evidence", "lead"}:
        raise CohortError("legacy evidence role must be explicit")
    tickers = row["tickers"]
    if (not isinstance(tickers, list) or not tickers
            or any(not isinstance(ticker, str) or ticker != ticker.strip() or not ticker for ticker in tickers)
            or len(set(tickers)) != len(tickers)):
        raise CohortError("legacy ticker membership must be explicit and unique")
    representation = row["representation"]
    if representation == "legacy_chunk":
        if not chunk_fields <= set(row):
            raise CohortError("legacy chunk identity is incomplete")
        _identifier(row["asset_id"])
        _identifier(row["chunk_id"])
        if type(row["ordinal"]) is not int or row["ordinal"] < 0:
            raise CohortError("legacy chunk ordinal must be a nonnegative integer")
        _digest(row["original_body_sha256"])
    elif representation == "raw_full_body":
        if chunk_fields.intersection(row):
            raise CohortError("raw full bodies cannot carry chunk identity")
    else:
        raise CohortError("legacy representation must preserve raw full body or original chunk")
    return LegacyRecord(
        **{name: _identifier(row[name]) for name in ("source_id", "document_id", "evidence_id")},
        kind=row["kind"], tickers=tuple(tickers), evidence_role=row["evidence_role"],
        eligible_at=_stamp(row["eligible_at"]), representation=representation,
        text_sha256=_digest(row["text_sha256"]), title_sha256=_digest(row["title_sha256"]),
        **{name: row[name] for name in chunk_fields if name in row},
    )


def _artifact(path, hashes):
    if not isinstance(path, (str, Path)):
        raise CohortError("legacy artifact path must be a string or Path")
    candidate = Path(path).expanduser()
    if not candidate.is_absolute() or candidate.is_symlink():
        raise CohortError("legacy artifact must be an absolute non-symlink path")
    candidate = candidate.resolve()
    if candidate not in hashes:
        raise CohortError("legacy artifact is outside pinned input membership")
    return candidate, hashes[candidate]


def load_cohort(path: str | Path, stores: VerifiedStores) -> LegacyCohort:
    """Load metadata from explicitly pinned artifacts; never infer a legacy set."""
    validate_verified_stores(stores)
    receipt = input_manifest.verify(
        stores.manifest_path, invocation=list(stores.invocation),
        required_env=stores.required_env, environ=stores.environ_override,
    )
    hashes = {Path(entry["path"]): entry["sha256"] for entry in receipt["files"]}
    catalog_path, catalog_sha = _artifact(path, hashes)
    spec = _json(catalog_path.read_bytes())
    _fields(spec, {"schema_version", "cohort_id", "source_snapshot", "eligibility_rule",
                   "correction_tables", "membership"})
    if type(spec["schema_version"]) is not int or spec["schema_version"] != 1:
        raise CohortError("unsupported legacy manifest schema")
    source, rule, membership = spec["source_snapshot"], spec["eligibility_rule"], spec["membership"]
    _fields(source, {"store_id", "sha256", "captured_at", "tables"})
    _fields(rule, {"id", "version"})
    _fields(membership, {"path", "sha256", "record_count"})
    store_id = _identifier(source["store_id"])
    pin = stores.stores.get(store_id)
    if pin is None or _digest(source["sha256"]) != pin.sha256:
        raise CohortError("legacy snapshot does not match the verified source store")
    tables = source["tables"]
    if (not isinstance(tables, list) or not tables
            or any(not isinstance(table, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table) for table in tables)
            or len(set(tables)) != len(tables)):
        raise CohortError("source tables must be explicit and unique")
    members_path, members_sha = _artifact(membership["path"], hashes)
    if _digest(membership["sha256"]) != members_sha:
        raise CohortError("legacy membership checksum differs from the input manifest")
    count = membership["record_count"]
    if type(count) is not int or count < 0:
        raise CohortError("legacy membership count must be nonnegative")
    records, identities, chunks, chunk_ids = [], set(), set(), set()
    with members_path.open("rb") as stream:
        for line in stream:
            record = _record(_json(line))
            identity = (record.source_id, record.document_id)
            if identity in identities:
                raise CohortError("duplicate legacy record identity")
            identities.add(identity)
            if record.representation == "legacy_chunk":
                chunk_identity = (record.asset_id, record.ordinal)
                if chunk_identity in chunks or record.chunk_id in chunk_ids:
                    raise CohortError("duplicate legacy chunk order identity")
                chunks.add(chunk_identity)
                chunk_ids.add(record.chunk_id)
            records.append(record)
    if len(records) != count:
        raise CohortError("legacy membership count does not match enumerated records")
    cohort = LegacyCohort(
        cohort_id=_identifier(spec["cohort_id"]), manifest_sha256=catalog_sha,
        input_manifest_sha256=stores.manifest_sha256, source_store_id=store_id,
        source_snapshot_sha256=pin.sha256, snapshot_captured_at=_stamp(source["captured_at"]),
        source_tables=tuple(tables), eligibility_rule_id=_identifier(rule["id"]),
        eligibility_rule_version=_identifier(rule["version"]), membership_sha256=members_sha,
        records=tuple(records), correction_bindings=_corrections(spec["correction_tables"]),
    )
    validate_verified_stores(stores)
    return cohort
