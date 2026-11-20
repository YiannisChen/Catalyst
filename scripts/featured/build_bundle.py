#!/usr/bin/env python3
"""Build the public featured-case SQLite bundle and its tracked manifest.

Input selection JSON is a list of objects (or ``{"cases": [...]}``) with:
``case_id, run_id, source_runtime_db, category, title, tags, summary, notes``.

Only public material is copied. The builder never copies context packs,
rendered prompts, decision traces, evidence details, provider accounting or
LLM recordings. Every answer view and event is passed through the same
non-admin projection used by ``/view`` and SSE before it is validated and
written.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [
    str(REPO / "packages/api"),
    str(REPO / "packages/agents"),
    str(REPO / "packages/data"),
]

from catalyst_app.events import (  # noqa: E402
    _EVENT_PAYLOAD_TYPES,
    ArtifactRef,
    PublicRunEvent,
    RunEventType,
    serialize_public_event,
)
from catalyst_app.featured import (  # noqa: E402
    FEATURED_CATEGORIES,
    FEATURED_MANIFEST_SCHEMA_VERSION,
    FEATURED_SCHEMA_VERSION,
    bundle_content_sha256,
    canonical_json,
    case_content_sha256,
    sha256_text,
)
from catalyst_app.persistence.connect import open_readonly  # noqa: E402
from catalyst_app.public_projection import (  # noqa: E402
    PublicEventProjector,
    _source_key,
    is_benzinga_source,
    project_public_run_view,
    public_projection_violations_many,
)
from catalyst_app.public_text import (  # noqa: E402
    is_sensitive_metadata_sentence,
    validate_safe_public_text,
)
from catalyst_app.routers.live_runs import _read_run_view  # noqa: E402
from fastapi import HTTPException  # noqa: E402

DEFAULT_BUNDLE = REPO / "data/featured/featured_cases_v1.sqlite3"
DEFAULT_MANIFEST = REPO / "data/manifests/featured_cases_v1.json"
_MODEL_TOKEN_EVENT_TYPES = frozenset({RunEventType.ANSWER_DELTA})


class BuildError(ValueError):
    """The selection or source run is not eligible for publication."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return result.stdout.strip() or "unknown"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_selection(path: str | Path) -> list[dict[str, Any]]:
    selection_path = Path(path)
    try:
        payload = json.loads(selection_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BuildError(f"cannot read selection file: {exc}") from exc
    if isinstance(payload, dict):
        payload = payload.get("cases")
    if not isinstance(payload, list):
        raise BuildError("selection file must be a JSON list or {'cases': [...]}")

    required = ("case_id", "run_id", "source_runtime_db", "category", "title", "summary")
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(payload):
        if not isinstance(raw, dict):
            raise BuildError(f"selection entry {index} must be an object")
        missing = [field for field in required if not isinstance(raw.get(field), str) or not raw[field].strip()]
        if missing:
            raise BuildError(
                f"selection entry {index} is missing nonempty fields: {', '.join(missing)}"
            )
        case_id = raw["case_id"].strip()
        if case_id in seen:
            raise BuildError(f"duplicate case_id: {case_id}")
        seen.add(case_id)
        category = raw["category"].strip()
        if category not in FEATURED_CATEGORIES:
            raise BuildError(
                f"{case_id}: category {category!r} is not one of {FEATURED_CATEGORIES}"
            )
        tags = raw.get("tags", [])
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise BuildError(f"{case_id}: tags must be a list of strings")
        notes = raw.get("notes")
        if notes is not None and not isinstance(notes, str):
            raise BuildError(f"{case_id}: notes must be a string or null")
        result.append(
            {
                **raw,
                "case_id": case_id,
                "run_id": raw["run_id"].strip(),
                "source_runtime_db": raw["source_runtime_db"].strip(),
                "category": category,
                "tags": list(tags),
                "notes": notes,
            }
        )
    return result


def _resolve_source_db(raw: str, *, selection_path: Path, source_root: Path | None) -> Path:
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        return candidate
    candidates = []
    if source_root is not None:
        candidates.append(source_root / candidate)
    candidates.extend((selection_path.parent / candidate, Path.cwd() / candidate))
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]


def _run_row(db_path: Path, run_id: str) -> dict[str, Any]:
    with open_readonly(db_path) as conn:
        row = conn.execute(
            "SELECT run_id, lifecycle_status, ticker, created_at, updated_at "
            "FROM runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
    if row is None:
        raise BuildError(f"run not found in source DB: {run_id}")
    return {key: row[key] for key in row.keys()}


def _session_date(db_path: Path, run_id: str) -> str | None:
    with open_readonly(db_path) as conn:
        row = conn.execute(
            "SELECT payload_json FROM run_artifacts WHERE run_id=? "
            "AND artifact_type='run_manifest' ORDER BY event_seq ASC LIMIT 1",
            (run_id,),
        ).fetchone()
    if row is None:
        return None
    try:
        manifest = json.loads(row["payload_json"])
    except (TypeError, ValueError):
        return None
    temporal = manifest.get("temporal_identity") if isinstance(manifest, dict) else None
    value = temporal.get("session_date") if isinstance(temporal, dict) else None
    return value if isinstance(value, str) else None


def _project_run_view(db_path: Path, run_id: str) -> dict[str, Any]:
    try:
        view = _read_run_view(db_path, run_id)
    except HTTPException as exc:
        raise BuildError(f"cannot read run view for {run_id}: {exc.detail}") from exc
    except Exception as exc:
        raise BuildError(f"cannot read run view for {run_id}: {exc}") from exc
    if view is None:
        raise BuildError(f"run not found in source DB: {run_id}")
    projected = project_public_run_view(view)
    return projected.model_dump(mode="json")


def _project_events(db_path: Path, run_id: str) -> list[dict[str, Any]]:
    with open_readonly(db_path) as conn:
        rows = conn.execute(
            "SELECT seq, occurred_at, event_type, stage, payload_json, schema_version "
            "FROM run_events WHERE run_id=? ORDER BY seq ASC",
            (run_id,),
        ).fetchall()
        artifact_rows = conn.execute(
            "SELECT event_seq, artifact_id, artifact_type, payload_hash "
            "FROM run_artifacts WHERE run_id=? ORDER BY event_seq ASC",
            (run_id,),
        ).fetchall()
    refs: dict[int, list[ArtifactRef]] = {}
    for row in artifact_rows:
        refs.setdefault(int(row["event_seq"]), []).append(
            ArtifactRef(
                artifact_id=str(row["artifact_id"]),
                artifact_type=str(row["artifact_type"]),
                schema_version="v1",
                content_sha256=str(row["payload_hash"]),
            )
        )

    projector = PublicEventProjector()
    projected: list[dict[str, Any]] = []
    first_at: datetime | None = None
    previous_offset = 0
    for row in rows:
        try:
            event_type = RunEventType(str(row["event_type"]))
            payload_model = _EVENT_PAYLOAD_TYPES[event_type]
            payload = payload_model.model_validate_json(row["payload_json"])
            event = PublicRunEvent(
                schema_version=str(row["schema_version"]),
                event_id=None,
                run_id=run_id,
                sequence=int(row["seq"]),
                event_type=event_type,
                emitted_at=row["occurred_at"],
                stage=row["stage"],
                payload=payload,
                artifact_refs=tuple(refs.get(int(row["seq"]), ())),
            )
        except Exception as exc:
            raise BuildError(f"cannot project event {row['seq']} for {run_id}: {exc}") from exc
        public_event = projector(event)
        if public_event is None or public_event.event_type in _MODEL_TOKEN_EVENT_TYPES:
            continue
        if first_at is None:
            first_at = event.emitted_at
        offset = max(0, int((event.emitted_at - first_at).total_seconds() * 1000))
        offset = max(previous_offset, offset)
        previous_offset = offset
        projected.append(
            {
                "offset_ms": offset,
                "event": json.loads(serialize_public_event(public_event)),
            }
        )
    return projected


def validate_public_material(
    view: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    original_texts: dict[str, str] | None = None,
) -> None:
    """Refuse any projected material that still violates the public rules."""
    documents: list[tuple[object, str]] = [(view, "$view")]
    for index, item in enumerate(events):
        event = item.get("event") if isinstance(item, dict) else None
        documents.append((event, f"$.events[{index}].event"))
    violations = public_projection_violations_many(
        documents, original_texts=original_texts
    )
    if violations:
        raise BuildError(
            "projected material violates public rules: " + "; ".join(violations[:8])
        )


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


def _raw_answer_snapshot(db_path: Path, run_id: str) -> dict[str, Any] | None:
    with open_readonly(db_path) as conn:
        row = conn.execute(
            "SELECT payload_json FROM run_artifacts WHERE run_id=? "
            "AND artifact_type='answer_snapshot' ORDER BY event_seq DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        if row is not None:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError):
                payload = None
            if isinstance(payload, dict):
                return payload
        row = conn.execute(
            "SELECT payload_json FROM run_events WHERE run_id=? AND event_type='answer.ready' "
            "ORDER BY seq DESC LIMIT 1",
            (run_id,),
        ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row["payload_json"])
    except (TypeError, ValueError):
        return None
    snapshot = payload.get("answer_snapshot") if isinstance(payload, dict) else None
    return snapshot if isinstance(snapshot, dict) else None


def _benzinga_original_texts(snapshot: dict[str, Any] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    if not isinstance(snapshot, dict):
        return result
    for path in _iter_source_mappings_for_builder(snapshot):
        if not is_benzinga_source(path.get("publisher"), path.get("url")):
            continue
        text = path.get("text")
        url = path.get("url")
        if not isinstance(text, str) or not text.strip() or not isinstance(url, str):
            continue
        try:
            key = _source_key(url)
        except ValueError:
            continue
        if len(text) > len(result.get(key, "")):
            result[key] = text
    return result


def _iter_source_mappings_for_builder(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if isinstance(value.get("text"), str) and (
            "publisher" in value or "url" in value
        ):
            found.append(value)
        for item in value.values():
            found.extend(_iter_source_mappings_for_builder(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_iter_source_mappings_for_builder(item))
    return found


def _metadata_violations(
    value: str | None, label: str, *, allow_empty: bool = False
) -> list[str]:
    if value is None:
        return []
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


def _all_strings(value: Any) -> list[str]:
    strings: list[str] = []
    if isinstance(value, str):
        strings.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            strings.extend(_all_strings(item))
    elif isinstance(value, list):
        for item in value:
            strings.extend(_all_strings(item))
    return strings


def _keyed_strings(value: Any, keys: set[str]) -> list[str]:
    strings: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in keys:
                strings.extend(_all_strings(item))
            else:
                strings.extend(_keyed_strings(item, keys))
    elif isinstance(value, list):
        for item in value:
            strings.extend(_keyed_strings(item, keys))
    return strings


def _sensitive_metadata_strings(
    db_path: Path, run_id: str, snapshot: dict[str, Any] | None
) -> list[str]:
    values: list[str] = _iter_source_texts(snapshot)
    with open_readonly(db_path) as conn:
        rows = conn.execute(
            "SELECT artifact_type, payload_json FROM run_artifacts WHERE run_id=? "
            "AND artifact_type IN ('rendered_messages','context_pack',"
            "'evidence_detail','decision_trace')",
            (run_id,),
        ).fetchall()
        event_row = conn.execute(
            "SELECT payload_json FROM run_events WHERE run_id=? "
            "AND event_type='investigator.done' ORDER BY seq DESC LIMIT 1",
            (run_id,),
        ).fetchone()
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            continue
        artifact_type = str(row["artifact_type"])
        if artifact_type == "rendered_messages":
            values.extend(_all_strings(payload))
        elif artifact_type in {"context_pack", "evidence_detail"}:
            values.extend(
                _keyed_strings(payload, {"text", "excerpt", "body", "body_text", "content"})
            )
        else:
            values.extend(
                _keyed_strings(payload, {"summary", "note", "notes", "search_hint"})
            )
    if event_row is not None:
        try:
            payload = json.loads(event_row["payload_json"])
        except (TypeError, ValueError):
            payload = None
        values.extend(
            _keyed_strings(payload, {"summary", "note", "notes", "search_hint"})
        )
    return values


def _metadata_leak_violations(
    value: str,
    sensitive: list[str],
    label: str,
    *,
    case_id: str = "case",
) -> list[str]:
    normalized_value = _normalize_metadata_text(value)
    for raw in sensitive:
        for sentence in _metadata_sentences(raw):
            if not is_sensitive_metadata_sentence(sentence):
                continue
            normalized_sentence = _normalize_metadata_text(sentence)
            if normalized_sentence and normalized_sentence in normalized_value:
                matched = " ".join(sentence.split())[:60]
                return [
                    f"{case_id}.{label}: contains source, prompt, or investigation "
                    f"sentence {matched!r}"
                ]
    return []


def validate_selection_metadata(
    entry: dict[str, Any],
    view: dict[str, Any],
    db_path: Path,
    *,
    snapshot: dict[str, Any] | None = None,
) -> None:
    violations = (
        _metadata_violations(entry["title"], "title")
        + _metadata_violations(entry["summary"], "summary")
        + _metadata_violations(entry.get("notes"), "notes", allow_empty=True)
    )
    if snapshot is None:
        snapshot = _raw_answer_snapshot(db_path, entry["run_id"])
    sensitive = _sensitive_metadata_strings(db_path, entry["run_id"], snapshot)
    for label, value in (
        ("title", entry["title"]),
        ("summary", entry["summary"]),
        ("notes", entry.get("notes")),
    ):
        if not isinstance(value, str) or not value:
            continue
        violations.extend(
            _metadata_leak_violations(
                value,
                sensitive,
                label,
                case_id=entry["case_id"],
            )
        )
    if violations:
        raise BuildError("selection metadata violates public rules: " + "; ".join(violations[:6]))


def _outcome(view: dict[str, Any], db_path: Path, run_id: str) -> str | None:
    answer = view.get("answer")
    if isinstance(answer, dict) and isinstance(answer.get("outcome"), str):
        return answer["outcome"]
    with open_readonly(db_path) as conn:
        row = conn.execute(
            "SELECT payload_json FROM run_events WHERE run_id=? AND event_type='run.completed' "
            "ORDER BY seq DESC LIMIT 1",
            (run_id,),
        ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row["payload_json"])
    except (TypeError, ValueError):
        return None
    status = payload.get("result_status") if isinstance(payload, dict) else None
    if status in {"ABSTAIN", "ABSTAINED"}:
        return "ABSTAINED"
    if status in {"SUCCEEDED", "ANSWERED", "PARTIAL"}:
        return "ANSWERED"
    return None


def build_case(entry: dict[str, Any], *, sort_order: int, source_root: Path | None = None, selection_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    db_path = _resolve_source_db(
        entry["source_runtime_db"], selection_path=selection_path, source_root=source_root
    )
    if not db_path.is_file():
        raise BuildError(f"{entry['case_id']}: source runtime DB not found: {db_path}")
    run = _run_row(db_path, entry["run_id"])
    if run["lifecycle_status"] != "COMPLETED":
        raise BuildError(
            f"{entry['case_id']}: run {entry['run_id']} is {run['lifecycle_status']}, not COMPLETED"
        )
    view = _project_run_view(db_path, entry["run_id"])
    events = _project_events(db_path, entry["run_id"])
    raw_snapshot = _raw_answer_snapshot(db_path, entry["run_id"])
    validate_public_material(
        view,
        events,
        original_texts=_benzinga_original_texts(raw_snapshot),
    )
    validate_selection_metadata(entry, view, db_path, snapshot=raw_snapshot)
    view_run = view.get("run") if isinstance(view.get("run"), dict) else {}
    case = {
        "case_id": entry["case_id"],
        "run_id": entry["run_id"],
        "ticker": view_run.get("ticker") or run.get("ticker"),
        "session_date": view_run.get("session_date") or _session_date(db_path, entry["run_id"]),
        "category": entry["category"],
        "title": entry["title"],
        "tags": list(entry["tags"]),
        "summary": entry["summary"],
        "notes": entry["notes"],
        "outcome": _outcome(view, db_path, entry["run_id"]),
        "recorded_at": view_run.get("created_at") or run["created_at"],
        "lifecycle_status": run["lifecycle_status"],
        "run_created_at": run["created_at"],
        "run_updated_at": run["updated_at"],
        "source_db_sha256": _sha256_file(db_path),
        "speed_hint": 1.0,
        "sort_order": sort_order,
        "view": view,
    }
    case["content_sha256"] = case_content_sha256(case, events)
    return case, events


_CASE_DDL = """
CREATE TABLE bundle_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE featured_cases (
    case_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE,
    ticker TEXT,
    session_date TEXT,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    summary TEXT NOT NULL,
    notes TEXT,
    outcome TEXT,
    recorded_at TEXT NOT NULL,
    lifecycle_status TEXT NOT NULL,
    run_created_at TEXT NOT NULL,
    run_updated_at TEXT NOT NULL,
    source_db_sha256 TEXT NOT NULL,
    speed_hint REAL NOT NULL,
    sort_order INTEGER NOT NULL,
    view_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL
);
CREATE TABLE featured_events (
    case_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    offset_ms INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    emitted_at TEXT NOT NULL,
    stage TEXT,
    schema_version TEXT NOT NULL,
    event_json TEXT NOT NULL,
    PRIMARY KEY (case_id, sequence),
    FOREIGN KEY (case_id) REFERENCES featured_cases(case_id)
);
"""


def write_bundle(
    *,
    bundle_path: Path,
    manifest_path: Path,
    cases: list[tuple[dict[str, Any], list[dict[str, Any]]]],
    built_at: str,
    builder_commit: str,
) -> dict[str, Any]:
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    content_sha = bundle_content_sha256(cases)
    manifest = {
        "schema_version": FEATURED_MANIFEST_SCHEMA_VERSION,
        "bundle_content_sha256": content_sha,
        "built_at": built_at,
        "builder_commit": builder_commit,
        "cases": [
            {
                "case_id": case["case_id"],
                "run_id": case["run_id"],
                "category": case["category"],
                "title": case["title"],
                "tags": list(case["tags"]),
                "summary": case["summary"],
                "source_db_sha256": case["source_db_sha256"],
                "bundle_content_sha256": case["content_sha256"],
                "built_at": built_at,
                "builder_commit": builder_commit,
            }
            for case, _ in cases
        ],
    }
    manifest_json = canonical_json(manifest)

    temporary = bundle_path.with_name(bundle_path.name + ".tmp")
    if temporary.exists():
        temporary.unlink()
    conn = sqlite3.connect(temporary)
    try:
        conn.executescript(_CASE_DDL)
        meta = {
            "schema_version": FEATURED_SCHEMA_VERSION,
            "built_at": built_at,
            "builder_commit": builder_commit,
            "bundle_content_sha256": content_sha,
            "manifest_json": manifest_json,
            "manifest_sha256": sha256_text(manifest_json),
            "case_count": str(len(cases)),
        }
        conn.executemany(
            "INSERT INTO bundle_meta(key, value) VALUES (?, ?)", meta.items()
        )
        for case, events in cases:
            conn.execute(
                "INSERT INTO featured_cases (case_id, run_id, ticker, session_date, category,"
                " title, tags_json, summary, notes, outcome, recorded_at, lifecycle_status,"
                " run_created_at, run_updated_at, source_db_sha256, speed_hint, sort_order,"
                " view_json, content_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                " ?, ?, ?, ?, ?, ?)",
                (
                    case["case_id"],
                    case["run_id"],
                    case["ticker"],
                    case["session_date"],
                    case["category"],
                    case["title"],
                    canonical_json(case["tags"]),
                    case["summary"],
                    case["notes"],
                    case["outcome"],
                    case["recorded_at"],
                    case["lifecycle_status"],
                    case["run_created_at"],
                    case["run_updated_at"],
                    case["source_db_sha256"],
                    case["speed_hint"],
                    case["sort_order"],
                    canonical_json(case["view"]),
                    case["content_sha256"],
                ),
            )
            for item in events:
                event = item["event"]
                conn.execute(
                    "INSERT INTO featured_events (case_id, sequence, offset_ms, event_type,"
                    " emitted_at, stage, schema_version, event_json) VALUES (?, ?, ?, ?, ?,"
                    " ?, ?, ?)",
                    (
                        case["case_id"],
                        int(event["sequence"]),
                        int(item["offset_ms"]),
                        str(event["event_type"]),
                        str(event["emitted_at"]),
                        event.get("stage"),
                        str(event["schema_version"]),
                        canonical_json(event),
                    ),
                )
        conn.commit()
    finally:
        conn.close()
    os.replace(temporary, bundle_path)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return manifest


def build_bundle(
    *,
    selection: str | Path,
    bundle: str | Path = DEFAULT_BUNDLE,
    manifest: str | Path = DEFAULT_MANIFEST,
    source_root: str | Path | None = None,
    built_at: str | None = None,
    builder_commit: str | None = None,
) -> dict[str, Any]:
    selection_path = Path(selection)
    entries = load_selection(selection_path)
    root = Path(source_root).expanduser() if source_root is not None else None
    built = built_at or _utc_now()
    commit = builder_commit or _git_commit()
    cases: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for index, entry in enumerate(entries):
        cases.append(
            build_case(
                entry,
                sort_order=index,
                source_root=root,
                selection_path=selection_path,
            )
        )
    return write_bundle(
        bundle_path=Path(bundle).expanduser(),
        manifest_path=Path(manifest).expanduser(),
        cases=cases,
        built_at=built,
        builder_commit=commit,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection", required=True, help="selection JSON file")
    parser.add_argument("--bundle", default=str(DEFAULT_BUNDLE), help="output SQLite bundle")
    parser.add_argument(
        "--manifest", default=str(DEFAULT_MANIFEST), help="tracked manifest JSON"
    )
    parser.add_argument(
        "--source-root", default=None, help="root for relative source_runtime_db paths"
    )
    args = parser.parse_args(argv)
    try:
        manifest = build_bundle(
            selection=args.selection,
            bundle=args.bundle,
            manifest=args.manifest,
            source_root=args.source_root,
        )
    except BuildError as exc:
        print(f"featured build refused: {exc}", file=sys.stderr)
        return 2
    print(
        f"wrote {args.bundle} and {args.manifest} "
        f"({len(manifest['cases'])} cases, sha256 {manifest['bundle_content_sha256']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
