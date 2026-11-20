"""Attribution policy switches, resolved once and recorded in the run manifest.

Production defaults (plan doc §16–§19): the same-event verifier is on and the
corrective round is off. ``CATALYST_PRIMARY_VERIFIER=0`` turns the verifier off.
``CATALYST_SKIP_CORRECTIVE=0`` still records the corrective round as enabled;
the round itself was removed and requesting it fails closed. The values a run
used are bound into its manifest (``RunManifest.attribution_policy``), so a
run's behavior is auditable from its manifest alone.
"""
from __future__ import annotations

import hashlib
import os
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict, model_serializer

from catalyst_agents.engine.rules import NAMES, V2Rules

EvidenceWindowPolicy = Literal["pre_close_v1", "event_time_v1", "event_time_v2", "event_time_p1", "desk_cascade_v1"]
WebEvidencePolicy = Literal[
    "off", "cache_code3", "cache_code3_why", "cache_code3_why_roundups", "roundups_only", "discovery_v2"
]
GAP_MIN_ABS_PCT = 2.0
GAP_CLOSE_RETURN_SHARE = 0.6


DEFAULT_EVIDENCE_WINDOW_POLICY: EvidenceWindowPolicy = "desk_cascade_v1"


class AttributionPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    verifier_enabled: bool
    # Version and prompt hash of the verifier; None when it is off.
    verifier_version: str | None = None
    verifier_prompt_sha256: str | None = None
    corrective_enabled: bool
    quote_rules_version: str
    force_full: bool = False
    evidence_window_policy: EvidenceWindowPolicy = "pre_close_v1"
    web_evidence: WebEvidencePolicy = "off"
    web_on_demand: bool = False
    # Desk cascade records both frozen verifier identities. Optional fields are
    # omitted for every existing policy to preserve their manifest bytes.
    stage1_verifier_version: str | None = None
    stage1_verifier_prompt_sha256: str | None = None
    stage2_verifier_version: str | None = None
    stage2_verifier_prompt_sha256: str | None = None
    desk_context_version: str | None = None
    # Stage-2 investigation for desk_cascade_v1: a fixed checklist (arm B) or the investigator (arm C).
    investigator_mode: Literal["checklist", "agent", "agent_v2", "diagnostic_retry", "agent_v3"] | None = None
    investigator_version: str | None = None
    v2_rules: str | None = None

    @model_serializer(mode="wrap")
    def _omit_default_evidence_window_policy(self, handler):
        data = handler(self)
        if self.evidence_window_policy == "pre_close_v1":
            data.pop("evidence_window_policy", None)
        if self.web_evidence == "off":
            data.pop("web_evidence", None)
        if not self.web_on_demand:
            data.pop("web_on_demand", None)
        for name in ("stage1_verifier_version", "stage1_verifier_prompt_sha256",
                     "stage2_verifier_version", "stage2_verifier_prompt_sha256", "desk_context_version",
                     "investigator_mode", "investigator_version"):
            if data.get(name) is None:
                data.pop(name, None)
        if self.v2_rules is None:
            data.pop("v2_rules", None)
        return data


def resolve_attribution_policy(
    env: Mapping[str, str] | None = None, *, force_full: bool = False,
    evidence_window_policy: EvidenceWindowPolicy | None = None,
    web_evidence: WebEvidencePolicy | None = None,
    web_on_demand: bool | None = None,
    investigator_mode: str | None = None,
    v2_rules: str | None = None,
) -> AttributionPolicy:
    from catalyst_agents.attribution.quotes import QUOTE_RULES_VERSION
    from catalyst_agents.attribution.verifier import (
        EVENT_TIME_P1_PROMPT,
        EVENT_TIME_P1_VERIFIER_VERSION,
        EVENT_TIME_PROMPT,
        EVENT_TIME_V2_PROMPT,
        EVENT_TIME_V2_VERIFIER_VERSION,
        EVENT_TIME_VERIFIER_VERSION,
        PROMPT,
        VERIFIER_VERSION,
    )

    env = os.environ if env is None else env
    selected_v2_rules = V2Rules.parse(v2_rules if v2_rules is not None else env.get("CATALYST_V2_RULES"))
    canonical_v2_rules = ",".join(name for name in NAMES if getattr(selected_v2_rules, name)) or None
    # The production default is A′ (desk_cascade_v1 with discovery_v2 web rows), the best
    # measured publisher; pre_close_v1 (P) stays selectable for baselines.
    selected_window_policy = evidence_window_policy or env.get(
        "CATALYST_EVIDENCE_WINDOW_POLICY", DEFAULT_EVIDENCE_WINDOW_POLICY
    )
    if selected_window_policy not in ("pre_close_v1", "event_time_v1", "event_time_v2", "event_time_p1", "desk_cascade_v1"):
        raise ValueError("unknown evidence window policy")
    selected_web_evidence = web_evidence or env.get(
        "CATALYST_WEB_EVIDENCE", "discovery_v2" if selected_window_policy == "desk_cascade_v1" else "off")
    selected_web_on_demand = (web_on_demand if web_on_demand is not None
                              else env.get("CATALYST_WEB_ON_DEMAND", "0") == "1")
    if selected_web_evidence not in ("off", "discovery_v2"):
        raise ValueError("unsupported web evidence policy")
    if selected_web_on_demand:
        raise ValueError("web on-demand is no longer supported")
    if selected_web_evidence == "discovery_v2" and selected_window_policy != "desk_cascade_v1":
        raise ValueError("discovery_v2 requires desk_cascade_v1")
    verifier = env.get("CATALYST_PRIMARY_VERIFIER", "1") != "0"
    selected_investigator = investigator_mode if investigator_mode is not None else env.get("CATALYST_INVESTIGATOR", "off")
    if selected_investigator != "off":
        raise ValueError("unknown investigator mode")
    if selected_window_policy == "desk_cascade_v1":
        from catalyst_agents.attribution.desk_context import DESK_CONTEXT_VERSION
        return AttributionPolicy(
            verifier_enabled=verifier,
            verifier_version=VERIFIER_VERSION if verifier else None,
            verifier_prompt_sha256=hashlib.sha256(PROMPT.encode()).hexdigest() if verifier else None,
            corrective_enabled=False,
            quote_rules_version="q1c_quote_rules_desk_cascade_v1",
            force_full=force_full,
            evidence_window_policy=selected_window_policy,
            web_evidence=selected_web_evidence,
            web_on_demand=False,
            stage1_verifier_version=VERIFIER_VERSION if verifier else None,
            stage1_verifier_prompt_sha256=hashlib.sha256(PROMPT.encode()).hexdigest() if verifier else None,
            stage2_verifier_version=EVENT_TIME_P1_VERIFIER_VERSION if verifier else None,
            stage2_verifier_prompt_sha256=hashlib.sha256(EVENT_TIME_P1_PROMPT.encode()).hexdigest() if verifier else None,
            desk_context_version=DESK_CONTEXT_VERSION,
            investigator_mode=None,
            investigator_version=None,
            v2_rules=canonical_v2_rules,
        )
    is_event_time = selected_window_policy in ("event_time_v1", "event_time_v2")
    verifier_prompt = (
        EVENT_TIME_V2_PROMPT if selected_window_policy == "event_time_v2"
        else EVENT_TIME_P1_PROMPT if selected_window_policy == "event_time_p1"
        else EVENT_TIME_PROMPT if is_event_time else PROMPT
    )
    verifier_version = (
        EVENT_TIME_V2_VERIFIER_VERSION if selected_window_policy == "event_time_v2"
        else EVENT_TIME_P1_VERIFIER_VERSION if selected_window_policy == "event_time_p1"
        else EVENT_TIME_VERIFIER_VERSION if is_event_time else VERIFIER_VERSION
    )
    return AttributionPolicy(
        verifier_enabled=verifier,
        verifier_version=verifier_version if verifier else None,
        verifier_prompt_sha256=hashlib.sha256(verifier_prompt.encode()).hexdigest() if verifier else None,
        corrective_enabled=env.get("CATALYST_SKIP_CORRECTIVE", "1") == "0",
        quote_rules_version=(
            "q1c_quote_rules_event_time_p1" if selected_window_policy == "event_time_p1"
            else "q1c_quote_rules_event_time_v2" if selected_window_policy == "event_time_v2"
            else "q1c_quote_rules_event_time_v1" if is_event_time else QUOTE_RULES_VERSION
        ),
        force_full=force_full,
        evidence_window_policy=selected_window_policy,
        web_evidence=selected_web_evidence,
        web_on_demand=False,
        v2_rules=canonical_v2_rules,
    )


def resolve_runtime_attribution_policy(
    env: Mapping[str, str] | None = None,
    *, force_full: bool = False,
) -> AttributionPolicy:
    """Resolve the sole supported top-level runtime strategy.

    ``AttributionPolicy`` still accepts legacy strategy values when decoding
    persisted manifests. New runtime selection, however, is cascade-only;
    ``pre_close_v1`` and ``event_time_p1`` remain internal cascade quote rules.
    """
    selected_env = os.environ if env is None else env
    selected = selected_env.get("CATALYST_EVIDENCE_WINDOW_POLICY", DEFAULT_EVIDENCE_WINDOW_POLICY)
    if selected != "desk_cascade_v1":
        raise ValueError("unsupported top-level evidence window policy; only desk_cascade_v1 is supported")
    return resolve_attribution_policy(
        env=selected_env, evidence_window_policy="desk_cascade_v1", force_full=force_full
    )


__all__ = ["AttributionPolicy", "EvidenceWindowPolicy", "WebEvidencePolicy", "GAP_MIN_ABS_PCT", "GAP_CLOSE_RETURN_SHARE", "resolve_attribution_policy", "resolve_runtime_attribution_policy"]
