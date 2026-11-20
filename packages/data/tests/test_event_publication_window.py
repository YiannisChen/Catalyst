from datetime import datetime, timezone

from catalyst_data.canonical.temporal import (
    build_temporal_identity,
    utc_iso_z,
    with_event_time_publication_window,
)
from catalyst_data.retrieval.fts5 import _eligibility_predicate


def test_event_time_publication_window_uses_three_trading_sessions_and_caps_now():
    identity = with_event_time_publication_window(
        build_temporal_identity("2026-04-13"),
        now=datetime(2026, 4, 15, 18, tzinfo=timezone.utc),
    )
    assert utc_iso_z(identity.cutoff_at) == "2026-04-13T20:00:00Z"
    assert utc_iso_z(identity.evidence_window_start_at) == "2026-04-07T20:00:00Z"
    assert utc_iso_z(identity.evidence_window_end_at) == "2026-04-15T18:00:00Z"


def test_event_time_window_includes_all_of_d_minus_three_session():
    from datetime import timezone
    identity = with_event_time_publication_window(
        build_temporal_identity("2026-04-13"),
        now=datetime(2026, 4, 15, 18, tzinfo=timezone.utc),
    )
    assert identity.evidence_window_start_at < datetime(2026, 4, 8, 14, tzinfo=timezone.utc)


def test_lexical_eligibility_adds_lower_publication_bound_only_when_requested():
    legacy_sql, legacy_params = _eligibility_predicate(
        "a" * 64, "2026-04-13T20:00:00Z", "PYPL", None, None,
    )
    event_sql, event_params = _eligibility_predicate(
        "a" * 64, "2026-04-16T20:00:00Z", "PYPL", None, None,
        window_start_at="2026-04-08T20:00:00Z",
    )
    assert "available_at >" not in legacy_sql
    assert "c.available_at >= ?" in event_sql
    assert event_params[-1] == "2026-04-08T20:00:00Z"
    assert len(event_params) == len(legacy_params) + 1


def test_authoritative_source_time_is_limited_to_stored_source_classes():
    from catalyst_data.canonical.model import SourceClass
    from catalyst_data.retrieval.lexical_retriever import _authoritative_source_time

    sec = _authoritative_source_time(
        {"subtype_metadata": '{"accepted_time":"2026-04-13T14:00:00Z"}'},
        SourceClass.OFFICIAL_GOVERNMENT, "https://www.sec.gov/Archives/filing.htm",
    )
    issuer = _authoritative_source_time(
        {"source_published_at": "2026-04-13T14:15:00Z"},
        SourceClass.ISSUER_DISCLOSURE, "https://issuer.example/news",
    )
    news = _authoritative_source_time(
        {"source_published_at": "2026-04-13T14:15:00Z"},
        SourceClass.REPORTED_NEWS, "https://news.example/story",
    )
    assert utc_iso_z(sec) == "2026-04-13T14:00:00Z"
    assert utc_iso_z(issuer) == "2026-04-13T14:15:00Z"
    assert news is None
