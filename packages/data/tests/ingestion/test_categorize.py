from catalyst_data.ingestion.categorize import classify


def test_classifies_each_news_category_with_rule_ids():
    examples = {
        "EARNINGS": "Acme beats quarterly earnings estimates and raises its guidance",
        "ANALYST": "Analyst upgrades Acme to Buy and raises price target",
        "DEALS": "Acme wins a $2 billion contract with the Pentagon",
        "PRODUCT": "Acme unveils its new AI chip for data centers",
        "REGULATORY_LEGAL": "FDA approves Acme's new treatment after lawsuit settlement",
        "MANAGEMENT": "Acme CEO steps down as company restructures",
        "CAPITAL": "Acme raises dividend and announces a share buyback",
        "MARKET": "Stocks making the biggest moves premarket: Acme, Beta and Gamma",
        "OPINION": "Is Acme stock a buy now? Reasons to hold it for the long term",
        "OTHER": "Acme investor event scheduled for Tuesday",
    }
    for category, title in examples.items():
        actual_category, rule_id = classify(title, None)
        assert actual_category == category
        assert rule_id


def test_title_rules_precede_summary_and_event_beats_price_move():
    assert classify("Acme shares fall after earnings miss", "Analysts cut target") [0] == "EARNINGS"
    assert classify("Acme shares rise", "Acme announces a new contract") [0] == "MARKET"
