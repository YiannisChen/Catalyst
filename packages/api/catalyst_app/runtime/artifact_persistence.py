"""Durable run artifacts: context packs and result artifacts written to the runtime DB.

Moved verbatim from composition.py (P3); composition.py re-exports these names.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from catalyst_agents.runtime.pack_persistence import (
    PersistedPackPair,
    PersistedPackRefs,
)

from catalyst_app.events import RunEventType, StageStartedPayload
from catalyst_app.persistence.connect import open_rw
from catalyst_app.persistence.events import ArtifactPayload, EventRepository


class RunArtifactsPackPersistence:
    """Production ``PackPersistence`` over the app-owned run_artifacts rows."""

    def __init__(
        self,
        *,
        db_path: str | Path,
        events: EventRepository,
        run_id: str,
    ) -> None:
        self.db_path = Path(db_path)
        self.events = events
        self.run_id = run_id

    def persist_pack_and_render(
        self,
        *,
        run_id: str,
        pack: Any | None,
        pack_sha256: str,
        rendered_messages: tuple[Any, ...],
        rendered_messages_sha256: str,
        prompt_template_version: str,
        prompt_template_sha256: str,
    ) -> PersistedPackRefs:
        import hashlib

        from catalyst_agents.attribution.context_pack_builder import (
            RenderMessage,
            canonical_context_pack_json,
        )

        # Canonical verification before commit (M6 corrective): the declared
        # hashes must match the exact serialized pack/render contract and the
        # prompt-template identity must match the pack; run_id must match the
        # store's run_id. Mismatches are rejected before anything is visible.
        if run_id != self.run_id:
            raise ValueError(
                f"persist run_id {run_id!r} does not match store run_id "
                f"{self.run_id!r}"
            )
        if pack is not None:
            if pack.run_id != self.run_id:
                raise ValueError(
                    f"pack run_id {pack.run_id!r} does not match store run_id "
                    f"{self.run_id!r}"
                )
            expected_pack_sha256 = hashlib.sha256(
                canonical_context_pack_json(
                    pack.model_dump(mode="json", exclude={"context_pack_sha256"})
                )
            ).hexdigest()
            if pack_sha256 != expected_pack_sha256:
                raise ValueError(
                    "declared pack_sha256 does not match the canonical pack contract"
                )
            if pack.context_pack_sha256 != expected_pack_sha256:
                raise ValueError(
                    "pack.context_pack_sha256 does not match its canonical "
                    "serialization"
                )
            if prompt_template_version != pack.prompt_template_version:
                raise ValueError(
                    "prompt-template version does not match the pack identity"
                )
            if prompt_template_sha256 != pack.prompt_template_sha256:
                raise ValueError(
                    "prompt-template hash does not match the pack identity"
                )
        expected_render_sha256 = hashlib.sha256(
            canonical_context_pack_json(
                [
                    RenderMessage.model_validate(message).model_dump(mode="json")
                    if not isinstance(message, RenderMessage)
                    else message.model_dump(mode="json")
                    for message in rendered_messages
                ]
            )
        ).hexdigest()
        if rendered_messages_sha256 != expected_render_sha256:
            raise ValueError(
                "rendered_messages_sha256 does not match the exact normalized "
                "rendered messages"
            )

        # Persist round 1 and round 2 using the actual pack round so the
        # artifact identity is stable and never derived from event_seq.
        round_no = pack.round if pack is not None else 1
        pack_artifact_id = f"pack:{run_id}:{round_no}"
        render_artifact_id = f"render:{run_id}:{round_no}"
        pack_payload = (
            pack.model_dump(mode="json") if pack is not None else {"round": round_no}
        )
        messages_payload = [
            {"role": message.role, "content": message.content}
            for message in rendered_messages
        ]
        seq = self.events.append(
            run_id=run_id,
            event_type=RunEventType.STAGE_STARTED,
            stage="CONTEXT_PACK_BUILD",
            payload=StageStartedPayload(
                stage="context_pack_build", round=round_no
            ),
            artifact_payloads=[
                ArtifactPayload(
                    artifact_id=pack_artifact_id,
                    artifact_type="context_pack",
                    payload=pack_payload,
                ),
                ArtifactPayload(
                    artifact_id=render_artifact_id,
                    artifact_type="rendered_messages",
                    payload={
                        "schema_version": "v1",
                        "messages": messages_payload,
                    },
                ),
            ],
        )
        del seq
        return PersistedPackRefs(
            run_id=run_id,
            pack_artifact_id=pack_artifact_id,
            rendered_messages_artifact_id=render_artifact_id,
            pack_sha256=pack_sha256,
            rendered_messages_sha256=rendered_messages_sha256,
            prompt_template_version=prompt_template_version,
            prompt_template_sha256=prompt_template_sha256,
        )

    def load_pair(self, *, run_id: str) -> PersistedPackPair | None:
        from catalyst_agents.attribution.context_pack import EvidenceAnalystContextPack
        from catalyst_agents.attribution.context_pack_builder import RenderMessage

        with open_rw(self.db_path) as conn:
            pack_row = conn.execute(
                "SELECT artifact_id, payload_hash, payload_json FROM run_artifacts"
                " WHERE run_id = ? AND artifact_type = 'context_pack'"
                " ORDER BY event_seq DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            render_row = conn.execute(
                "SELECT artifact_id, payload_hash, payload_json FROM run_artifacts"
                " WHERE run_id = ? AND artifact_type = 'rendered_messages'"
                " ORDER BY event_seq DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        if pack_row is None or render_row is None:
            return None
        pack_payload = json.loads(pack_row["payload_json"])
        render_payload = json.loads(render_row["payload_json"])
        pack = EvidenceAnalystContextPack.model_validate(pack_payload)
        messages = tuple(
            RenderMessage(role=item["role"], content=item["content"])
            for item in render_payload.get("messages", ())
        )
        refs = PersistedPackRefs(
            run_id=run_id,
            pack_artifact_id=pack_row["artifact_id"],
            rendered_messages_artifact_id=render_row["artifact_id"],
            pack_sha256=pack.context_pack_sha256,
            rendered_messages_sha256=pack.rendered_messages_sha256,
            prompt_template_version=pack.prompt_template_version,
            prompt_template_sha256=pack.prompt_template_sha256,
        )
        return PersistedPackPair(
            run_id=run_id,
            pack=pack,
            rendered_messages=messages,
            refs=refs,
        )

    def persist_artifact(self, *, run_id: str, artifact_id: str, artifact_type: str, payload: dict) -> None:
        if run_id != self.run_id:
            raise ValueError("artifact run_id does not match the run-scoped persistence boundary")
        self.events.append(
            run_id=run_id,
            event_type=RunEventType.STAGE_STARTED,
            stage=artifact_type.upper(),
            payload=StageStartedPayload(stage=artifact_type),
            artifact_payloads=[ArtifactPayload(artifact_id=artifact_id, artifact_type=artifact_type,
                                               payload=payload)],
        )
