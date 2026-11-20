"""Read-only API for the generated M9 quality figures."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

from catalyst_app.access_control import require_admin
from catalyst_data.storage.runtime_paths import runtime_data_dir
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(tags=["quality"])
REPO = Path(__file__).resolve().parents[4]


class QualityInput(BaseModel):
    path: str
    sha256: str
    role: str


class QualityResponse(BaseModel):
    schema_version: Literal["quality_v1"]
    generated_at: str
    code_revision: str | None
    model: dict[str, str]
    inputs: list[QualityInput]
    sets: list[dict[str, Any]]
    comparisons: list[dict[str, Any]]
    calibration: dict[str, Any]
    adjudications: dict[str, Any]


def _quality_path() -> Path:
    configured = os.environ.get("CATALYST_QUALITY_JSON")
    path = Path(configured) if configured else runtime_data_dir(REPO) / "quality/quality_v1.json"
    return path if path.is_absolute() else REPO / path


@router.get("/api/quality", response_model=QualityResponse)
def get_quality(request: Request) -> QualityResponse:
    require_admin(request)
    path = _quality_path()
    if not path.is_file():
        raise HTTPException(status_code=404, detail="quality export not generated")
    return QualityResponse.model_validate(json.loads(path.read_text()))


__all__ = ["QualityResponse", "router"]
