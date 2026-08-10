"""T4 case pack contract: schema, deterministic identity, golden-set derivation.

Expected identity values and diversity counts are literal independent fixtures
(no production hashing/selection helpers are used to compute them at test time).

Amendment P1: answerable queries must be neutral questions built only from
non-answer fields (ticker / trade_date / observed price move). Cause text,
cause category, evidence text, expected answer/status must never appear in the
query. Refusal cases keep the validated query_override.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from catalyst_eval.post_import.case_pack import (
    SCHEMA_VERSION,
    CasePackCase,
    build_smoke_case_pack,
    compute_case_pack_id,
    load_case_pack,
    write_case_pack,
)

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "golden_set"

# Literal fixture: two canonical case objects whose pack identity is
# pre-computed independently (sha256 of canonical JSON, sorted keys).
# Answerable query is neutral; refusal query is the validated query_override.
LITERAL_CASES = [
    {
        "schema_version": SCHEMA_VERSION,
        "case_id": "t1",
        "ticker": "AAPL",
        "session_date": "2025-06-12",
        "cutoff": "2025-06-12T20:00:00Z",
        "query": "Apple was down around 2% on April 19, 2025.",
        "source_set": "h_refusal_cases.validated.json",
        "golden": {"golden_id": "h003", "expected_status": "INSUFFICIENT", "should_refuse": True},
    },
    {
        "schema_version": SCHEMA_VERSION,
        "case_id": "t2",
        "ticker": "NVDA",
        "session_date": "2025-10-28",
        "cutoff": "2025-10-28T20:00:00Z",
        "query": "Why did NVDA move on 2025-10-28?",
        "source_set": "v1_2_p0_set.jsonl",
        "golden": {"golden_id": "g013", "expected_status": "SUFFICIENT", "should_refuse": False},
    },
]
LITERAL_CASE_PACK_ID = "05333690c3a3f074e34b038a17792837568bf55870abf5678b15ec85c41a03aa"

# The reviewed smoke selection: 10 cases across the frozen golden sets.
EXPECTED_SMOKE_SELECTION = (
    ("g006", "TSLA", "v1_2_p0_set.jsonl"),
    ("g013", "NVDA", "v1_2_p0_set.jsonl"),
    ("g017", "AMD", "v1_2_p0_set.jsonl"),
    ("g024", "UNH", "v1_2_p0_set.jsonl"),
    ("g041", "AMZN", "v1_2_p0_set.jsonl"),
    ("g007", "TSLA", "v1_2_p0_set.jsonl"),
    ("h001", "AAPL", "h_refusal_cases.validated.json"),
    ("h004", "GOOGL", "h_refusal_cases.validated.json"),
    ("h005", "JPM", "h_refusal_cases.validated.json"),
    ("h007", "MSFT", "h_refusal_cases.validated.json"),
)


def _golden_rows(filename: str) -> list[dict]:
    path = GOLDEN_DIR / filename
    if filename.endswith(".jsonl"):
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return json.loads(path.read_text())


def test_case_pack_schema_version_is_literal():
    assert SCHEMA_VERSION == "post_import_case_pack_v1"


def test_case_pack_identity_matches_independent_literal_oracle():
    """Deterministic identity equals a pre-computed canonical-JSON literal."""
    cases = [CasePackCase(**row) for row in LITERAL_CASES]
    assert compute_case_pack_id(cases) == LITERAL_CASE_PACK_ID


def test_case_pack_identity_changes_for_every_semantic_mutation():
    base = [CasePackCase(**row) for row in LITERAL_CASES]
    base_id = compute_case_pack_id(base)
    mutations = {
        "case_id": {"case_id": "t3"},
        "ticker": {"ticker": "MSFT"},
        "session_date": {"session_date": "2025-06-13"},
        "cutoff": {"cutoff": "2025-06-12T21:00:00Z"},
        "query": {"query": "different question"},
        "source_set": {"source_set": "other.jsonl"},
        "golden": {"golden": {"golden_id": "x", "expected_status": "SUFFICIENT", "should_refuse": False}},
    }
    for field, change in mutations.items():
        mutated = [CasePackCase(**{**row.to_dict(), **change}) if row.case_id == "t1" else row for row in base]
        assert compute_case_pack_id(mutated) != base_id, field
        assert compute_case_pack_id(mutated) == compute_case_pack_id(mutated)


def test_case_pack_write_and_load_roundtrip(tmp_path: Path):
    cases = [CasePackCase(**row) for row in LITERAL_CASES]
    path = write_case_pack(cases, tmp_path / "pack.jsonl")
    assert path.is_file()
    loaded = load_case_pack(path)
    assert [case.case_id for case in loaded] == ["t1", "t2"]
    assert [case.ticker for case in loaded] == ["AAPL", "NVDA"]
    assert all(case.cutoff.endswith("Z") for case in loaded)
    assert all(case.query for case in loaded)
    assert compute_case_pack_id(loaded) == LITERAL_CASE_PACK_ID


def test_smoke_pack_selection_is_exact_and_ordered():
    cases = build_smoke_case_pack(GOLDEN_DIR)
    assert [(c.case_id, c.ticker, c.source_set) for c in cases] == list(EXPECTED_SMOKE_SELECTION)
    assert len(cases) == 10


def test_smoke_pack_every_case_has_complete_runner_schema():
    cases = build_smoke_case_pack(GOLDEN_DIR)
    required = {"case_id", "ticker", "session_date", "cutoff", "query", "source_set", "golden"}
    for case in cases:
        d = case.to_dict()
        assert required <= set(d)
        assert case.cutoff
        assert case.cutoff.endswith("Z")
        assert case.session_date
        assert case.query
        assert case.golden.get("golden_id") == case.case_id


def test_smoke_pack_diversity_literal_counts():
    cases = build_smoke_case_pack(GOLDEN_DIR)
    statuses = [c.golden.get("expected_status") for c in cases]
    tickers = {c.ticker for c in cases}
    dates = {c.session_date for c in cases}
    assert statuses.count("SUFFICIENT") == 5
    assert statuses.count("PARTIAL") == 1
    assert statuses.count("INSUFFICIENT") == 4
    assert len(tickers) == 9  # TSLA, NVDA, AMD, UNH, AMZN, AAPL, GOOGL, JPM, MSFT
    assert len(dates) >= 8
    # Refusal/low-evidence and answerable coverage
    assert any(c.golden.get("should_refuse") for c in cases)
    assert any(not c.golden.get("should_refuse") for c in cases)


# ---------------------------------------------------------------------------
# Amendment P1: neutral query, no golden answer leakage
# ---------------------------------------------------------------------------


def test_answerable_query_is_never_equal_to_a_golden_cause_text():
    cases = build_smoke_case_pack(GOLDEN_DIR)
    for case in cases:
        if case.source_set.endswith("h_refusal_cases.validated.json"):
            continue
        rows = _golden_rows(case.source_set)
        row = next(r for r in rows if r["id"] == case.case_id)
        cause_texts = [cause.get("text", "") for cause in row.get("causes", [])]
        assert case.query not in cause_texts
        assert all(case.query != text for text in cause_texts)


def test_answerable_query_contains_no_cause_category_evidence_or_answer_fields():
    import re as _re

    cases = build_smoke_case_pack(GOLDEN_DIR)
    for case in cases:
        if case.source_set.endswith("h_refusal_cases.validated.json"):
            continue
        rows = _golden_rows(case.source_set)
        row = next(r for r in rows if r["id"] == case.case_id)
        allowed_field_tokens = set()
        allowed_field_tokens.add(case.ticker.lower())
        allowed_field_tokens.update(case.session_date.lower().split("-"))
        allowed_field_tokens.update(case.session_date.lower().replace("-", " ").split())
        move = str(case.golden.get("price_move_pct")).lower() if case.golden.get("price_move_pct") is not None else ""
        allowed_field_tokens.add(move)
        allowed_field_tokens.add(move.lstrip("-"))
        forbidden_terms = []
        for cause in row.get("causes", []):
            cause_text = cause.get("text", "")
            # Tokenize cause text into significant words, excluding allowed
            # query fields (ticker/date/move) which legitimately appear in the
            # neutral question.
            words = [
                w for w in _re.sub(r"[^a-z0-9 ]", " ", cause_text.lower()).split()
                if len(w) > 2 and w not in allowed_field_tokens
            ]
            forbidden_terms.extend(words[:8])
            category = cause.get("category", "").lower()
            if category:
                forbidden_terms.append(category)
        expected_status = (row.get("expected_status") or "").lower()
        if expected_status:
            forbidden_terms.append(expected_status)
        normalized_query = _re.sub(r"[^a-z0-9 ]", " ", case.query.lower())
        for term in forbidden_terms:
            assert term not in normalized_query, (case.case_id, term, case.query)


def test_answerable_query_uses_only_allowed_fields():
    """Query must contain only ticker/date/move tokens plus neutral template text."""
    import re as _re

    cases = build_smoke_case_pack(GOLDEN_DIR)
    allowed_tokens = {
        "why", "did", "move", "on", "explain", "the", "price", "for", "what",
        "observed", "in", "by", "to", "of", "a", "an", "this", "that", "was",
        "ticker", "date", "session",
    }
    for case in cases:
        if case.source_set.endswith("h_refusal_cases.validated.json"):
            continue
        # Normalize punctuation so date '2025-07-24?' and move '-8.2%' become
        # the allowed raw values.
        normalized = _re.sub(r"[?;!%]", "", case.query.lower())
        tokens = set(normalized.split())
        allowed_date = case.session_date.lower()
        move = str(case.golden.get("price_move_pct")).lower() if case.golden.get("price_move_pct") is not None else ""
        unexpected = {
            t for t in tokens
            if t not in allowed_tokens
            and t not in case.ticker.lower()
            and t not in allowed_date
            and t not in allowed_date.replace("-", " ")
            and t not in move
            and t not in move.lstrip("-")
        }
        assert not unexpected, (case.case_id, case.query, unexpected)


def test_answerable_query_changes_only_with_allowed_fields():
    """Two cases with same ticker/date must produce identical queries."""
    cases = build_smoke_case_pack(GOLDEN_DIR)
    by_key = {}
    for case in cases:
        if case.source_set.endswith("h_refusal_cases.validated.json"):
            continue
        key = (case.ticker, case.session_date)
        by_key.setdefault(key, set()).add(case.query)
    # g006 and g007 are both TSLA but on different dates => distinct queries.
    tsla_queries = [c.query for c in cases if c.ticker == "TSLA" and not c.source_set.endswith("h_refusal_cases.validated.json")]
    assert len(set(tsla_queries)) == len(tsla_queries)  # date drives query change


def test_refusal_query_uses_validated_query_override():
    cases = build_smoke_case_pack(GOLDEN_DIR)
    for case in cases:
        if not case.source_set.endswith("h_refusal_cases.validated.json"):
            continue
        rows = _golden_rows(case.source_set)
        row = next(r for r in rows if r["id"] == case.case_id)
        assert case.query == row["query_override"]


def test_refusal_golden_payload_has_no_inherited_causes():
    """Refusal golden payload must not carry causes unrelated to the query."""
    cases = build_smoke_case_pack(GOLDEN_DIR)
    for case in cases:
        if not case.source_set.endswith("h_refusal_cases.validated.json"):
            continue
        assert not case.golden.get("causes"), case.case_id


def test_smoke_pack_cutoff_is_close_to_close_for_session():
    from catalyst_data.retrieval.cutoff import compute_cutoff

    cases = build_smoke_case_pack(GOLDEN_DIR)
    for case in cases:
        assert case.cutoff == compute_cutoff(case.ticker, case.session_date, mode="close_to_close")


def test_smoke_pack_does_not_modify_golden_set_files():
    before = {
        path.name: path.read_bytes()
        for path in GOLDEN_DIR.glob("*.jsonl")
    }
    before.update({
        path.name: path.read_bytes()
        for path in GOLDEN_DIR.glob("*.json")
    })
    build_smoke_case_pack(GOLDEN_DIR)
    after = {
        path.name: path.read_bytes()
        for path in GOLDEN_DIR.glob("*.jsonl")
    }
    after.update({
        path.name: path.read_bytes()
        for path in GOLDEN_DIR.glob("*.json")
    })
    assert before == after


def test_load_case_pack_rejects_incomplete_schema(tmp_path: Path):
    (tmp_path / "bad.jsonl").write_text(
        json.dumps({"case_id": "x", "ticker": "AAPL"}) + "\n", encoding="utf-8"
    )
    with pytest.raises(ValueError):
        load_case_pack(tmp_path / "bad.jsonl")
