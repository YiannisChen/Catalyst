from catalyst_data.ingestion.display_text import normalize_display_text


def test_normalize_display_text_repairs_entities_and_mojibake():
    assert normalize_display_text("Wall Street&#39;s top calls") == "Wall Street's top calls"
    assert normalize_display_text("M&amp;A") == "M&A"
    assert normalize_display_text("Wall Street&amp;#39;s") == "Wall Street's"
    assert normalize_display_text("Nvidiaâ€™s leadership") == "Nvidia’s leadership"
    assert normalize_display_text("â€œGreat resultsâ€\x9d") == "“Great results”"
    assert normalize_display_text("cafÃ©") == "café"


def test_normalize_display_text_preserves_correct_text_and_cleans_whitespace():
    assert normalize_display_text("Société Générale") == "Société Générale"
    assert normalize_display_text("ServiceNow’s") == "ServiceNow’s"
    assert normalize_display_text(None) is None
    assert normalize_display_text("") == ""
    assert normalize_display_text("  a\xa0\t b  ") == "a b"
