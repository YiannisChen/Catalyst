"""Sealed model/code/case identity for the M1 baseline (M1-4).

Resolves the production app default model, the post-import user-smoke default
provider/model, the optional ``CATALYST_DEFAULT_MODEL`` env override, and the
manager-approved T4/user-smoke case-pack inputs. The app/smoke default split
is reported explicitly and never silently resolved.
"""

from __future__ import annotations

import dataclasses
import os
from typing import Mapping

MODEL_IDENTITY_SCHEMA = "model_identity_v1"

# Literal audited T4/user-smoke input contracts (mirrors the production
# ApprovedT4Contract / USER_SMOKE_CASE_IDS at seal time).
AUDITED_T4_CASE_PACK_ID = (
    "579844605892bd2a99b4287fb469c8800d48a9ec552300504ebde9c548b9076d"
)
AUDITED_T4_CASE_COUNT = 10
AUDITED_T4_ORDERED_CASE_IDS = (
    "g006", "g013", "g017", "g024", "g041",
    "g007", "h001", "h004", "h005", "h007",
)
AUDITED_USER_SMOKE_CASE_IDS = ("g006", "g007", "h004")


@dataclasses.dataclass(frozen=True)
class CasePackIdentity:
    """Sealed T4 and user-smoke case-pack inputs."""

    t4_case_pack_id: str
    t4_case_count: int
    t4_ordered_case_ids: tuple[str, ...]
    user_smoke_case_ids: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class ModelIdentity:
    """Sealed model identity facts; the split is reported, never resolved."""

    app_default_model: str | None
    smoke_default_provider: str | None
    smoke_default_model_id: str | None
    env_default_model: str | None
    defaults_are_split: bool
    case_pack: CasePackIdentity

    def to_dict(self) -> dict:
        return {
            "schema_version": MODEL_IDENTITY_SCHEMA,
            "app_default_model": self.app_default_model,
            "smoke_default_provider": self.smoke_default_provider,
            "smoke_default_model_id": self.smoke_default_model_id,
            "env_default_model": self.env_default_model,
            "defaults_are_split": self.defaults_are_split,
            "case_pack": dataclasses.asdict(self.case_pack),
        }


def resolve_model_identity(*, env: Mapping[str, str] | None = None) -> ModelIdentity:
    """Resolve model defaults and sealed case-pack inputs.

    App/smoke imports are lazy so ``catalyst_eval`` can load without forcing a
    production app import at module import time.
    """
    resolved_env = dict(os.environ if env is None else env)

    from catalyst_app.llm_factory import DEFAULT_MODEL
    from catalyst_eval.post_import.t4_evidence import APPROVED_T4_CONTRACT
    from catalyst_eval.post_import.user_smoke import (
        DEFAULT_MODEL_ID,
        DEFAULT_PROVIDER,
        USER_SMOKE_CASE_IDS,
    )

    app_default_model = DEFAULT_MODEL
    smoke_default_provider = DEFAULT_PROVIDER
    smoke_default_model_id = DEFAULT_MODEL_ID
    env_default_model = resolved_env.get("CATALYST_DEFAULT_MODEL") or None

    return ModelIdentity(
        app_default_model=app_default_model,
        smoke_default_provider=smoke_default_provider,
        smoke_default_model_id=smoke_default_model_id,
        env_default_model=env_default_model,
        defaults_are_split=app_default_model != smoke_default_model_id,
        case_pack=CasePackIdentity(
            t4_case_pack_id=APPROVED_T4_CONTRACT.approved_case_pack_id,
            t4_case_count=APPROVED_T4_CONTRACT.expected_case_count,
            t4_ordered_case_ids=tuple(APPROVED_T4_CONTRACT.ordered_case_ids),
            user_smoke_case_ids=tuple(USER_SMOKE_CASE_IDS),
        ),
    )


__all__ = [
    "AUDITED_T4_CASE_PACK_ID",
    "CasePackIdentity",
    "MODEL_IDENTITY_SCHEMA",
    "ModelIdentity",
    "resolve_model_identity",
]
