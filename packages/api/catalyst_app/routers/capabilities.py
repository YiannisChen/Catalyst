"""Capability preflight endpoint (M6-9).

Final Migration TSD §20.1 / §15: GET /api/capabilities exposes the exact
Phase 5 §23.2 capability fields (writer streaming, structured output,
timeout/token/cancel). Capability probes are preflight, never attribution
model calls; credentials never appear in the response.
"""
from __future__ import annotations

from datetime import datetime, timezone

from catalyst_app.api_dto import CapabilityModelDTO, CapabilityResponse
from catalyst_app.model_catalog import build_catalog
from fastapi import APIRouter

router = APIRouter(prefix="/api", tags=["capabilities"])


@router.get("/capabilities", response_model=CapabilityResponse)
def capabilities() -> CapabilityResponse:
    catalog = build_catalog()
    models: list[CapabilityModelDTO] = []
    now = datetime.now(timezone.utc)
    for provider in catalog.providers:
        for model in provider.models:
            executable = bool(
                model.supported and (provider.executable or provider.env_key_configured)
            )
            models.append(
                CapabilityModelDTO(
                    provider=provider.id,
                    model_id=model.model_id,
                    executable=executable,
                    # The answer is rendered by code from verified quotes and sent
                    # whole; no Writer model streams tokens (Q1c).
                    writer_streaming=False,
                    analyst_structured_output=model.supported,
                    cancellation="supported",
                    token_usage="provider_reported",
                    timeout_seconds=60,
                    offline_only=False,
                    readiness="ready" if executable else "unavailable",
                    capability_probe_at=now,
                    revision="cap:v1",
                )
            )
    return CapabilityResponse(models=tuple(models))
