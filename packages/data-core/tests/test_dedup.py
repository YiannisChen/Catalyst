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
