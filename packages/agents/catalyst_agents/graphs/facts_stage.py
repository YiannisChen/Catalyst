from __future__ import annotations

from typing import Any, Callable

from catalyst_data.canonical.temporal import TemporalIdentity

from catalyst_agents.attribution.answer_snapshot import build_facts_snapshot
from catalyst_agents.attribution.move_profile import MoveProfile
from catalyst_agents.attribution.session_facts import _observed_move_text


def emit_facts(
    profile: MoveProfile,
    *,
    ticker: str,
    temporal_identity: TemporalIdentity,
    on_step: Callable[[str, dict[str, Any]], None] | None,
) -> None:
    facts = build_facts_snapshot(
        ticker=ticker,
        move_profile=profile,
        temporal_identity=temporal_identity,
        facts_text=_observed_move_text(ticker, profile, temporal_identity.session_date),
    )
    if on_step is not None:
        on_step("facts.ready", facts)
