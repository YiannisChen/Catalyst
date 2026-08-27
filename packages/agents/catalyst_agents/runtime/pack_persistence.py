"""M4-7: agents-owned ContextPack persistence protocol (Final TSD §7).

The authoritative record of an Analyst call is the immutable pair of the
exact serialized ``EvidenceAnalystContextPack`` (with ``context_pack_sha256``)
and the exact normalized rendered model messages (plus template identity).
Both artifacts commit before a provider request is dispatched; a rebuild is
valid only as a replay assertion reproducing both hashes; missing artifacts
mean the run is non-replayable and reconstruction never becomes authority.

Ownership: M4 provides the protocol and a test-only fixture store. The
production ``run_artifacts`` write envelope is app-owned in M6; agents never
write V1.1 run_artifact rows here and never touch ``trace/artifacts.py``.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol

from catalyst_agents.attribution.context_pack import EvidenceAnalystContextPack
from catalyst_agents.attribution.context_pack_builder import (
    RenderMessage,
    canonical_context_pack_json,
)


class PackNotPersistedError(RuntimeError):
    """The persisted pair is missing; the run is non-replayable and a provider
    request must not be dispatched."""


@dataclass(frozen=True)
class PersistedPackRefs:
    """Immutable references returned only after both artifacts commit."""

    run_id: str
    pack_artifact_id: str
    rendered_messages_artifact_id: str
    pack_sha256: str
    rendered_messages_sha256: str
    prompt_template_version: str
    prompt_template_sha256: str


@dataclass(frozen=True)
class PersistedPackPair:
    """The committed immutable pair used by the provider dispatch gate."""

    run_id: str
    pack: EvidenceAnalystContextPack | None
    rendered_messages: tuple[RenderMessage, ...]
    refs: PersistedPackRefs


class PackPersistence(Protocol):
    """Agents-owned persistence seam; M6 app supplies the production envelope."""

    def persist_pack_and_render(
        self,
        *,
        run_id: str,
        pack: EvidenceAnalystContextPack | None,
        pack_sha256: str,
        rendered_messages: tuple[RenderMessage, ...],
        rendered_messages_sha256: str,
        prompt_template_version: str,
        prompt_template_sha256: str,
    ) -> PersistedPackRefs:
        """Persist both immutable artifacts; return refs only after both commit."""
        ...

    def load_pair(self, *, run_id: str) -> PersistedPackPair | None:
        ...


def require_committed_pair(
    persistence: PackPersistence, *, run_id: str
) -> PersistedPackPair:
    """Provider dispatch gate: never dispatch before the pair is committed."""
    pair = persistence.load_pair(run_id=run_id)
    if pair is None:
        raise PackNotPersistedError(
            f"run {run_id!r} has no persisted pack/render pair; provider "
            "dispatch is prohibited and the run is non-replayable"
        )
    return pair


def replay_asserts_pair(
    refs: PersistedPackRefs,
    *,
    pack_sha256: str,
    rendered_messages: tuple[RenderMessage, ...],
    rendered_messages_sha256: str,
    prompt_template_sha256: str,
) -> None:
    """Replay verification only: reconstruction never becomes authority."""
    if pack_sha256 != refs.pack_sha256:
        raise ValueError(
            "replay pack hash mismatch: reconstruction does not match the "
            "persisted pack"
        )
    if rendered_messages_sha256 != refs.rendered_messages_sha256:
        raise ValueError(
            "replay rendered-messages hash mismatch: reconstruction does not "
            "match the persisted messages"
        )
    if prompt_template_sha256 != refs.prompt_template_sha256:
        raise ValueError(
            "replay prompt-template hash mismatch: reconstruction does not "
            "match the persisted template identity"
        )


def _rendered_messages_sha256(messages: tuple[RenderMessage, ...]) -> str:
    return hashlib.sha256(
        canonical_context_pack_json(
            [message.model_dump(mode="json") for message in messages]
        )
    ).hexdigest()


class InMemoryPackStore:
    """M4 test-only fixture store.

    The production ``run_artifacts`` write envelope is M6 app-owned; this
    store exists only for FAST tests of the contract and is never a production
    artifact store.
    """

    def __init__(self) -> None:
        self._pairs: dict[str, PersistedPackPair] = {}
        self._next_id = 0

    def persist_pack_and_render(
        self,
        *,
        run_id: str,
        pack: EvidenceAnalystContextPack | None = None,
        pack_sha256: str,
        rendered_messages: tuple[RenderMessage, ...],
        rendered_messages_sha256: str,
        prompt_template_version: str,
        prompt_template_sha256: str,
    ) -> PersistedPackRefs:
        # Both artifacts must be exact before either is visible.
        if len(rendered_messages_sha256) != 64 or len(pack_sha256) != 64:
            raise ValueError("persisted hashes must be SHA-256 hex digests")
        expected_render = _rendered_messages_sha256(rendered_messages)
        if expected_render != rendered_messages_sha256:
            raise ValueError(
                "rendered messages artifact does not match its declared hash"
            )
        self._next_id += 1
        refs = PersistedPackRefs(
            run_id=run_id,
            pack_artifact_id=f"pack:{run_id}:{self._next_id}",
            rendered_messages_artifact_id=f"render:{run_id}:{self._next_id}",
            pack_sha256=pack_sha256,
            rendered_messages_sha256=rendered_messages_sha256,
            prompt_template_version=prompt_template_version,
            prompt_template_sha256=prompt_template_sha256,
        )
        self._pairs[run_id] = PersistedPackPair(
            run_id=run_id,
            pack=pack,
            rendered_messages=tuple(rendered_messages),
            refs=refs,
        )
        return refs

    def load_pair(self, *, run_id: str) -> PersistedPackPair | None:
        return self._pairs.get(run_id)


__all__ = [
    "InMemoryPackStore",
    "PackNotPersistedError",
    "PackPersistence",
    "PersistedPackPair",
    "PersistedPackRefs",
    "replay_asserts_pair",
    "require_committed_pair",
]
