"""Public featured-case list, detail and replay endpoints (contract §12.1)."""
from __future__ import annotations

from catalyst_app.featured import (
    FeaturedDetailDTO,
    FeaturedListDTO,
    FeaturedReplayDTO,
    FeaturedUnavailable,
    load_featured_case,
    load_featured_list,
    load_featured_replay,
)
from fastapi import APIRouter, HTTPException, Response

router = APIRouter(prefix="/api", tags=["featured"])


def _unavailable(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail=f"runtime_unavailable: featured_bundle_invalid ({exc})",
    )


@router.get("/featured", response_model=FeaturedListDTO)
def list_featured(response: Response) -> FeaturedListDTO:
    response.headers["Cache-Control"] = "public, max-age=300"
    try:
        return load_featured_list()
    except FeaturedUnavailable as exc:
        raise _unavailable(exc) from exc


@router.get("/featured/{case_id}", response_model=FeaturedDetailDTO)
def get_featured(case_id: str) -> FeaturedDetailDTO:
    try:
        detail = load_featured_case(case_id)
    except FeaturedUnavailable as exc:
        raise _unavailable(exc) from exc
    if detail is None:
        raise HTTPException(status_code=404, detail="case_not_found")
    return detail


@router.get("/featured/{case_id}/replay", response_model=FeaturedReplayDTO)
def replay_featured(case_id: str) -> FeaturedReplayDTO:
    try:
        replay = load_featured_replay(case_id)
    except FeaturedUnavailable as exc:
        raise _unavailable(exc) from exc
    if replay is None:
        raise HTTPException(status_code=404, detail="case_not_found")
    return replay
