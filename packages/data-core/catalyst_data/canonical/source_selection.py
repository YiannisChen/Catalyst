"""Sealed general source-selection manifest (M8-A).

The M8-A recovery rebuild must be *class*-selected, never gold-ID selected.
This module owns the frozen contract for the manifest that is sealed before any
benchmark membership audit runs:

* ``selection_policy_id`` is ``general-public-fulltext-v1``;
* documents are addressed only by public identity (URL, source class, times,
  publisher/provider, tickers, hash-bound body);
* benchmark semantics (``case_id``, ``evidence_id``, ``gold``, ``role``,
  ``oracle_status``, ``label``, ``attribution``, expected-evidence fields) are
  rejected recursively anywhere in the payload;
* the sealed identity excludes ``generated_at`` and every local filesystem
  root, so re-sealing the same documents is byte-identical;
* every body below the caller-supplied ``body_root`` is re-hashed on load.

Loading is read-only: no SQLite access happens here.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = "v1_1_source_selection_v1"
SELECTION_POLICY_ID = "general-public-fulltext-v1"

SOURCE_CLASSES = frozenset(
    {
        "structured_market_data",
        "official_government",
        "issuer_disclosure",
        "corporate_press_release",
        "reported_news",
        "analysis_opinion",
        "aggregated_unknown",
    }
)

# Benchmark/gold semantics are never part of source selection.
FORBIDDEN_KEYS = frozenset(
    {
        "case_id",
        "evidence_id",
        "evidence_ids",
        "expected_primary_evidence",
        "expected_primary_evidence_ids",
        "expected_evidence",
        "evidence_judgments",
        "oracle_status",
        "attribution",
        "gold",
        "label",
        "labels",
        "role",
        "roles",
        "acceptable_cause_labels",
        "expected_refusal_reason",
        "expected_research_behavior",
    }
)

_MANIFEST_KEYS = frozenset(
    {"schema_version", "selection_policy_id", "documents", "generated_at"}
)
_DOCUMENT_KEYS = frozenset(
    {
        "canonical_url",
        "source_class",
        "source_published_at",
        "eligible_at",
        "fetched_at",
        "body_sha256",
        "body_path",
        "provider",
        "publisher",
        "tickers",
    }
)

_UTC_ISO_Z = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_TICKER = re.compile(r"[A-Z][A-Z0-9.\-]{0,9}\Z")


def canonical_json_bytes(payload: object) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


@dataclass(frozen=True)
class SelectedDocument:
    canonical_url: str
    source_class: str
    source_published_at: str
    eligible_at: str
    fetched_at: str
    body_sha256: str
    body_path: str
    provider: str
    publisher: str
    tickers: tuple[str, ...]

    def identity_payload(self) -> dict[str, Any]:
        return {
            "canonical_url": self.canonical_url,
            "source_class": self.source_class,
            "source_published_at": self.source_published_at,
            "eligible_at": self.eligible_at,
            "fetched_at": self.fetched_at,
            "body_sha256": self.body_sha256,
            "body_path": self.body_path,
            "provider": self.provider,
            "publisher": self.publisher,
            "tickers": list(self.tickers),
        }


@dataclass(frozen=True)
class SourceSelectionManifest:
    schema_version: str
    selection_policy_id: str
    documents: tuple[SelectedDocument, ...]
    source_selection_id: str
    generated_at: str | None = None


def _forbidden_paths(value: object, *, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            child = f"{path}.{key_text}" if path else key_text
            if key_text.lower() in FORBIDDEN_KEYS:
                found.append(child)
            found.extend(_forbidden_paths(item, path=child))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found.extend(_forbidden_paths(item, path=f"{path}[{index}]"))
    return found


def _reject_forbidden(payload: object) -> None:
    found = _forbidden_paths(payload)
    if found:
        raise ValueError(
            "source-selection manifest contains benchmark field(s): "
            + ", ".join(sorted(found)[:5])
        )


def _require_str(document: Mapping[str, Any], key: str, *, where: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"source-selection document {where} requires {key!r}")
    return value


def _validate_times(document: Mapping[str, Any], *, where: str) -> None:
    for key in ("source_published_at", "eligible_at", "fetched_at"):
        value = _require_str(document, key, where=where)
        if _UTC_ISO_Z.fullmatch(value) is None:
            raise ValueError(
                f"source-selection document {where} has non-UTC-ISO-Z {key}={value!r}"
            )


def _resolve_body(body_root: Path, body_path: str, *, where: str) -> Path:
    if not body_path or body_path.startswith("/") or "\\" in body_path:
        raise ValueError(
            f"source-selection document {where} body_path must be a relative POSIX path"
        )
    parts = Path(body_path).parts
    if any(part in {"..", ""} for part in parts):
        raise ValueError(
            f"source-selection document {where} body_path escapes the body root"
        )
    root = Path(body_root).resolve()
    candidate = (root / body_path).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(
            f"source-selection document {where} body_path escapes the body root"
        )
    if candidate.is_symlink():
        raise ValueError(
            f"source-selection document {where} body_path must not be a symlink"
        )
    if not candidate.is_file():
        raise ValueError(f"source-selection document {where} body is missing: {body_path}")
    return candidate


def _validate_document(
    document: object, *, index: int, body_root: Path
) -> SelectedDocument:
    where = f"[{index}]"
    if not isinstance(document, Mapping):
        raise ValueError(f"source-selection document {where} must be an object")
    unknown = sorted(set(document) - _DOCUMENT_KEYS)
    if unknown:
        raise ValueError(
            f"source-selection document {where} has unknown key(s): "
            + ", ".join(unknown)
        )
    canonical_url = _require_str(document, "canonical_url", where=where)
    if not canonical_url.startswith(("http://", "https://")):
        raise ValueError(
            f"source-selection document {where} canonical_url must be http(s)"
        )
    source_class = _require_str(document, "source_class", where=where)
    if source_class not in SOURCE_CLASSES:
        raise ValueError(
            f"source-selection document {where} has invalid source_class {source_class!r}"
        )
    _validate_times(document, where=where)
    body_sha256 = _require_str(document, "body_sha256", where=where)
    if _SHA256.fullmatch(body_sha256) is None:
        raise ValueError(
            f"source-selection document {where} body_sha256 must be lowercase SHA-256"
        )
    body_path = _require_str(document, "body_path", where=where)
    provider = _require_str(document, "provider", where=where)
    publisher = _require_str(document, "publisher", where=where)
    raw_tickers = document.get("tickers")
    if not isinstance(raw_tickers, (list, tuple)) or not raw_tickers:
        raise ValueError(f"source-selection document {where} requires non-empty tickers")
    tickers: list[str] = []
    for ticker in raw_tickers:
        if not isinstance(ticker, str) or _TICKER.fullmatch(ticker) is None:
            raise ValueError(
                f"source-selection document {where} has invalid ticker {ticker!r}"
            )
        if ticker in tickers:
            raise ValueError(
                f"source-selection document {where} has duplicate ticker {ticker!r}"
            )
        tickers.append(ticker)

    body_file = _resolve_body(body_root, body_path, where=where)
    digest = hashlib.sha256(body_file.read_bytes()).hexdigest()
    if digest != body_sha256:
        raise ValueError(
            f"source-selection document {where} body hash mismatch for {body_path}"
        )

    return SelectedDocument(
        canonical_url=canonical_url,
        source_class=source_class,
        source_published_at=document["source_published_at"],
        eligible_at=document["eligible_at"],
        fetched_at=document["fetched_at"],
        body_sha256=body_sha256,
        body_path=body_path,
        provider=provider,
        publisher=publisher,
        tickers=tuple(tickers),
    )


def _identity_payload(
    *, selection_policy_id: str, documents: Sequence[SelectedDocument]
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "selection_policy_id": selection_policy_id,
        "documents": [document.identity_payload() for document in documents],
    }


def compute_source_selection_id(
    *, selection_policy_id: str, documents: Sequence[SelectedDocument]
) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            _identity_payload(
                selection_policy_id=selection_policy_id, documents=documents
            )
        )
    ).hexdigest()


def source_selection_id(
    payload: Mapping[str, Any] | SourceSelectionManifest,
) -> str:
    """Sealed identity; excludes ``generated_at`` and local body roots."""
    if isinstance(payload, SourceSelectionManifest):
        return payload.source_selection_id
    if not isinstance(payload, Mapping):
        raise ValueError("source_selection_id requires a manifest mapping or object")
    documents: list[SelectedDocument] = []
    for index, document in enumerate(payload.get("documents") or []):
        if not isinstance(document, Mapping):
            raise ValueError(f"source-selection document [{index}] must be an object")
        documents.append(
            SelectedDocument(
                canonical_url=str(document.get("canonical_url", "")),
                source_class=str(document.get("source_class", "")),
                source_published_at=str(document.get("source_published_at", "")),
                eligible_at=str(document.get("eligible_at", "")),
                fetched_at=str(document.get("fetched_at", "")),
                body_sha256=str(document.get("body_sha256", "")),
                body_path=str(document.get("body_path", "")),
                provider=str(document.get("provider", "")),
                publisher=str(document.get("publisher", "")),
                tickers=tuple(str(item) for item in document.get("tickers") or ()),
            )
        )
    policy = payload.get("selection_policy_id")
    if not isinstance(policy, str) or not policy:
        raise ValueError("source-selection manifest requires selection_policy_id")
    return compute_source_selection_id(
        selection_policy_id=policy, documents=documents
    )


def load_source_selection_manifest(
    path: str | Path, *, body_root: str | Path
) -> SourceSelectionManifest:
    manifest_path = Path(path)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"source-selection manifest is unreadable: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("source-selection manifest must be a JSON object")
    _reject_forbidden(payload)
    unknown = sorted(set(payload) - _MANIFEST_KEYS)
    if unknown:
        raise ValueError(
            "source-selection manifest has unknown key(s): " + ", ".join(unknown)
        )
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("source-selection manifest schema_version mismatch")
    policy = payload.get("selection_policy_id")
    if policy != SELECTION_POLICY_ID:
        raise ValueError(
            f"source-selection manifest selection_policy_id must be "
            f"{SELECTION_POLICY_ID!r}"
        )
    generated_at = payload.get("generated_at")
    if generated_at is not None:
        if not isinstance(generated_at, str) or _UTC_ISO_Z.fullmatch(generated_at) is None:
            raise ValueError("source-selection generated_at must be UTC ISO Z")
    raw_documents = payload.get("documents")
    if not isinstance(raw_documents, (list, tuple)) or not raw_documents:
        raise ValueError("source-selection manifest requires a non-empty documents list")
    root = Path(body_root)
    documents: list[SelectedDocument] = []
    seen_urls: set[str] = set()
    seen_bodies: set[str] = set()
    seen_hashes: set[str] = set()
    for index, raw_document in enumerate(raw_documents):
        document = _validate_document(raw_document, index=index, body_root=root)
        if document.canonical_url in seen_urls:
            raise ValueError(
                "source-selection manifest has duplicate canonical_url "
                f"{document.canonical_url}"
            )
        if document.body_path in seen_bodies:
            raise ValueError(
                "source-selection manifest has duplicate body identity "
                f"{document.body_path}"
            )
        if document.body_sha256 in seen_hashes:
            raise ValueError(
                "source-selection manifest has duplicate body hash "
                f"{document.body_sha256}"
            )
        seen_urls.add(document.canonical_url)
        seen_bodies.add(document.body_path)
        seen_hashes.add(document.body_sha256)
        documents.append(document)
    return SourceSelectionManifest(
        schema_version=SCHEMA_VERSION,
        selection_policy_id=policy,
        documents=tuple(documents),
        source_selection_id=compute_source_selection_id(
            selection_policy_id=policy, documents=documents
        ),
        generated_at=generated_at,
    )


__all__ = [
    "FORBIDDEN_KEYS",
    "SCHEMA_VERSION",
    "SELECTION_POLICY_ID",
    "SOURCE_CLASSES",
    "SelectedDocument",
    "SourceSelectionManifest",
    "canonical_json_bytes",
    "compute_source_selection_id",
    "load_source_selection_manifest",
    "source_selection_id",
]
