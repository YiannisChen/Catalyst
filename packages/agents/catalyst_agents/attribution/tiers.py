"""Deterministic answer-tier decisions over the frozen session MoveProfile."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from catalyst_agents.attribution.ceilings import PRE_OPEN_MIN_MOVE_PCT
from catalyst_agents.attribution.move_profile import AlignmentBand, MoveProfile
from catalyst_agents.attribution.observation_builder import ALIGNED_RESIDUAL_SHARE
from catalyst_agents.retrieval.policy import (
    COMMODITY_MIN_MOVE_PCT,
    MARKET_MIN_MOVE_PCT,
    SECTOR_MIN_MOVE_PCT,
)
from catalyst_agents.runtime.manifest import ObservationPolicyConfig

Tier = Literal["T0", "T1", "T2", "T3"]
_REFERENCE_MOVE_MIN_PCT = min(
    COMMODITY_MIN_MOVE_PCT, MARKET_MIN_MOVE_PCT, SECTOR_MIN_MOVE_PCT
)


@dataclass(frozen=True)
class TierDecision:
    tier: Tier
    governing_ref: dict[str, Any] | None
    overrides: list[str]
    reason_code: str
    reason_text: str
    threshold_used: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _value(enum_or_value: Any) -> Any:
    return getattr(enum_or_value, "value", enum_or_value)


def _aligned(alignment: Any) -> bool:
    return alignment is not None and _value(getattr(alignment, "band", None)) == AlignmentBand.ALIGNED.value


def _reference(
    kind: str,
    ticker: str | None,
    return_pct: float | None,
    residual_pct: float | None,
    band: str,
) -> dict[str, Any] | None:
    if return_pct is None:
        return None
    return {
        "kind": kind,
        "ticker": ticker,
        "return_pct": float(return_pct),
        "residual_pct": float(residual_pct) if residual_pct is not None else None,
        "band": band,
    }


def _governing_ref(profile: MoveProfile) -> dict[str, Any] | None:
    target = profile.target_return
    commodity_return = profile.commodity_return
    commodity_aligned = bool(
        profile.commodity_ticker
        and target is not None
        and commodity_return is not None
        and abs(commodity_return) >= COMMODITY_MIN_MOVE_PCT
        and target != 0
        and (commodity_return > 0) == (target > 0)
    )
    if commodity_aligned:
        return _reference(
            "commodity", profile.commodity_ticker, commodity_return,
            target - commodity_return, "ALIGNED",
        )

    sector = profile.sector_comove
    market = profile.market_comove
    # A sector/market disagreement is a conflict, so no lower-priority
    # reference can turn the market move into a T2 explanation.
    if _aligned(market) and sector is not None and _value(sector.band) == AlignmentBand.DIVERGENT.value:
        return None
    if (
        _aligned(sector)
        and sector.reference_return_pct is not None
        and abs(sector.reference_return_pct) >= SECTOR_MIN_MOVE_PCT
    ):
        return _reference(
            "sector", profile.sector_ticker, sector.reference_return_pct,
            sector.residual_return_pct, AlignmentBand.ALIGNED.value,
        )
    peer = profile.peer_comove
    summary = profile.peer_summary
    if (
        _aligned(peer)
        and summary is not None
        and summary.available_peer_count >= 3
        and peer.reference_return_pct is not None
    ):
        return _reference(
            "peers", "peer median", peer.reference_return_pct,
            peer.residual_return_pct, AlignmentBand.ALIGNED.value,
        )
    if (
        _aligned(market)
        and market.reference_return_pct is not None
        and abs(market.reference_return_pct) >= MARKET_MIN_MOVE_PCT
    ):
        return _reference(
            "market", "SPY", market.reference_return_pct,
            market.residual_return_pct, AlignmentBand.ALIGNED.value,
        )
    return None


def _all_available_refs_aligned(profile: MoveProfile, policy: ObservationPolicyConfig) -> bool:
    target = profile.target_return
    peers = profile.peer_summary
    too_few_peers = peers is None or (peers.available_peer_count or 0) < policy.minimum_peer_count
    for alignment in (profile.market_comove, profile.sector_comove, profile.peer_comove):
        if alignment is None or _value(alignment.availability) == "UNAVAILABLE":
            continue
        if alignment is profile.peer_comove and too_few_peers:
            continue  # same minimum as the governing-reference rule: a 1-2 name peer set is not a reference
        if _aligned(alignment):
            continue
        # Existing classifier intentionally marks a flat target UNKNOWN. A
        # sub-1% reference is harmless for the ordinary-close tier.
        ref = alignment.reference_return_pct
        if (
            target is not None
            and abs(target) < policy.flat_reference_return_pct
            and ref is not None
            and abs(ref) < _REFERENCE_MOVE_MIN_PCT
        ):
            continue
        return False
    return True


def default_observation_policy() -> ObservationPolicyConfig:
    """Canonical fallback values shared by tiering and production composition."""
    return ObservationPolicyConfig(
        material_target_return_pct=2.0,
        material_prior_return_pct=1.5,
        quiet_target_return_pct=0.5,
        flat_reference_return_pct=0.25,
        aligned_residual_pct=1.0,
        volume_elevated_ratio=1.5,
        volume_extreme_ratio=3.0,
        minimum_peer_count=3,
        require_sector_and_peer_for_broad_sector=False,
        scenario_policy_version="sp:v2",
    )


def decide_tier(
    move_profile: MoveProfile,
    *,
    filings_in_window: int,
    policy: ObservationPolicyConfig | None = None,
) -> TierDecision:
    """Select the answer tier using production policy and existing move bands.

    ``policy`` is the pinned observation policy in a run manifest. The
    fallback values match ``production_observation_policy`` and are only for
    direct callers that do not carry a run manifest.
    """
    if filings_in_window < 0:
        raise ValueError("filings_in_window must be non-negative")
    if policy is None:
        policy = default_observation_policy()

    target = move_profile.target_return
    threshold = (
        max(policy.aligned_residual_pct, ALIGNED_RESIDUAL_SHARE * abs(target))
        if target is not None else None
    )
    if "not_a_trading_session" in move_profile.coverage_flags or target is None:
        reason = (
            "not_a_trading_session"
            if "not_a_trading_session" in move_profile.coverage_flags
            else "target_return_unavailable"
        )
        return TierDecision("T0", None, [], reason, f"No close-to-close return is available ({reason}).", None)

    overrides: list[str] = []
    if move_profile.gap_return is not None and abs(move_profile.gap_return) >= PRE_OPEN_MIN_MOVE_PCT:
        overrides.append("GAP_GE_2")
    volume = move_profile.volume_abnormality
    if volume is not None and _value(volume.band) == "EXTREME":
        overrides.append("VOLUME_EXTREME")
    if filings_in_window:
        overrides.append("ISSUER_FILING")
    prior = move_profile.prior_session_return
    if (
        abs(target) >= policy.material_target_return_pct
        and prior is not None
        and abs(prior) >= policy.material_prior_return_pct
        and target != 0
        and prior != 0
        and (target > 0) == (prior > 0)
    ):
        overrides.append("CONTINUATION")

    governing = _governing_ref(move_profile)
    commodity_move = (
        move_profile.commodity_return is not None
        and abs(move_profile.commodity_return) >= COMMODITY_MIN_MOVE_PCT
    )
    sector = move_profile.sector_comove
    target_flat_vs_moving_sector = bool(
        abs(target) < policy.flat_reference_return_pct
        and sector is not None
        and sector.reference_return_pct is not None
        and abs(sector.reference_return_pct) >= SECTOR_MIN_MOVE_PCT
    )

    if (
        not overrides
        and abs(target) < policy.material_target_return_pct
        and not commodity_move
        and not target_flat_vs_moving_sector
        and _all_available_refs_aligned(move_profile, policy)
    ):
        reason_code = "ORDINARY_ALIGNED_CLOSE" if governing else "ORDINARY_CLOSE_REFERENCES_UNAVAILABLE"
        reason_text = "The close was below the material-move threshold and no tier override applied. We did not look for a stock-specific story."
        return TierDecision("T1", governing, overrides, reason_code, reason_text, threshold)

    if (
        governing is not None
        and not overrides
        and (
            abs(target) >= policy.material_target_return_pct
            or abs(governing["return_pct"]) >= _REFERENCE_MOVE_MIN_PCT
        )
        and governing["band"] == AlignmentBand.ALIGNED.value
    ):
        return TierDecision(
            "T2", governing, overrides, "MOVED_WITH_GOVERNING_REFERENCE",
            f"The move met the material threshold and aligned with {governing['ticker'] or governing['kind']}.",
            threshold,
        )

    if target_flat_vs_moving_sector:
        reason_code = "HELD_UP_VS_SECTOR"
        reason_text = "The stock was flat while its sector moved materially, a relative move that merits a stock-specific review."
    elif overrides:
        reason_code = overrides[0]
        reason_text = "A session override applied, so the close alone does not establish an ordinary move."
    elif governing is None:
        reason_code = "NO_GOVERNING_REFERENCE"
        reason_text = "No available reference met the governing-reference rules for this move."
    else:
        reason_code = "GOVERNING_REFERENCE_NOT_ALIGNED"
        reason_text = f"The move did not align with the governing reference {governing['ticker'] or governing['kind']}."
    return TierDecision("T3", governing, overrides, reason_code, reason_text, threshold)


__all__ = ["TierDecision", "decide_tier"]
