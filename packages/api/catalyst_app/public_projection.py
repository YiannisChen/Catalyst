"""Public (non-admin) response projection for run answers and progress events."""
from __future__ import annotations

import os
import re
from copy import deepcopy
from typing import Any, Mapping
from urllib.parse import urlsplit

from catalyst_agents.attribution.quotes import _ABBREVIATIONS
from pydantic import BaseModel

from catalyst_app.api_dto import RunViewDTO, RunViewPublicAnswerDTO
from catalyst_app.events import (
    PublicRunEvent,
    RunEventType,
)

_PUBLIC_SOURCE_FIELDS = (
    "text",
    "publisher",
    "url",
    "evidence_id",
    "role",
    "published_at",
    "published_precision",
    "release_session",
    "release_time_et",
)
_PUBLIC_ATTRIBUTION_FIELDS = ("quotes", "event_type", "scope", "event_date")
_PUBLIC_POSSIBLE_EXPLANATION_FIELDS = (
    "quotes",
    "verdict_category",
    "category_text",
    "label",
)
_PUBLIC_LEAD_FIELDS = ("title", "publisher", "published_at", "url")
_PUBLIC_STEP_FIELDS = (
    "step",
    "mode",
    "action",
    "tool",
    "forced",
    "status",
    "n_facts",
    "n_rejected",
    "new_evidence",
)
_HIDDEN_OPEN_TEXT_KEYS = frozenset(
    {
        "body",
        "body_text",
        "content",
        "excerpt",
        "excerpt_text",
        "note",
        "notes",
        "quote",
        "quotes",
        "snippet",
        "statement",
        "summary",
        "text",
    }
)

_SENTENCE_END = re.compile(r"[.!?][\"'”’)\]]*(?=\s|$)")
_URL_HOST = re.compile(r"[a-z0-9.-]+")


def _parse_source_url(url: object) -> tuple[Any | None, str]:
    if url is None:
        return None, "missing"
    if not isinstance(url, str):
        return None, "unparseable"
    raw = url.strip()
    if not raw:
        return None, "missing"
    candidate = raw if "://" in raw else f"https://{raw}"
    try:
        parts = urlsplit(candidate)
        host = (parts.hostname or "").casefold().rstrip(".")
    except (UnicodeError, ValueError):
        return None, "unparseable"
    if not host or _URL_HOST.fullmatch(host) is None:
        return None, "unparseable"
    return parts, "parsed"


def _source_status(publisher: object, url: object) -> str:
    if isinstance(publisher, str) and "benzinga" in publisher.casefold():
        return "benzinga"
    parts, state = _parse_source_url(url)
    if state == "unparseable":
        return "unparseable"
    if state == "missing":
        if not isinstance(publisher, str) or not publisher.strip():
            return "ambiguous"
        return "other"
    assert parts is not None
    host = (parts.hostname or "").casefold().rstrip(".")
    if host == "benzinga.com" or host.endswith(".benzinga.com"):
        return "benzinga"
    if any(segment.casefold() == "benzinga" for segment in parts.path.split("/")):
        return "benzinga"
    return "other"


def is_benzinga_source(publisher: object, url: object) -> bool:
    """Whether source text must be hidden as Benzinga or an unsafe source."""
    return _source_status(publisher, url) != "other"


def source_limits_enabled() -> bool:
    return os.getenv("CATALYST_PUBLIC_SOURCE_LIMITS") == "1"


def must_hide_source_text(publisher: object, url: object) -> bool:
    """Whether source text must not be published, including parse failures."""
    return source_limits_enabled() and is_benzinga_source(publisher, url)


def _mask_sentence_periods(text: str) -> str:
    """Replace non-boundary periods without changing string offsets."""
    masked = text
    for abbreviation in _ABBREVIATIONS:
        masked = masked.replace(abbreviation, abbreviation.replace(".", "\x00"))
    return re.sub(r"(?<=\d)\.(?=\d)", "\x00", masked)


def _complete_sentence_ends(text: str) -> list[int]:
    return [match.end() for match in _SENTENCE_END.finditer(_mask_sentence_periods(text))]


def _complete_sentence_count(text: str) -> int:
    return len(_complete_sentence_ends(text))


def _first_complete_sentences(text: str, limit: int) -> str | None:
    """Return the contiguous prefix ending at the ``limit``th complete sentence."""
    if limit <= 0:
        return None
    ends = _complete_sentence_ends(text)
    if not ends:
        return None
    end = ends[min(limit, len(ends)) - 1]
    return text[:end]


def _public_source(source: Mapping[str, Any]) -> dict[str, Any]:
    return {field: deepcopy(source[field]) for field in _PUBLIC_SOURCE_FIELDS if field in source}


def _public_source_without_text(source: Mapping[str, Any]) -> dict[str, Any]:
    public = _public_source(source)
    public.pop("text", None)
    return public


def _source_key(url: str) -> str:
    parts, state = _parse_source_url(url)
    if parts is None or state != "parsed":
        raise ValueError("unparseable source URL")
    host = (parts.hostname or "").casefold().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    elif host.startswith("m."):
        host = host[2:]
    path = parts.path.rstrip("/") or "/"
    return f"{host}{path}"


class _BenzingaBudget:
    def __init__(self) -> None:
        self.used: dict[str, int] = {}
        self.shown: dict[tuple[str, str], str | None] = {}

    def project(self, source: Mapping[str, Any]) -> dict[str, Any] | None:
        if not source_limits_enabled():
            return _public_source(source)
        status = _source_status(source.get("publisher"), source.get("url"))
        if status == "other":
            return _public_source(source)
        if status in {"ambiguous", "unparseable"}:
            return _public_source_without_text(source)

        url = source.get("url")
        if not isinstance(url, str) or not url.strip():
            return None
        try:
            key = _source_key(url)
        except ValueError:
            return _public_source_without_text(source)

        text = source.get("text")
        if not isinstance(text, str) or not text.strip():
            return _public_source(source)
        display_key = (key, text)
        if display_key in self.shown:
            shown = self.shown[display_key]
            if shown is None:
                return _public_source_without_text(source)
            public = _public_source(source)
            public["text"] = shown
            return public

        remaining = 2 - self.used.get(key, 0)
        shown = _first_complete_sentences(text, remaining)
        self.shown[display_key] = shown
        if shown is None:
            return _public_source_without_text(source)
        self.used[key] = self.used.get(key, 0) + _complete_sentence_count(shown)
        public = _public_source(source)
        public["text"] = shown
        return public


def _project_sources(value: Any, budget: _BenzingaBudget) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    projected: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        public = budget.project(item)
        if public is not None:
            projected.append(public)
    return projected


def _project_source_field(value: Any, budget: _BenzingaBudget) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return budget.project(value)


def _project_attribution(value: Any, budget: _BenzingaBudget) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    result = {field: deepcopy(value[field]) for field in _PUBLIC_ATTRIBUTION_FIELDS if field in value}
    quotes = _project_sources(value.get("quotes"), budget)
    if quotes:
        result["quotes"] = quotes
    else:
        result.pop("quotes", None)
    return result


def _project_possible_explanation(value: Any, budget: _BenzingaBudget) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    result = {
        field: deepcopy(value[field])
        for field in _PUBLIC_POSSIBLE_EXPLANATION_FIELDS
        if field in value
    }
    quotes = _project_sources(value.get("quotes"), budget)
    if quotes:
        result["quotes"] = quotes
    else:
        result.pop("quotes", None)
    return result


def _project_leads(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [
        {field: deepcopy(item[field]) for field in _PUBLIC_LEAD_FIELDS if field in item}
        for item in value
        if isinstance(item, Mapping)
    ]


def _project_abstention(value: Any, budget: _BenzingaBudget) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    result: dict[str, Any] = {}
    for field in ("reason_code", "reason_text", "gate_reason_code", "failed_stage"):
        if field in value:
            result[field] = deepcopy(value[field])
    if "details" in value:
        result["details"] = {}
    if "possible_explanation" in value:
        result["possible_explanation"] = _project_possible_explanation(
            value.get("possible_explanation"), budget
        )
    if "leads" in value:
        result["leads"] = _project_leads(value.get("leads"))
    return result


def _scrub_open_text(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _scrub_open_text(item)
            for key, item in value.items()
            if not _is_hidden_open_text_key(key)
        }
    if isinstance(value, list):
        return [_scrub_open_text(item) for item in value]
    return deepcopy(value)


def _is_hidden_open_text_key(key: object) -> bool:
    normalized = str(key).casefold()
    return (
        normalized in _HIDDEN_OPEN_TEXT_KEYS
        or normalized.endswith("_text")
        or "snippet" in normalized
        or "quote" in normalized
        or "body" in normalized
        or "excerpt" in normalized
    )


def _project_board(value: Any) -> list[Any]:
    if not isinstance(value, list):
        return []
    result: list[Any] = []
    for item in value:
        if isinstance(item, Mapping):
            board_item = {key: deepcopy(item[key]) for key in ("kind", "status", "evidence_ids") if key in item}
            result.append(board_item)
        elif isinstance(item, list):
            result.append(deepcopy(item[:2]))
    return result


def _project_hypothesis(value: Any, budget: _BenzingaBudget) -> tuple[Any, bool]:
    if isinstance(value, Mapping):
        if "quotes" in value:
            result = _scrub_open_text(value)
            quotes = _project_sources(value.get("quotes"), budget)
            if quotes:
                result["quotes"] = quotes
                return result, True
            return None, False
        if any(field in value for field in ("text", "publisher", "url", "evidence_id")):
            projected = budget.project(value)
            return (projected, True) if projected is not None else (None, False)
        return _scrub_open_text(value), True
    if isinstance(value, list):
        result: list[Any] = []
        had_source = False
        for item in value:
            if isinstance(item, Mapping) and any(
                field in item for field in ("text", "publisher", "url", "evidence_id", "quotes")
            ):
                had_source = True
                projected, keep = _project_hypothesis(item, budget)
                if keep:
                    result.append(projected)
            else:
                result.append(_scrub_open_text(item))
        if had_source and not result:
            return None, False
        return result, True
    return _scrub_open_text(value), True


def _project_step(step: Mapping[str, Any], budget: _BenzingaBudget) -> dict[str, Any] | None:
    result = {field: deepcopy(step[field]) for field in _PUBLIC_STEP_FIELDS if field in step}
    if "hypothesis" in step:
        hypothesis, keep = _project_hypothesis(step.get("hypothesis"), budget)
        if keep:
            result["hypothesis"] = hypothesis
    if "board_after" in step:
        result["board_after"] = _project_board(step.get("board_after"))
    return result


def project_public_steps(
    value: Any, *, budget: _BenzingaBudget | None = None
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    shared = budget if budget is not None else _BenzingaBudget()
    result: list[dict[str, Any]] = []
    for step in value:
        if not isinstance(step, Mapping):
            continue
        projected = _project_step(step, shared)
        if projected is not None:
            result.append(projected)
    return result


def _project_investigation(value: Any, budget: _BenzingaBudget) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    result = {
        field: deepcopy(value[field])
        for field in ("mode", "stop_reason", "published", "evidence_added")
        if field in value
    }
    if "steps" in value:
        result["steps"] = project_public_steps(value.get("steps"), budget=budget)
    if "board" in value:
        result["board"] = _project_board(value.get("board"))
    return result


def project_public_answer(
    snapshot: Mapping[str, Any], *, budget: _BenzingaBudget | None = None
) -> dict[str, Any]:
    """Reduce an answer snapshot to the public allowlist and Benzinga quote cap."""
    shared = budget if budget is not None else _BenzingaBudget()
    answer = deepcopy(dict(snapshot))
    for field in ("key_sentence", "also_sentence", "event_sentence"):
        if field in answer:
            answer[field] = _project_source_field(answer.get(field), shared)
    for field in ("primary", "secondary"):
        if field in answer:
            answer[field] = _project_attribution(answer.get(field), shared)
    if "sources" in answer:
        answer["sources"] = _project_sources(answer.get("sources"), shared)
    if "abstention" in answer:
        answer["abstention"] = _project_abstention(answer.get("abstention"), shared)
    if "investigation" in answer:
        answer["investigation"] = _project_investigation(answer.get("investigation"), shared)
    for field in ("facts", "web_evidence", "desk_context", "reported_by"):
        if field in answer:
            answer[field] = _scrub_open_text(answer.get(field))
    return RunViewPublicAnswerDTO.model_validate(answer).model_dump(mode="json")


def project_public_run_view(view: RunViewDTO) -> RunViewDTO:
    """Return the non-admin view: no trace/legacy text and only public answer text."""
    payload = view.model_dump(mode="json")
    answer = payload.get("answer")
    payload["answer"] = project_public_answer(answer) if isinstance(answer, Mapping) else None
    payload["trace"] = None
    payload["legacy_text"] = None
    availability = dict(payload.get("availability") or {})
    if availability.get("trace") != "not_applicable":
        availability["trace"] = "not_recorded"
    payload["availability"] = availability
    return RunViewDTO.model_validate(payload)


_PUBLIC_EVENT_PAYLOAD_FIELDS: dict[RunEventType, frozenset[str]] = {
    RunEventType.RUN_ACCEPTED: frozenset({
        "request_identity_digest", "ticker", "trade_date", "workflow_version",
        "model_provider_label", "stream_url",
    }),
    RunEventType.STAGE_STARTED: frozenset({
        "stage", "round", "task_id", "input_artifact_refs", "deadline",
    }),
    RunEventType.EVIDENCE_RETRIEVED: frozenset({
        "task_id", "candidate_evidence_ids", "candidate_count",
        "retrieval_degradation", "latency_ms",
    }),
    RunEventType.EVIDENCE_ASSESSED: frozenset({
        "accepted_evidence_ids", "lead_only_evidence_ids", "rejected_evidence_ids",
        "reason_codes", "gap_ids", "status_ceiling", "decision", "truncated",
    }),
    RunEventType.FOLLOWUP_STARTED: frozenset({
        "batch_id", "action_ids", "gap_ids", "evidence_needs", "time_scopes",
        "research_fingerprints",
    }),
    RunEventType.ANSWER_STARTED: frozenset({
        "writer_stream_id", "validated_claim_plan_ref", "validated_claim_plan_hash",
        "citation_ids", "provisional",
    }),
    RunEventType.ANSWER_COMPLETED: frozenset({
        "writer_stream_id", "final_text_hash", "citation_ids", "usage_tokens_in",
        "usage_tokens_out", "provisional",
    }),
    RunEventType.ASSURANCE_COMPLETED: frozenset({
        "valid", "violations", "final_result_status", "provisional_invalidated",
    }),
    RunEventType.RUN_COMPLETED: frozenset({
        "result_status", "final_output_artifact_ref", "total_latency_ms",
        "total_tokens", "cost", "runtime_identity_ref",
    }),
    RunEventType.RUN_FAILED: frozenset({
        "failure_code", "stage", "retryable", "safe_message",
        "provisional_output_invalidated",
    }),
    RunEventType.RUN_CANCELLED: frozenset({
        "cancellation_reason", "stage", "acknowledgement_mode",
        "provisional_output_invalidated",
    }),
    RunEventType.FACTS_READY: frozenset({"window", "facts", "headline", "truncated"}),
    RunEventType.RETRIEVAL_DONE: frozenset({
        "inventory_count", "included_in_pack", "lead_only", "items", "duration_ms",
        "truncated",
    }),
    RunEventType.ANALYST_DONE: frozenset({
        "duration_ms", "evidence_decisions", "hypotheses", "research_decision",
        "truncated",
    }),
    RunEventType.ANALYST_DECISION: frozenset({
        "kind", "evidence_id", "disposition", "reason_code", "ref", "cause_type",
        "proposed_role", "truncated",
    }),
    RunEventType.CHECKS_DONE: frozenset({"quotes", "gates", "duration_ms", "truncated"}),
    RunEventType.GATES_DONE: frozenset({"gates", "duration_ms", "truncated"}),
    RunEventType.VERIFIER_DONE: frozenset({
        "called", "verdict", "category", "skipped_because", "truncated",
    }),
    RunEventType.ANSWER_READY: frozenset({"answer_snapshot", "provisional", "truncated"}),
    RunEventType.TIER_DECIDED: frozenset({
        "tier", "governing_ref", "overrides", "reason_code", "reason_text",
        "threshold_used", "truncated",
    }),
    RunEventType.INVESTIGATOR_DONE: frozenset({
        "mode", "stop_reason", "steps", "evidence_added", "truncated",
    }),
}
_PUBLIC_RETRIEVAL_ITEM_FIELDS = frozenset({
    "evidence_id", "title", "publisher", "published_at", "url", "phase",
    "content_state",
})
_PUBLIC_ANALYST_DECISION_FIELDS = frozenset({
    "evidence_id", "disposition", "reason_code",
})
_PUBLIC_ANALYST_HYPOTHESIS_FIELDS = frozenset({
    "ref", "cause_type", "proposed_role",
})
_PUBLIC_CHECKED_QUOTE_FIELDS = frozenset({
    "hypothesis_ref", "evidence_id", "rule", "kept",
})
_PUBLIC_CHECK_GATE_FIELDS = frozenset({"gate", "result"})


def _filter_fields(value: Mapping[str, Any], fields: frozenset[str]) -> dict[str, Any]:
    return {
        key: deepcopy(value[key])
        for key in value
        if key in fields
    }


def _filter_list(value: Any, fields: frozenset[str]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [
        _filter_fields(item, fields)
        for item in value
        if isinstance(item, Mapping)
    ]


def _filter_nested_event_fields(
    event_type: RunEventType, data: dict[str, Any]
) -> dict[str, Any]:
    if event_type is RunEventType.RETRIEVAL_DONE:
        data["items"] = _filter_list(
            data.get("items", []), _PUBLIC_RETRIEVAL_ITEM_FIELDS
        )
    elif event_type is RunEventType.ANALYST_DONE:
        data["evidence_decisions"] = _filter_list(
            data.get("evidence_decisions", []), _PUBLIC_ANALYST_DECISION_FIELDS
        )
        data["hypotheses"] = _filter_list(
            data.get("hypotheses", []), _PUBLIC_ANALYST_HYPOTHESIS_FIELDS
        )
    elif event_type is RunEventType.CHECKS_DONE:
        data["quotes"] = _filter_list(
            data.get("quotes", []), _PUBLIC_CHECKED_QUOTE_FIELDS
        )
        data["gates"] = _filter_list(
            data.get("gates", []), _PUBLIC_CHECK_GATE_FIELDS
        )
    elif event_type is RunEventType.GATES_DONE:
        data["gates"] = _filter_list(
            data.get("gates", []), _PUBLIC_CHECK_GATE_FIELDS
        )
    return data


def _filter_public_event_payload(
    event_type: RunEventType,
    raw: Mapping[str, Any],
    *,
    budget: _BenzingaBudget | None = None,
    project_embedded: bool,
) -> dict[str, Any]:
    filtered = _filter_fields(raw, _PUBLIC_EVENT_PAYLOAD_FIELDS[event_type])
    if project_embedded:
        if event_type is RunEventType.ANSWER_READY:
            filtered["answer_snapshot"] = project_public_answer(
                raw.get("answer_snapshot", {}), budget=budget
            )
        elif event_type is RunEventType.INVESTIGATOR_DONE:
            filtered["steps"] = project_public_steps(
                raw.get("steps", []), budget=budget
            )
    return _filter_nested_event_fields(event_type, filtered)


def _prune_model_fields(model: Any, fields: frozenset[str]) -> None:
    for key in list(getattr(model, "__dict__", {})):
        if key not in fields:
            del model.__dict__[key]
    if hasattr(model, "__pydantic_fields_set__"):
        model.__pydantic_fields_set__ &= set(fields)


def _prune_list_models(items: Any, fields: frozenset[str]) -> None:
    if not isinstance(items, (list, tuple)):
        return
    for item in items:
        if isinstance(item, BaseModel):
            _prune_model_fields(item, fields)


def _prune_nested_event_models(event_type: RunEventType, payload: Any) -> None:
    if event_type is RunEventType.RETRIEVAL_DONE:
        _prune_list_models(payload.items, _PUBLIC_RETRIEVAL_ITEM_FIELDS)
    elif event_type is RunEventType.ANALYST_DONE:
        _prune_list_models(
            payload.evidence_decisions, _PUBLIC_ANALYST_DECISION_FIELDS
        )
        _prune_list_models(payload.hypotheses, _PUBLIC_ANALYST_HYPOTHESIS_FIELDS)
    elif event_type is RunEventType.CHECKS_DONE:
        _prune_list_models(payload.quotes, _PUBLIC_CHECKED_QUOTE_FIELDS)
        _prune_list_models(payload.gates, _PUBLIC_CHECK_GATE_FIELDS)
    elif event_type is RunEventType.GATES_DONE:
        _prune_list_models(payload.gates, _PUBLIC_CHECK_GATE_FIELDS)


def _public_event_payload(
    event: PublicRunEvent, *, budget: _BenzingaBudget | None = None
) -> Any | None:
    if event.event_type is RunEventType.ANSWER_DELTA:
        return None
    payload = event.payload
    raw = payload.model_dump(mode="json")
    first_pass = _filter_public_event_payload(
        event.event_type, raw, budget=budget, project_embedded=True
    )
    validated = type(payload).model_validate(first_pass)
    keep_fields = set(_PUBLIC_EVENT_PAYLOAD_FIELDS[event.event_type])
    if event.event_type is RunEventType.ANALYST_DECISION:
        # The model validator reads ``note``; keep its default-null attribute.
        # Its model serializer omits None already, so it stays out of output.
        keep_fields.add("note")
    _prune_model_fields(validated, frozenset(keep_fields))
    _prune_nested_event_models(event.event_type, validated)
    return validated


def _walk_string_paths(value: Any, path: tuple[Any, ...] = ()) -> list[tuple[tuple[Any, ...], str]]:
    found: list[tuple[tuple[Any, ...], str]] = []
    if isinstance(value, str):
        found.append((path, value))
    elif isinstance(value, Mapping):
        for key, item in value.items():
            found.extend(_walk_string_paths(item, path + (key,)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_walk_string_paths(item, path + (index,)))
    return found


def public_event_long_string_violations(
    event: PublicRunEvent, *, limit: int = 200
) -> list[str]:
    """Backstop: long strings may survive only in projected allowlisted fields."""
    projected = _public_event_payload(event)
    if projected is None:
        return []
    projected_dump = projected.model_dump(mode="json")
    allowed_paths = {
        path
        for path, value in _walk_string_paths(projected_dump)
        if isinstance(value, str)
    }
    violations: list[str] = []
    for path, value in _walk_string_paths(event.payload.model_dump(mode="json")):
        if len(value) > limit and path not in allowed_paths:
            location = ".".join(str(part) for part in path)
            violations.append(
                f"{event.event_type.value}.payload.{location}: long string is not public"
            )
    return violations


def _replace_payload(event: PublicRunEvent, payload: Any) -> PublicRunEvent:
    return PublicRunEvent(
        schema_version=event.schema_version,
        event_id=event.event_id,
        run_id=event.run_id,
        sequence=event.sequence,
        event_type=event.event_type,
        emitted_at=event.emitted_at,
        stage=event.stage,
        payload=payload,
        artifact_refs=event.artifact_refs,
    )


def project_public_event(
    event: PublicRunEvent, *, budget: _BenzingaBudget | None = None
) -> PublicRunEvent | None:
    """Project one SSE event for a non-admin reader; ``None`` suppresses it."""
    if event.event_type is RunEventType.ANSWER_DELTA:
        return None
    projected_payload = _public_event_payload(
        event, budget=budget if budget is not None else _BenzingaBudget()
    )
    if projected_payload is None:
        return None
    return _replace_payload(event, projected_payload)


_PUBLIC_FORBIDDEN_ARTIFACT_KEYS = frozenset(
    {
        "context_pack",
        "decision_trace",
        "evidence_detail",
        "llm_exchange",
        "model_judgement",
        "provider_response",
        "raw_response",
        "reasoning",
        "chain_of_thought",
        "rendered_messages",
        "run_provider_accounting",
        "prompt",
        "prompts",
    }
)
_TRACE_STATUS_VALUES = frozenset(
    {"present", "derived", "not_recorded", "not_applicable"}
)
_PUBLIC_FORBIDDEN_OPEN_TEXT_KEYS = frozenset(
    {"summary", "note", "notes", "search_hint"}
)
_BARE_TEXT_KEYS = frozenset(
    {"text", "delta", "delta_text", "snippet", "excerpt", "body", "body_text"}
)


def _is_cnbc_source(publisher: object, url: object) -> bool:
    if isinstance(publisher, str) and "cnbc" in publisher.casefold():
        return True
    raw = url if isinstance(url, str) else ""
    lowered = raw.strip().casefold()
    if "://" not in lowered and lowered.startswith("//"):
        lowered = "https:" + lowered
    path = ""
    try:
        parts = urlsplit(
            lowered if "://" in lowered else f"https://{lowered}"
        )
        host = (parts.hostname or "").casefold().rstrip(".")
        path = parts.path
    except (UnicodeError, ValueError):
        host = ""
    if host == "cnbc.com" or host.endswith(".cnbc.com"):
        return True
    if any(segment.casefold() == "cnbc" for segment in path.split("/")):
        return True
    # Protocol-relative and otherwise host-less inputs still name the host.
    return "//www.cnbc.com" in lowered or "//cnbc.com" in lowered


def _is_benzinga_source(publisher: object, url: object) -> bool:
    return _source_status(publisher, url) == "benzinga"


def _is_source_mapping(value: Mapping[str, Any]) -> bool:
    return isinstance(value.get("text"), str) and (
        "publisher" in value or "url" in value
    )


def _allowed_fact_text(path: str, value: Any) -> bool:
    parts = [part for part in path.replace("[", ".").replace("]", "").split(".") if part]
    return (
        len(parts) >= 2
        and parts[-2:] == ["earnings_fallback", "fact"]
        and isinstance(value, str)
        and len(value) <= 1000
    )


def _source_has_other_text(source: Mapping[str, Any]) -> bool:
    for key, value in source.items():
        normalized = str(key).casefold()
        if normalized != "text" and normalized in _BARE_TEXT_KEYS and value not in (None, ""):
            return True
    return False


def _source_entry_violations(source: Mapping[str, Any], path: str) -> list[str]:
    if not source_limits_enabled():
        return []
    text = source.get("text")
    status = _source_status(source.get("publisher"), source.get("url"))
    if status == "benzinga":
        url = source.get("url")
        if text is not None and (not isinstance(url, str) or not url.strip()):
            return [f"{path}: Benzinga text requires a source URL"]
        return []
    if _is_cnbc_source(source.get("publisher"), source.get("url")):
        if text is not None and isinstance(text, str) and text.strip():
            return [f"{path}.text: CNBC source text is not public"]
        if _source_has_other_text(source):
            return [f"{path}: CNBC source contains non-public text"]
        return []
    if status in {"ambiguous", "unparseable"} and text is not None:
        return [f"{path}.text: source without a parseable URL must not expose text"]
    return []


def _base_projection_violations(value: Any, *, path: str = "$") -> list[str]:
    violations: list[str] = []
    if isinstance(value, Mapping):
        source_mapping = _is_source_mapping(value)
        if source_mapping:
            violations.extend(_source_entry_violations(value, path))
        for key, item in value.items():
            child = f"{path}.{key}"
            normalized = str(key).casefold()
            if (
                normalized == "trace"
                and item is not None
                and not (isinstance(item, str) and item in _TRACE_STATUS_VALUES)
            ):
                violations.append(f"{child}: forbidden public artifact")
            if normalized == "legacy_text" and item is not None:
                violations.append(f"{child}: forbidden public artifact")
            if (
                normalized in _PUBLIC_FORBIDDEN_ARTIFACT_KEYS
                and item is not None
            ):
                violations.append(f"{child}: forbidden public artifact")
            if (
                normalized in _PUBLIC_FORBIDDEN_OPEN_TEXT_KEYS
                and item is not None
            ):
                violations.append(f"{child}: model or investigation text is not public")
            if normalized in _BARE_TEXT_KEYS and item is not None:
                source_text = (
                    normalized == "text"
                    and source_mapping
                    and isinstance(item, str)
                )
                fact_text = normalized == "text" and _allowed_fact_text(path, item)
                if not (source_text or fact_text):
                    violations.append(f"{child}: text node is not a source quote")
            violations.extend(_base_projection_violations(item, path=child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            violations.extend(
                _base_projection_violations(item, path=f"{path}[{index}]")
            )
    return violations


def _iter_source_mappings(value: Any, path: str = "$") -> list[tuple[str, Mapping[str, Any]]]:
    found: list[tuple[str, Mapping[str, Any]]] = []
    if isinstance(value, Mapping):
        if _is_source_mapping(value):
            found.append((path, value))
        for key, item in value.items():
            found.extend(_iter_source_mappings(item, path=f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_iter_source_mappings(item, path=f"{path}[{index}]"))
    return found


def _benzinga_excerpt_violations(text: str, path: str) -> list[str]:
    stripped = text.strip()
    if not stripped:
        return []
    count = _complete_sentence_count(stripped)
    if count not in {1, 2}:
        return [f"{path}.text: Benzinga excerpt must be one or two complete sentences"]
    ends = _complete_sentence_ends(stripped)
    if not ends or ends[-1] != len(stripped):
        return [f"{path}.text: Benzinga excerpt does not end at a sentence boundary"]
    first_sentence = stripped[: ends[0]]
    if any(marker in first_sentence for marker in (":", " - ", "—", "–")):
        return [f"{path}.text: Benzinga excerpt has a preamble before its first sentence"]
    return []


def _normalize_whitespace(text: str) -> str:
    return " ".join(text.split())


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    ends = _complete_sentence_ends(text)
    spans: list[tuple[int, int]] = []
    previous_end = 0
    for end in ends:
        start = previous_end
        while start < len(text) and text[start].isspace():
            start += 1
        if start < end:
            spans.append((start, end))
        previous_end = end
    return spans


def _is_contiguous_sentence_run(excerpt: str, original: str) -> bool:
    excerpt_text = _normalize_whitespace(excerpt)
    original_text = _normalize_whitespace(original)
    if not excerpt_text or not original_text:
        return False
    spans = _sentence_spans(original_text)
    if not spans:
        return False
    starts = {start for start, _ in spans}
    ends = {end for _, end in spans}
    position = 0
    while True:
        index = original_text.find(excerpt_text, position)
        if index < 0:
            return False
        end = index + len(excerpt_text)
        if index in starts and end in ends:
            return True
        position = index + 1


def _grouped_benzinga_violations(
    entries: list[tuple[str, Mapping[str, Any]]],
    original_texts: Mapping[str, str] | None = None,
) -> list[str]:
    if not source_limits_enabled():
        return []
    grouped: dict[str, list[tuple[str, str]]] = {}
    violations: list[str] = []
    for path, source in entries:
        text = source.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        url = source.get("url")
        if not isinstance(url, str) or not url.strip():
            violations.append(f"{path}: Benzinga text requires a source URL")
            continue
        try:
            key = _source_key(url)
        except ValueError:
            violations.append(f"{path}: Benzinga text has an unparseable source URL")
            continue
        excerpt_violations = _benzinga_excerpt_violations(text, path)
        if excerpt_violations:
            violations.extend(excerpt_violations)
            continue
        if original_texts is not None:
            original = original_texts.get(key)
            if not isinstance(original, str) or not original.strip():
                violations.append(
                    f"{path}.text: Benzinga original article text is required"
                )
                continue
            if not _is_contiguous_sentence_run(text, original):
                violations.append(
                    f"{path}.text: Benzinga excerpt is not a contiguous run of complete sentences in the article"
                )
                continue
        grouped.setdefault(key, []).append(
            (path, _normalize_whitespace(text.strip()))
        )

    for key, items in grouped.items():
        unique: list[str] = []
        for _, text in items:
            if text not in unique:
                unique.append(text)
        maximal = [
            text
            for text in unique
            if not any(text != other and other.startswith(text) for other in unique)
        ]
        if not maximal:
            continue
        paths = ", ".join(path for path, _ in items[:3])
        if original_texts is None:
            total_sentences = sum(_complete_sentence_count(text) for text in maximal)
            if total_sentences > 2:
                violations.append(
                    f"{paths}: Benzinga excerpts for one article exceed two sentences"
                )
            continue
        original = original_texts.get(key)
        if not isinstance(original, str) or not original.strip():
            violations.append(f"{paths}: Benzinga original article text is required")
            continue
        total_sentences = sum(_complete_sentence_count(text) for text in maximal)
        if total_sentences > 2:
            violations.append(
                f"{paths}: Benzinga excerpts for one article exceed two sentences"
            )
            continue
        import itertools

        if not any(
            _is_contiguous_sentence_run(" ".join(order), original)
            for order in itertools.permutations(maximal)
        ):
            violations.append(
                f"{paths}: Benzinga excerpts for one article are not one contiguous sentence run"
            )
    return violations


def public_projection_violations_many(
    documents: list[tuple[Any, str]] | tuple[tuple[Any, str], ...],
    *,
    original_texts: Mapping[str, str] | None = None,
) -> list[str]:
    """Validate public rules across multiple projected documents.

    Benzinga excerpts are grouped by article URL across the whole set, so a
    view and its replay events cannot each receive a separate two-sentence
    allowance.
    """
    violations: list[str] = []
    benzinga_entries: list[tuple[str, Mapping[str, Any]]] = []
    for value, path in documents:
        violations.extend(_base_projection_violations(value, path=path))
        benzinga_entries.extend(
            (source_path, source)
            for source_path, source in _iter_source_mappings(value, path=path)
            if _is_benzinga_source(source.get("publisher"), source.get("url"))
        )
    violations.extend(
        _grouped_benzinga_violations(benzinga_entries, original_texts)
    )
    return violations


def public_projection_violations(value: Any, *, path: str = "$") -> list[str]:
    """Return public-rule violations in one already-projected value."""
    return public_projection_violations_many([(value, path)])


class PublicEventProjector:
    """A connection-scoped projector with one shared Benzinga budget."""

    def __init__(self) -> None:
        self.budget = _BenzingaBudget()

    def __call__(self, event: PublicRunEvent) -> PublicRunEvent | None:
        return project_public_event(event, budget=self.budget)


__all__ = [
    "PublicEventProjector",
    "is_benzinga_source",
    "must_hide_source_text",
    "project_public_answer",
    "public_event_long_string_violations",
    "project_public_event",
    "public_projection_violations",
    "public_projection_violations_many",
    "project_public_run_view",
    "project_public_steps",
]
