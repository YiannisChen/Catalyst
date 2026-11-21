"""Read-only featured-case bundle support.

The bundle is a small SQLite file containing only public material: run-row
metadata, the non-admin projected run view, and the non-admin projected public
events. The tracked manifest is embedded in the bundle (and may also be read
from ``CATALYST_FEATURED_MANIFEST``) so the API can fail closed on corruption
or a manifest/content mismatch.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Literal

from pydantic import BaseModel, ConfigDict

from catalyst_app.api_dto import RunViewDTO
from catalyst_app.events import PublicRunEvent
from catalyst_app.persistence.connect import open_readonly
from catalyst_app.public_projection import (
    project_public_event,
    public_event_long_string_violations,
    public_projection_violations_many,
)
from catalyst_app.public_text import (
    is_sensitive_metadata_sentence,
    validate_safe_public_text,
)

FEATURED_BUNDLE_ENV = "CATALYST_FEATURED_BUNDLE"
FEATURED_MANIFEST_ENV = "CATALYST_FEATURED_MANIFEST"
FEATURED_SCHEMA_VERSION = "featured_bundle_v1"
FEATURED_MANIFEST_SCHEMA_VERSION = "featured_manifest_v1"
FEATURED_CATEGORIES: tuple[str, ...] = (
    "ANSWERED_REPORTED_ATTRIBUTION",
    "ANSWERED_GROUP_DRIVER",
    "ANSWERED_UNIQUE_COMPANY_EVENT",
    "ANSWERED_EARNINGS_FALLBACK",
    "ABSTAINED_NO_CAUSE",
    "ABSTAINED_VERIFIER_REJECTED",
    "PLACEBO_DAY",
    "REPORTED_REASON",
    "MOVED_WITH_GROUP",
)
FeaturedHealth = Literal["ready", "corrupt", "not_configured"]


class FeaturedUnavailable(RuntimeError):
    """The configured bundle is corrupt or inconsistent."""


class FeaturedItemDTO(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    title: str
    ticker: str | None = None
    session_date: str | None = None
    category: str
    tags: list[str]
    outcome: str | None = None
    summary: str
    source_run_id: str
    product_tier: str | None = None


class FeaturedListDTO(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    items: list[FeaturedItemDTO]
    categories: list[str]


class FeaturedDetailDTO(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    run_id: str
    recorded_at: str
    view: RunViewDTO
    notes: str | None = None


class FeaturedReplayEventDTO(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    offset_ms: int
    event: PublicRunEvent


class FeaturedReplayDTO(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    speed_hint: float
    events: list[FeaturedReplayEventDTO]


def canonical_json(value: Any) -> str:
    """Canonical JSON used by both the builder and the bundle verifier."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _hashable_case(case: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in case.items() if key != "content_sha256"}


def _featured_product_tier(answer: Any) -> str | None:
    if not isinstance(answer, dict):
        return None
    tier = answer.get("product_tier")
    if isinstance(tier, str):
        return tier
    from catalyst_app.product_tier import with_product_fields

    computed = with_product_fields(answer).get("product_tier")
    return computed if isinstance(computed, str) else None


def case_content_sha256(case: dict[str, Any], events: Iterable[dict[str, Any]]) -> str:
    return sha256_text(
        canonical_json({"case": _hashable_case(case), "events": list(events)})
    )


def bundle_content_sha256(
    cases: Iterable[tuple[dict[str, Any], list[dict[str, Any]]]],
) -> str:
    payload = {
        "schema_version": FEATURED_SCHEMA_VERSION,
        "cases": [
            {"case": _hashable_case(case), "events": list(events)}
            for case, events in cases
        ],
    }
    return sha256_text(canonical_json(payload))


def featured_bundle_path() -> Path | None:
    raw = os.getenv(FEATURED_BUNDLE_ENV)
    if raw is None or not raw.strip():
        return None
    return Path(raw).expanduser()


def _meta(conn: Any) -> dict[str, str]:
    rows = conn.execute("SELECT key, value FROM bundle_meta").fetchall()
    return {str(row["key"]): str(row["value"]) for row in rows}


def _required_tables(conn: Any) -> bool:
    names = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    return {"bundle_meta", "featured_cases", "featured_events"} <= names


def _load_manifest(manifest_json: str, expected_sha256: str) -> dict[str, Any]:
    actual = sha256_text(manifest_json)
    if actual != expected_sha256:
        raise FeaturedUnavailable("featured manifest hash mismatch")
    try:
        manifest = json.loads(manifest_json)
    except (TypeError, ValueError) as exc:
        raise FeaturedUnavailable("featured manifest is not valid JSON") from exc
    if not isinstance(manifest, dict):
        raise FeaturedUnavailable("featured manifest must be an object")
    if manifest.get("schema_version") != FEATURED_MANIFEST_SCHEMA_VERSION:
        raise FeaturedUnavailable("unsupported featured manifest schema")
    return manifest


def _external_manifest() -> dict[str, Any] | None:
    raw = os.getenv(FEATURED_MANIFEST_ENV)
    if raw is None or not raw.strip():
        return None
    path = Path(raw).expanduser()
    try:
        text = path.read_text(encoding="utf-8")
        manifest = json.loads(text)
    except (OSError, ValueError) as exc:
        raise FeaturedUnavailable("configured featured manifest is unavailable") from exc
    if not isinstance(manifest, dict):
        raise FeaturedUnavailable("configured featured manifest must be an object")
    return manifest


def _validate_manifest(
    manifest: dict[str, Any],
    cases: dict[str, dict[str, Any]],
    content_sha256: str,
) -> None:
    if manifest.get("bundle_content_sha256") != content_sha256:
        raise FeaturedUnavailable("featured manifest/content hash mismatch")
    entries = manifest.get("cases")
    if not isinstance(entries, list):
        raise FeaturedUnavailable("featured manifest cases must be a list")
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise FeaturedUnavailable("featured manifest case must be an object")
        case_id = entry.get("case_id")
        if not isinstance(case_id, str) or case_id in seen:
            raise FeaturedUnavailable("featured manifest case ids are invalid")
        seen.add(case_id)
        case = cases.get(case_id)
        if case is None:
            raise FeaturedUnavailable("featured manifest references an unknown case")
        for field in ("run_id", "category", "title", "tags", "summary", "source_db_sha256"):
            if entry.get(field) != case.get(field):
                raise FeaturedUnavailable(f"featured manifest mismatch for {field}")
        if entry.get("bundle_content_sha256") != case.get("content_sha256"):
            raise FeaturedUnavailable("featured manifest case/content hash mismatch")
    if seen != set(cases):
        raise FeaturedUnavailable("featured manifest does not cover every case")


@dataclass(frozen=True)
class _Bundle:
    meta: dict[str, str]
    cases: dict[str, dict[str, Any]]
    events: dict[str, list[dict[str, Any]]]
    categories: tuple[str, ...]


def _load_bundle(path: Path) -> _Bundle:
    if not path.is_file():
        raise FeaturedUnavailable("featured bundle is missing")
    try:
        with open_readonly(path) as conn:
            if not _required_tables(conn):
                raise FeaturedUnavailable("featured bundle tables are missing")
            meta = _meta(conn)
            if meta.get("schema_version") != FEATURED_SCHEMA_VERSION:
                raise FeaturedUnavailable("unsupported featured bundle schema")
            case_rows = conn.execute(
                "SELECT * FROM featured_cases ORDER BY sort_order, case_id"
            ).fetchall()
            event_rows = conn.execute(
                "SELECT * FROM featured_events ORDER BY case_id, sequence"
            ).fetchall()
    except FeaturedUnavailable:
        raise
    except Exception as exc:
        raise FeaturedUnavailable("featured bundle is unreadable") from exc

    return _assemble_bundle(meta, case_rows, event_rows)


@lru_cache(maxsize=8)
def _load_bundle_cached(path: str, mtime_ns: int, size: int) -> _Bundle:
    del mtime_ns, size
    return _load_bundle(Path(path))


_FEATURED_CACHE_KEYS: set[tuple[str, int, int]] = set()
_FEATURED_CACHE_ERRORS: set[tuple[str, int, int]] = set()


def _cached_bundle(path: Path) -> _Bundle | None:
    try:
        resolved = path.expanduser().resolve()
        stat = resolved.stat()
    except OSError as exc:
        raise FeaturedUnavailable("featured bundle is missing") from exc
    key = (str(resolved), stat.st_mtime_ns, stat.st_size)
    try:
        bundle = _load_bundle_cached(*key)
    except FeaturedUnavailable:
        _FEATURED_CACHE_KEYS.add(key)
        _FEATURED_CACHE_ERRORS.add(key)
        raise
    _FEATURED_CACHE_KEYS.add(key)
    _FEATURED_CACHE_ERRORS.discard(key)
    if len(_FEATURED_CACHE_KEYS) > 8:
        stale = _FEATURED_CACHE_KEYS.pop()
        _FEATURED_CACHE_ERRORS.discard(stale)
    return bundle


def _assemble_bundle(meta: dict[str, str], case_rows: list[Any], event_rows: list[Any]) -> _Bundle:
    try:
        return _assemble_bundle_unchecked(meta, case_rows, event_rows)
    except FeaturedUnavailable:
        raise
    except Exception as exc:
        raise FeaturedUnavailable("featured bundle is invalid") from exc


def _metadata_violations(value: Any, label: str, *, allow_empty: bool = False) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, str):
        return [f"{label}: must be a string"]
    if allow_empty and not value:
        return []
    try:
        validate_safe_public_text(value)
    except ValueError as exc:
        return [f"{label}: {exc}"]
    return []


def _normalize_metadata_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[\W_]+", "", normalized, flags=re.UNICODE)


def _metadata_sentences(value: str) -> list[str]:
    chunks = re.split(r"(?<=[.!?。！？；;])\s*|\n+", value)
    sentences = [chunk.strip() for chunk in chunks if chunk.strip()]
    if not sentences and value.strip():
        sentences = [value.strip()]
    return sentences


def _iter_source_texts(value: Any) -> list[str]:
    texts: list[str] = []
    if isinstance(value, dict):
        if isinstance(value.get("text"), str) and (
            "publisher" in value or "url" in value
        ):
            texts.append(value["text"])
        for item in value.values():
            texts.extend(_iter_source_texts(item))
    elif isinstance(value, list):
        for item in value:
            texts.extend(_iter_source_texts(item))
    return texts


def _metadata_view_violations(
    value: Any, label: str, view: dict[str, Any]
) -> list[str]:
    if not isinstance(value, str) or not value:
        return []
    normalized_value = _normalize_metadata_text(value)
    for source_text in _iter_source_texts(view):
        for sentence in _metadata_sentences(source_text):
            if not is_sensitive_metadata_sentence(sentence):
                continue
            normalized_sentence = _normalize_metadata_text(sentence)
            if normalized_sentence and normalized_sentence in normalized_value:
                matched = " ".join(sentence.split())[:60]
                return [
                    f"{label}: contains source text from the stored view {matched!r}"
                ]
    return []


def _assemble_bundle_unchecked(
    meta: dict[str, str], case_rows: list[Any], event_rows: list[Any]
) -> _Bundle:
    manifest = _load_manifest(
        meta.get("manifest_json", ""), meta.get("manifest_sha256", "")
    )
    external = _external_manifest()
    if external is not None and external != manifest:
        raise FeaturedUnavailable("featured manifest/bundle mismatch")

    cases: dict[str, dict[str, Any]] = {}
    for row in case_rows:
        try:
            tags = json.loads(row["tags_json"])
            view = json.loads(row["view_json"])
        except (TypeError, ValueError) as exc:
            raise FeaturedUnavailable("featured bundle contains malformed JSON") from exc
        if not isinstance(tags, list) or not all(isinstance(item, str) for item in tags):
            raise FeaturedUnavailable("featured tags must be strings")
        if not isinstance(view, dict):
            raise FeaturedUnavailable("featured view must be an object")
        case = {
            "case_id": str(row["case_id"]),
            "run_id": str(row["run_id"]),
            "ticker": row["ticker"],
            "session_date": row["session_date"],
            "category": str(row["category"]),
            "title": str(row["title"]),
            "tags": tags,
            "summary": str(row["summary"]),
            "notes": row["notes"],
            "outcome": row["outcome"],
            "recorded_at": str(row["recorded_at"]),
            "lifecycle_status": str(row["lifecycle_status"]),
            "run_created_at": str(row["run_created_at"]),
            "run_updated_at": str(row["run_updated_at"]),
            "source_db_sha256": str(row["source_db_sha256"]),
            "speed_hint": float(row["speed_hint"]),
            "sort_order": int(row["sort_order"]),
            "view": view,
            "content_sha256": str(row["content_sha256"]),
        }
        metadata_violations = (
            _metadata_violations(case["title"], f"{case['case_id']}.title")
            + _metadata_violations(case["summary"], f"{case['case_id']}.summary")
            + _metadata_violations(
                case["notes"], f"{case['case_id']}.notes", allow_empty=True
            )
            + _metadata_view_violations(
                case["title"], f"{case['case_id']}.title", case["view"]
            )
            + _metadata_view_violations(
                case["summary"], f"{case['case_id']}.summary", case["view"]
            )
            + _metadata_view_violations(
                case["notes"], f"{case['case_id']}.notes", case["view"]
            )
        )
        if metadata_violations:
            raise FeaturedUnavailable(
                "featured metadata violates public text rules: "
                + "; ".join(metadata_violations[:4])
            )
        cases[case["case_id"]] = case

    events: dict[str, list[dict[str, Any]]] = {case_id: [] for case_id in cases}
    previous_offset: dict[str, int] = {}
    previous_sequence: dict[str, int] = {}
    for row in event_rows:
        case_id = str(row["case_id"])
        if case_id not in cases:
            raise FeaturedUnavailable("featured event references an unknown case")
        try:
            event = json.loads(row["event_json"])
        except (TypeError, ValueError) as exc:
            raise FeaturedUnavailable("featured event JSON is malformed") from exc
        if not isinstance(event, dict):
            raise FeaturedUnavailable("featured event must be an object")
        offset = int(row["offset_ms"])
        sequence = int(row["sequence"])
        if case_id in previous_offset and offset < previous_offset[case_id]:
            raise FeaturedUnavailable("featured replay offsets are not monotonic")
        if case_id in previous_sequence and sequence <= previous_sequence[case_id]:
            raise FeaturedUnavailable("featured replay sequences are not increasing")
        previous_offset[case_id] = offset
        previous_sequence[case_id] = sequence
        events[case_id].append({"offset_ms": offset, "event": event})

    bundle_cases = [
        (
            cases[str(row["case_id"])],
            events[str(row["case_id"])],
        )
        for row in case_rows
    ]
    content_sha256 = bundle_content_sha256(bundle_cases)
    if meta.get("bundle_content_sha256") != content_sha256:
        raise FeaturedUnavailable("featured bundle content hash mismatch")
    _validate_manifest(manifest, cases, content_sha256)

    for case in cases.values():
        category = case["category"]
        if category not in FEATURED_CATEGORIES:
            raise FeaturedUnavailable("featured case has an unsupported category")
        try:
            RunViewDTO.model_validate(case["view"])
        except Exception as exc:
            raise FeaturedUnavailable("featured view does not match run_view_v1") from exc
        documents: list[tuple[Any, str]] = [(case["view"], f"$.cases[{case['case_id']}].view")]
        for index, item in enumerate(events[case["case_id"]]):
            try:
                event_model = PublicRunEvent.model_validate(item["event"])
            except Exception as exc:
                raise FeaturedUnavailable("featured event does not match PublicRunEvent") from exc
            long_string_violations = public_event_long_string_violations(event_model)
            if long_string_violations:
                raise FeaturedUnavailable(
                    "featured event has non-public long text: "
                    + "; ".join(long_string_violations[:4])
                )
            documents.append(
                (item["event"], f"$.cases[{case['case_id']}].events[{index}].event")
            )
        violations = public_projection_violations_many(documents)
        if violations:
            raise FeaturedUnavailable("featured case violates public text rules")

    used_categories = {case["category"] for case in cases.values()}
    categories = tuple(category for category in FEATURED_CATEGORIES if category in used_categories)
    return _Bundle(meta=meta, cases=cases, events=events, categories=categories)


def featured_health_status(*, cached_only: bool = False) -> FeaturedHealth:
    path = featured_bundle_path()
    if path is None or not path.is_file():
        return "not_configured"
    if cached_only:
        try:
            resolved = path.expanduser().resolve()
            stat = resolved.stat()
        except OSError:
            return "corrupt"
        key = (str(resolved), stat.st_mtime_ns, stat.st_size)
        if key not in _FEATURED_CACHE_KEYS:
            return "not_configured"
        return "corrupt" if key in _FEATURED_CACHE_ERRORS else "ready"
    try:
        _cached_bundle(path)
    except Exception:
        return "corrupt"
    return "ready"


def featured_is_ready() -> bool:
    return featured_health_status() == "ready"


def _load_configured_bundle() -> _Bundle | None:
    path = featured_bundle_path()
    if path is None or not path.is_file():
        return None
    return _cached_bundle(path)


def load_featured_list() -> FeaturedListDTO:
    bundle = _load_configured_bundle()
    if bundle is None:
        return FeaturedListDTO(items=[], categories=[])
    items = [
        FeaturedItemDTO(
            case_id=case["case_id"],
            title=case["title"],
            ticker=case["ticker"],
            session_date=case["session_date"],
            category=case["category"],
            tags=list(case["tags"]),
            outcome=case["outcome"],
            summary=case["summary"],
            source_run_id=case["run_id"],
            product_tier=_featured_product_tier(case["view"].get("answer")),
        )
        for case in sorted(
            bundle.cases.values(), key=lambda value: (value["sort_order"], value["case_id"])
        )
    ]
    used_categories = {item.category for item in items}
    categories = [category for category in FEATURED_CATEGORIES if category in used_categories]
    return FeaturedListDTO(items=items, categories=categories)


def load_featured_case(case_id: str) -> FeaturedDetailDTO | None:
    bundle = _load_configured_bundle()
    if bundle is None:
        return None
    case = bundle.cases.get(case_id)
    if case is None:
        return None
    view = RunViewDTO.model_validate(case["view"])
    return FeaturedDetailDTO(
        case_id=case["case_id"],
        run_id=case["run_id"],
        recorded_at=case["recorded_at"],
        view=view,
        notes=None,
    )


def load_featured_replay(case_id: str) -> FeaturedReplayDTO | None:
    bundle = _load_configured_bundle()
    if bundle is None:
        return None
    case = bundle.cases.get(case_id)
    if case is None:
        return None
    replay_events: list[FeaturedReplayEventDTO] = []
    for item in bundle.events[case_id]:
        event = PublicRunEvent.model_validate(item["event"])
        projected = project_public_event(event)
        if projected is None:
            continue
        replay_events.append(
            FeaturedReplayEventDTO(offset_ms=item["offset_ms"], event=projected)
        )
    return FeaturedReplayDTO(
        case_id=case["case_id"],
        speed_hint=case["speed_hint"],
        events=replay_events,
    )


__all__ = [
    "FEATURED_BUNDLE_ENV",
    "FEATURED_CATEGORIES",
    "FEATURED_MANIFEST_ENV",
    "FEATURED_MANIFEST_SCHEMA_VERSION",
    "FEATURED_SCHEMA_VERSION",
    "FeaturedDetailDTO",
    "FeaturedItemDTO",
    "FeaturedListDTO",
    "FeaturedReplayDTO",
    "FeaturedReplayEventDTO",
    "FeaturedUnavailable",
    "bundle_content_sha256",
    "canonical_json",
    "case_content_sha256",
    "featured_bundle_path",
    "featured_health_status",
    "featured_is_ready",
    "load_featured_case",
    "load_featured_list",
    "load_featured_replay",
    "sha256_text",
]
