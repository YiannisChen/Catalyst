"""M1-4: sealed model/code/case identity and T4/user-smoke input tests."""

from __future__ import annotations

import dataclasses

import pytest

from catalyst_eval.baseline.model_identity import (
    CasePackIdentity,
    ModelIdentity,
    resolve_model_identity,
)

AUDITED_T4_CASE_PACK_ID = (
    "579844605892bd2a99b4287fb469c8800d48a9ec552300504ebde9c548b9076d"
)


def test_model_identity_detects_app_smoke_split():
    identity = resolve_model_identity(env={})
    assert isinstance(identity, ModelIdentity)
    assert dataclasses.is_dataclass(identity)
    assert identity.__dataclass_params__.frozen
    # Repo-audited fact: app default gemini-2.5-flash-nothink != smoke deepseek-v4-flash.
    assert identity.app_default_model == "gemini-2.5-flash-nothink"
    assert identity.smoke_default_provider == "deepseek"
    assert identity.smoke_default_model_id == "deepseek-v4-flash"
    assert identity.defaults_are_split is True


def test_env_default_model_is_captured_without_overriding_split():
    identity = resolve_model_identity(env={"CATALYST_DEFAULT_MODEL": "qwen3.6-flash"})
    assert identity.env_default_model == "qwen3.6-flash"
    assert identity.defaults_are_split is True
    assert identity.app_default_model == "gemini-2.5-flash-nothink"


def test_missing_env_default_model_is_none():
    identity = resolve_model_identity(env={})
    assert identity.env_default_model is None


def test_case_pack_identity_is_sealed():
    identity = resolve_model_identity(env={})
    case_pack = identity.case_pack
    assert isinstance(case_pack, CasePackIdentity)
    assert case_pack.t4_case_pack_id == AUDITED_T4_CASE_PACK_ID
    assert case_pack.t4_case_count == 10
    assert case_pack.t4_ordered_case_ids == (
        "g006", "g013", "g017", "g024", "g041",
        "g007", "h001", "h004", "h005", "h007",
    )
    assert case_pack.user_smoke_case_ids == ("g006", "g007", "h004")
