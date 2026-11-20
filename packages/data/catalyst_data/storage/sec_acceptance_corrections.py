"""Read scoped SEC acceptance-time corrections from a pinned CSV byte string."""
from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePath


def _identifier(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field} must be a nonempty identifier")
    return value


def _instant(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("time must be an aware ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("time must be an aware ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("time must be an aware ISO timestamp")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class SecAcceptanceDecision:
    status: str
    effective_at: str | None
    table_id: str


@dataclass(frozen=True)
class SecAcceptanceCorrectionTable:
    table_id: str
    entries: tuple[tuple[str, tuple[str, ...], str | None, str], ...]

    def apply(self, current_at: str, *, store: str, accession: str) -> SecAcceptanceDecision:
        current = _instant(current_at)
        store = _identifier(store, "store")
        accession = _identifier(accession, "accession")
        matching = [
            entry for entry in self.entries
            if entry[0] == accession and store in entry[1]
        ]
        if matching:
            _, _, _, corrected_at = matching[0]
            status = "already_corrected" if current == corrected_at else "corrected"
            return SecAcceptanceDecision(status, corrected_at, self.table_id)
        status = (
            "not_applicable"
            if any(entry[0] == accession for entry in self.entries)
            else "unmatched"
        )
        return SecAcceptanceDecision(status, current, self.table_id)


def load_sec_acceptance_corrections(
    csv_bytes: bytes,
    *,
    filename: str,
    expected_sha256: str,
) -> SecAcceptanceCorrectionTable:
    """Validate and load a store-scoped SEC acceptance correction table."""
    if not isinstance(csv_bytes, bytes):
        raise ValueError("correction input must be CSV bytes")
    digest = hashlib.sha256(csv_bytes).hexdigest()
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
        raise ValueError("expected SHA256 must contain 64 hex digits")
    if digest != expected_sha256.lower():
        raise ValueError("correction CSV SHA256 mismatch")
    _identifier(filename, "filename")
    if PurePath(filename).name != filename or not filename.endswith(".csv"):
        raise ValueError("filename must be a CSV basename")

    required = {
        "accession", "json_acceptance_utc", "edgar_accepted_utc", "in_stores",
    }
    try:
        reader = csv.DictReader(io.StringIO(csv_bytes.decode("utf-8-sig")), strict=True)
    except UnicodeError as exc:
        raise ValueError("correction CSV must be valid UTF-8") from exc
    if (
        not reader.fieldnames
        or len(set(reader.fieldnames)) != len(reader.fieldnames)
        or not required <= set(reader.fieldnames)
    ):
        raise ValueError("SEC correction CSV schema mismatch")

    entries: list[tuple[str, tuple[str, ...], str | None, str]] = []
    scoped: dict[tuple[str, str], tuple[str | None, str]] = {}
    try:
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError("malformed correction CSV row")
            accession = _identifier(row["accession"], "accession")
            corrected_at = _instant(row["edgar_accepted_utc"])
            stored_at = (
                _instant(row["json_acceptance_utc"])
                if row["json_acceptance_utc"]
                else None
            )
            stores = tuple(sorted({
                token.strip() for token in row["in_stores"].split(";") if token.strip()
            }))
            entry = (accession, stores, stored_at, corrected_at)
            for store in stores:
                key = (accession, store)
                previous = scoped.get(key)
                identity = (stored_at, corrected_at)
                if previous is not None and previous != identity:
                    raise ValueError("conflicting scoped correction identity")
                scoped[key] = identity
            entries.append(entry)
    except csv.Error as exc:
        raise ValueError("SEC correction CSV syntax is invalid") from exc

    table_id = f"{PurePath(filename).stem}:{digest}"
    return SecAcceptanceCorrectionTable(table_id, tuple(entries))
