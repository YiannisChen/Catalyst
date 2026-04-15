from catalyst_data.dedup.hard import compute_dedup_fingerprint, deduplicate_articles


def test_fingerprint_ignores_punctuation():
    fp1 = compute_dedup_fingerprint("Apple Beats Earnings!", "2026-01-15T10:00:00Z")
    fp2 = compute_dedup_fingerprint("Apple beats earnings", "2026-01-15T10:30:00Z")
    assert fp1 == fp2  # same 2-hour window, normalized title matches


def test_fingerprint_differs_across_time_windows():
    fp1 = compute_dedup_fingerprint("Apple Beats Earnings", "2026-01-15T10:00:00Z")
    fp2 = compute_dedup_fingerprint("Apple Beats Earnings", "2026-01-15T14:00:00Z")
    assert fp1 != fp2  # different 2-hour windows


def test_deduplicate_articles_removes_dupes():
    articles = [
        {"title": "Apple Beats Earnings!", "published_utc": "2026-01-15T10:00:00Z"},
        {"title": "Apple beats earnings", "published_utc": "2026-01-15T10:30:00Z"},
        {"title": "Different Story", "published_utc": "2026-01-15T10:00:00Z"},
    ]
    result = deduplicate_articles(articles)
    assert len(result) == 2


def test_deduplicate_preserves_order():
    articles = [
        {"title": "First Article", "published_utc": "2026-01-15T10:00:00Z"},
        {"title": "Second Article", "published_utc": "2026-01-15T10:00:00Z"},
    ]
    result = deduplicate_articles(articles)
    assert result[0]["title"] == "First Article"
    assert result[1]["title"] == "Second Article"


def test_same_title_within_2h_window_is_duplicate():
    """Same title at 10:00 and 11:59 — both in the 10:00-12:00 window — deduped."""
    articles = [
        {"title": "Breaking News!", "published_utc": "2026-01-15T10:00:00Z"},
        {"title": "Breaking News!", "published_utc": "2026-01-15T11:59:00Z"},
    ]
    result = deduplicate_articles(articles)
    assert len(result) == 1


def test_same_title_outside_2h_window_is_kept():
    """Same title at 10:00 and 12:00 — different windows (10-12, 12-14) — kept."""
    articles = [
        {"title": "Breaking News!", "published_utc": "2026-01-15T10:00:00Z"},
        {"title": "Breaking News!", "published_utc": "2026-01-15T12:00:00Z"},
    ]
    result = deduplicate_articles(articles)
    assert len(result) == 2


def test_clean_py_uses_dedup_hard(monkeypatch):
    """Verify clean.py delegates dedup to dedup.hard (BUG-006: single source)."""
    from catalyst_data.pipeline import clean as clean_mod

    calls = []
    original = clean_mod.deduplicate_articles

    def spy(articles):
        calls.append(len(articles))
        return original(articles)

    monkeypatch.setattr(clean_mod, "deduplicate_articles", spy)
    from catalyst_data.pipeline.clean import run_clean

    run_clean({"results": [
        {"title": "A", "published_utc": "2026-01-15T10:00:00Z"},
        {"title": "A", "published_utc": "2026-01-15T10:30:00Z"},
    ]}, "polygon_news")
    assert len(calls) == 1  # dedup.hard was called
