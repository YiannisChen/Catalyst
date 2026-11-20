"""Pure decisions from SHA-pinned correction CSV bytes; never rewrite a store.

Store scopes are exact CSV tokens, not a choice of legacy or evaluation cohort.
Callers remain responsible for input manifests and any eventual read integration.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePath

from catalyst_data.dedup.links import normalize_identity_url


def _identifier(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f'{field} must be a nonempty identifier')
    return value


def _instant(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError('time must be an aware ISO timestamp')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError('time must be an aware ISO timestamp') from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('time must be an aware ISO timestamp')
    return parsed.astimezone(timezone.utc).isoformat(timespec='microseconds')


@dataclass(frozen=True)
class Correction:
    key: tuple[str, ...]
    stores: tuple[str, ...]
    stored_at: str | None
    corrected_at: str


@dataclass(frozen=True)
class CorrectionDecision:
    status: str
    current_at: str | None
    original_at: str | None
    effective_at: str | None
    stored_at: str | None
    corrected_at: str | None
    table_id: str
    sha256: str
    time_flags: tuple[str, ...]

    @property
    def original_missing(self) -> bool:
        """Whether an applied SEC correction lacks its recorded JSON instant."""
        return self.status in {'corrected', 'already_corrected'} and self.stored_at is None


@dataclass(frozen=True)
class CorrectionTable:
    kind: str
    table_id: str
    sha256: str
    entries: tuple[Correction, ...]

    def _key(self, *, url=None, publisher=None, accession=None):
        if self.kind == 'finnhub':
            if accession is not None:
                raise ValueError('Finnhub corrections require URL and publisher only')
            normalized = normalize_identity_url(url)
            if normalized is None or not isinstance(publisher, str) or not publisher.strip():
                raise ValueError('Finnhub corrections require URL and publisher')
            return normalized, publisher.strip().casefold()
        if url is not None or publisher is not None:
            raise ValueError('SEC corrections require accession only')
        return (_identifier(accession, 'accession'),)

    def lookup(self, *, store: str, url=None, publisher=None, accession=None) -> Correction | None:
        """Return the exact scoped correction, without inferring scope aliases."""
        store = _identifier(store, 'store')
        key = self._key(url=url, publisher=publisher, accession=accession)
        return next((row for row in self.entries if row.key == key and store in row.stores), None)

    def apply(self, current_at: str | None, *, store: str, url=None, publisher=None,
              accession=None) -> CorrectionDecision:
        """Correct only a matching stored instant, or acknowledge corrected replay.

        Finnhub conflicts remain untouched. SEC index acceptance is authoritative
        by accession even when its submissions JSON time was never recorded. No
        caller's input record or database is modified by any decision.
        """
        original = None if current_at is None and self.kind == 'sec' else _instant(current_at)
        row = self.lookup(store=store, url=url, publisher=publisher, accession=accession)
        if row is None:
            key = self._key(url=url, publisher=publisher, accession=accession)
            status = 'not_applicable' if any(r.key == key for r in self.entries) else 'unmatched'
            return CorrectionDecision(status, original, None, original, None, None,
                                      self.table_id, self.sha256, ())
        if original == row.corrected_at:
            status = 'already_corrected'
        elif self.kind == 'sec' or original == row.stored_at:
            status = 'corrected'
        else:
            return CorrectionDecision('conflict', original, row.stored_at, original, row.stored_at,
                                      row.corrected_at, self.table_id, self.sha256, ())
        return CorrectionDecision(status, original, row.stored_at, row.corrected_at, row.stored_at,
                                  row.corrected_at, self.table_id, self.sha256,
                                  ('corrected:' + self.table_id,))


def load_corrections(csv_bytes: bytes, *, kind: str, filename: str,
                     expected_sha256: str) -> CorrectionTable:
    """Validate immutable bytes and schema before exposing scoped lookup/apply.

    ``stores`` uses '+' in the Finnhub table; ``in_stores`` uses ';' in SEC.
    Empty scope never applies. Canonical duplicate keys with overlapping scopes
    must agree on both instants; ambiguity rejects the complete table.
    """
    if not isinstance(csv_bytes, bytes):
        raise ValueError('correction input must be CSV bytes')
    digest = hashlib.sha256(csv_bytes).hexdigest()
    if not isinstance(expected_sha256, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', expected_sha256):
        raise ValueError('expected SHA256 must contain 64 hex digits')
    if digest != expected_sha256.lower():
        raise ValueError('correction CSV SHA256 mismatch')
    _identifier(filename, 'filename')
    if PurePath(filename).name != filename or not filename.endswith('.csv'):
        raise ValueError('filename must be a CSV basename')
    if kind not in {'finnhub', 'sec'}:
        raise ValueError('kind must be finnhub or sec')
    table = CorrectionTable(kind, PurePath(filename).stem + ':' + digest, digest, ())
    try:
        reader = csv.DictReader(io.StringIO(csv_bytes.decode('utf-8-sig')), strict=True)
    except UnicodeError as exc:
        raise ValueError('correction CSV must be valid UTF-8') from exc
    required = ({'url', 'publisher', 'stored_eligible_at', 'corrected_at', 'stores'} if kind == 'finnhub'
                else {'accession', 'json_acceptance_utc', 'edgar_accepted_utc', 'in_stores'})
    if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames) or not required <= set(reader.fieldnames):
        raise ValueError('correction CSV schema mismatch')
    entries = []
    scoped = {}
    try:
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError('malformed correction CSV row')
            if kind == 'finnhub':
                key = table._key(url=row['url'], publisher=row['publisher'])
                stored, corrected = row['stored_eligible_at'], row['corrected_at']
                scope, separator = row['stores'], '+'
            else:
                key = table._key(accession=row['accession'])
                stored, corrected = row['json_acceptance_utc'], row['edgar_accepted_utc']
                scope, separator = row['in_stores'], ';'
            stores = tuple(sorted(set(token.strip() for token in scope.split(separator) if token.strip())))
            entry = Correction(key, stores, None if kind == 'sec' and not stored else _instant(stored),
                               _instant(corrected))
            for store in stores:
                identity = (key, store)
                previous = scoped.get(identity)
                if previous is not None and previous != (entry.stored_at, entry.corrected_at):
                    raise ValueError('conflicting scoped correction identity')
                scoped[identity] = (entry.stored_at, entry.corrected_at)
            entries.append(entry)
    except csv.Error as exc:
        raise ValueError('correction CSV syntax is invalid') from exc
    return CorrectionTable(kind, table.table_id, digest, tuple(entries))
