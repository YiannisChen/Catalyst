"""T4 smoke case pack: schema, deterministic identity, and golden-set derivation.

The case pack is a derived artifact: it reuses existing reviewed golden sets
verbatim (never modifying them) and computes deterministic close-to-close
cutoffs through the production calendar.

Amendment P1: answerable queries are neutral questions constructed only from
non-answer fields (ticker / trade_date / observed price move). Cause text,
cause category, evidence text, expected answer, and expected status never enter
the query. Refusal/adversarial cases keep the validated ``query_override``.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from catalyst_data.retrieval.cutoff import compute_cutoff

SCHEMA_VERSION = "post_import_case_pack_v1"

REQUIRED_FIELDS = {
    "schema_version", "case_id", "ticker", "session_date", "cutoff",
    "query", "source_set", "golden",
}

# Reviewed smoke selection: (golden_set_file, golden_id). This selection is a
# local reviewed set, not a manager-approved frozen artifact; approval is
# decided by manager re-review of the persisted T4 evidence.
SMOKE_SELECTION: tuple[tuple[str, str], ...] = (
    ("v1_2_p0_set.jsonl", "g006"),
    ("v1_2_p0_set.jsonl", "g013"),
    ("v1_2_p0_set.jsonl", "g017"),
    ("v1_2_p0_set.jsonl", "g024"),
    ("v1_2_p0_set.jsonl", "g041"),
    ("v1_2_p0_set.jsonl", "g007"),
    ("h_refusal_cases.validated.json", "h001"),
    ("h_refusal_cases.validated.json", "h004"),
    ("h_refusal_cases.validated.json", "h005"),
    ("h_refusal_cases.validated.json", "h007"),
)


@dataclass(frozen=True)
class CasePackCase:
    schema_version: str = SCHEMA_VERSION
    case_id: str = ""
    ticker: str = ""
    session_date: str = ""
    cutoff: str = ""
    query: str = ""
    source_set: str = ""
    golden: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "case_id": self.case_id,
            "ticker": self.ticker,
            "session_date": self.session_date,
            "cutoff": self.cutoff,
            "query": self.query,
            "source_set": self.source_set,
            "golden": dict(self.golden),
        }


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compute_case_pack_id(cases: list[CasePackCase]) -> str:
    """Deterministic identity from canonical JSON of the ordered case list."""
    ordered = sorted((case.to_dict() for case in cases), key=lambda row: row["case_id"])
    return hashlib.sha256(
        _canonical_json({"schema_version": SCHEMA_VERSION, "cases": ordered}).encode("utf-8")
    ).hexdigest()


def _validate_row(row: dict[str, Any], line_number: int) -> None:
    if not isinstance(row, dict):
        raise ValueError(f"case at line {line_number} must be an object")
    if set(row) != REQUIRED_FIELDS:
        missing = sorted(REQUIRED_FIELDS - set(row))
        extra = sorted(set(row) - REQUIRED_FIELDS)
        raise ValueError(
            f"case at line {line_number} fields mismatch"
            f"{f' missing={missing}' if missing else ''}{f' extra={extra}' if extra else ''}"
        )
    if row.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"case at line {line_number} has invalid schema_version")
    if not row.get("case_id") or not row.get("ticker"):
        raise ValueError(f"case at line {line_number} missing identity")
    if not row.get("session_date") or not row.get("cutoff"):
        raise ValueError(f"case at line {line_number} missing session/cutoff")
    if not row.get("query"):
        raise ValueError(f"case at line {line_number} missing query")


def load_case_pack(path: Path) -> list[CasePackCase]:
    path = Path(path)
    cases: list[CasePackCase] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            try:
                row = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid case JSON at line {line_number}") from exc
            _validate_row(row, line_number)
            cases.append(CasePackCase(**row))
    if not cases:
        raise ValueError("case pack is empty")
    return cases


def write_case_pack(cases: list[CasePackCase], path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for case in cases:
            handle.write(_canonical_json(case.to_dict()) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    return path


def build_neutral_answerable_query(*, ticker: str, trade_date: str, price_move_pct: Any = None) -> str:
    """Neutral question from non-answer fields only.

    Follows the project's existing neutral query convention
    (``catalyst_agents.nodes.miner``): ``Why did {ticker} move on {trade_date}?``.
    When an observed price move exists it may be included as magnitude context,
    never as an explanation.
    """
    if price_move_pct is None:
        return f"Why did {ticker} move on {trade_date}?"
    return f"Why did {ticker} move {price_move_pct}% on {trade_date}?"


def _load_golden_jsonl(golden_dir: Path, filename: str) -> list[dict[str, Any]]:
    path = golden_dir / filename
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _load_golden_json(golden_dir: Path, filename: str) -> list[dict[str, Any]]:
    path = golden_dir / filename
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"golden file {filename} must be a JSON array")
    return data


def build_smoke_case_pack(golden_dir: Path) -> list[CasePackCase]:
    """Build the 10-case T4 smoke pack from existing golden sets only."""
    golden_dir = Path(golden_dir)
    by_source: dict[str, list[dict[str, Any]]] = {}
    cases: list[CasePackCase] = []
    for source_set, golden_id in SMOKE_SELECTION:
        if source_set not in by_source:
            if source_set.endswith(".jsonl"):
                by_source[source_set] = _load_golden_jsonl(golden_dir, source_set)
            else:
                by_source[source_set] = _load_golden_json(golden_dir, source_set)
        row = next((r for r in by_source[source_set] if r.get("id") == golden_id), None)
        if row is None:
            raise ValueError(f"golden id {golden_id} not found in {source_set}")
        ticker = row["ticker"]
        trade_date = row["trade_date"]
        cutoff = compute_cutoff(ticker, trade_date, mode="close_to_close")
        refusal = source_set.endswith("h_refusal_cases.validated.json")
        if refusal:
            query = row["query_override"]
            causes: list[Any] = []
        else:
            query = build_neutral_answerable_query(
                ticker=ticker,
                trade_date=trade_date,
                price_move_pct=row.get("price_move_pct"),
            )
            causes = row.get("causes") or []
        golden = {
            "golden_id": row.get("id"),
            "expected_status": row.get("expected_status"),
            "should_refuse": bool(row.get("should_refuse", False)),
            "price_move_pct": row.get("price_move_pct"),
            "causes": causes,
        }
        cases.append(CasePackCase(
            schema_version=SCHEMA_VERSION,
            case_id=golden_id,
            ticker=ticker,
            session_date=trade_date,
            cutoff=cutoff,
            query=query,
            source_set=source_set,
            golden=golden,
        ))
    return cases


__all__ = [
    "SCHEMA_VERSION", "SMOKE_SELECTION", "CasePackCase", "build_neutral_answerable_query",
    "build_smoke_case_pack", "compute_case_pack_id", "load_case_pack", "write_case_pack",
]
