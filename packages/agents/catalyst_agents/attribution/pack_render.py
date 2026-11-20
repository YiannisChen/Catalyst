"""Prompt renderers for immutable context-pack drafts."""
from __future__ import annotations

from typing import Callable

from catalyst_agents.attribution.context_pack import EvidenceAnalystContextPack
from catalyst_agents.attribution.context_pack_builder import (
    ContextPackFinalizer,
    PackedContextDraft,
    RenderMessage,
    canonical_context_pack_json,
)
from catalyst_agents.runtime.token_budget import TokenCounter


def _citable_inventory_line(draft: PackedContextDraft) -> str:
    """Citable inventory is ``included_evidence_ids`` only.

    METADATA_ONLY / TITLE_ONLY / excluded rows may exist in
    ``evidence_inventory`` as identity records, but they are not citable and
    must not be labeled ``inventory=``. An empty citable set is explicit
    ``inventory=NONE``.
    """
    if not draft.included_evidence_ids:
        return "inventory=NONE"
    return "inventory=" + ",".join(draft.included_evidence_ids)


def _metadata_only_identities_line(draft: PackedContextDraft) -> str | None:
    """Non-citable METADATA_ONLY identities, never labeled as inventory."""
    included = set(draft.included_evidence_ids)
    ids = tuple(
        item.evidence_id
        for item in draft.evidence_inventory
        if item.content_state == "METADATA_ONLY" and item.evidence_id not in included
    )
    if not ids:
        return None
    return "metadata_only_identities=" + ",".join(ids)


def _title_only_identities_line(draft: PackedContextDraft) -> str | None:
    """Non-citable TITLE_ONLY identities, never labeled as inventory."""
    included = set(draft.included_evidence_ids)
    ids = tuple(
        item.evidence_id
        for item in draft.evidence_inventory
        if item.content_state == "TITLE_ONLY" and item.evidence_id not in included
    )
    if not ids:
        return None
    return "title_only_identities=" + ",".join(ids)


def foundation_renderer(draft: PackedContextDraft) -> tuple[RenderMessage, ...]:
    """Deterministic fixture renderer; never includes context_pack_sha256."""
    messages = [
        RenderMessage(
            role="system",
            content="observation="
            + canonical_context_pack_json(
                draft.observation.model_dump(mode="json")
            ).decode("utf-8"),
        ),
        RenderMessage(
            role="system",
            content=_citable_inventory_line(draft),
        ),
        RenderMessage(
            role="system",
            content="coverage="
            + canonical_context_pack_json(
                draft.coverage_summary.model_dump(mode="json")
            ).decode("utf-8"),
        ),
    ]
    extra = _metadata_only_identities_line(draft)
    if extra is not None:
        messages.append(RenderMessage(role="system", content=extra))
    title_extra = _title_only_identities_line(draft)
    if title_extra is not None:
        messages.append(RenderMessage(role="system", content=title_extra))
    return tuple(messages)


def evidence_body_renderer(draft: PackedContextDraft) -> tuple[RenderMessage, ...]:
    """Foundation render plus the exact packed bodies for citable evidence."""
    messages = list(foundation_renderer(draft))
    if not draft.included_evidence_ids:
        return tuple(messages)

    inventory_by_id = {item.evidence_id: item for item in draft.evidence_inventory}
    blocks: list[str] = []
    for evidence_id in draft.included_evidence_ids:
        item = inventory_by_id[evidence_id]
        excerpt = item.excerpt_text or "<EMPTY>"
        blocks.append(
            f"[e:{evidence_id}]\n"
            f"role={item.evidence_role} "
            f"class={item.source_class.value} "
            f"state={item.content_state} "
            f"available_at={item.eligible_at.isoformat()} "
            f"section={item.section_key or '-'}\n"
            f"text: {excerpt}"
        )
    messages.insert(
        2,
        RenderMessage(
            role="system",
            content="evidence_bodies=\n" + "\n".join(blocks),
        ),
    )
    return tuple(messages)


def critic_prefix_renderer(draft: PackedContextDraft) -> tuple[RenderMessage, ...]:
    """A1 raw critic-prefix block: one bounded 600-char prefix per included
    evidence unit (``critic_prefix_600chars_v1``). The Analyst sees a raw
    prefix block instead of the structured ContextPack render."""
    blocks: list[str] = []
    for item in draft.evidence_inventory:
        if item.evidence_id not in draft.included_evidence_ids:
            continue
        excerpt = item.excerpt_text or ""
        prefix = excerpt[:600]
        blocks.append(f"evidence:{item.evidence_id}:{prefix}")
    content = "critic_prefix=" + "|".join(blocks)
    return (RenderMessage(role="system", content=content),)


def renderer_for_packing(
    draft: PackedContextDraft,
) -> Callable[[PackedContextDraft], tuple[RenderMessage, ...]]:
    """A1 behavioral seam: select the renderer for the draft's packing policy.

    ``evidence_body_v1`` renders the full ContextPack plus packed evidence
    bodies; ``critic_prefix_600chars_v1`` renders the raw critic-prefix block;
    every other version keeps the legacy full ContextPack render. ``None``
    never reaches this function because the draft always carries an explicit
    packing policy version.
    """
    if draft.packing_policy_version == "evidence_body_v1":
        return evidence_body_renderer
    if draft.packing_policy_version == "critic_prefix_600chars_v1":
        return critic_prefix_renderer
    return foundation_renderer


def finalize_with_render(
    draft: PackedContextDraft,
    *,
    template_bytes: bytes,
    template_version: str,
    token_counter: TokenCounter,
) -> tuple[EvidenceAnalystContextPack, tuple[RenderMessage, ...]]:
    """Render once, then finalize the pack against those exact messages."""
    messages = renderer_for_packing(draft)(draft)
    finalizer = ContextPackFinalizer(
        template_bytes=template_bytes,
        template_version=template_version,
        token_counter=token_counter,
    )
    return finalizer.finalize(draft, messages), messages
