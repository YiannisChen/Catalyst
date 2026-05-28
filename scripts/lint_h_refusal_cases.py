from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path


def _row(case_id: str, code: str, message: str) -> dict:
    return {"case_id": case_id, "code": code, "message": message}


def load_holiday_set(path: Path) -> set[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return set(payload.get("holidays", []))


def load_db_tickers(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute("select distinct symbol from ohlcv")
        return {r[0] for r in cur.fetchall() if r[0]}
    finally:
        conn.close()


def is_iso_date(s: str) -> bool:
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except Exception:
        return False


def is_weekend_or_holiday(trade_date: str, holidays: set[str]) -> bool:
    if not is_iso_date(trade_date):
        return False
    d = datetime.strptime(trade_date, "%Y-%m-%d")
    return d.weekday() >= 5 or trade_date in holidays


def extract_query_tokens(query: str) -> list[str]:
    return re.findall(r"\b[A-Z]{2,5}\b", query or "")


def lint_cases(path: Path, db_path: Path, holiday_config: Path) -> dict:
    rows = json.loads(path.read_text(encoding="utf-8"))
    holidays = load_holiday_set(holiday_config)
    db_tickers = load_db_tickers(db_path)

    errors: list[dict] = []
    warnings: list[dict] = []
    required = ["id", "ticker", "trade_date", "query_override", "expected_status", "should_refuse", "failure_criteria"]

    for row in rows:
        cid = str(row.get("id", "<missing>"))
        for k in required:
            if k not in row or row[k] in (None, ""):
                errors.append(_row(cid, f"missing_{k}", f"{k} is required"))

        trade_date = str(row.get("trade_date", ""))
        ticker = str(row.get("ticker", ""))
        query_override = str(row.get("query_override", ""))

        if not is_iso_date(trade_date):
            errors.append(_row(cid, "trade_date_format_invalid", "trade_date must be YYYY-MM-DD"))
        elif is_weekend_or_holiday(trade_date, holidays):
            warnings.append(_row(cid, "market_closed_date", "trade_date is weekend/holiday"))

        if ticker not in db_tickers:
            errors.append(_row(cid, "ticker_not_in_db", "ticker is not in ohlcv universe"))

        if row.get("expected_status") != "INSUFFICIENT":
            errors.append(_row(cid, "expected_status_not_insufficient", "expected_status must be INSUFFICIENT"))

        if row.get("should_refuse") is not True:
            errors.append(_row(cid, "should_refuse_not_true", "should_refuse must be true"))

        query_tokens = extract_query_tokens(query_override)
        db_query_ticker = next((tok for tok in query_tokens if tok in db_tickers), None)
        if cid == "h001":
            if "APPL" in query_tokens:
                warnings.append(_row(cid, "query_ticker_mismatch_expected_refusal", "h001 typo path should trigger refusal"))
            elif db_query_ticker and db_query_ticker != ticker:
                warnings.append(_row(cid, "query_ticker_mismatch_expected_refusal", "h001 typo path should trigger refusal"))
        elif db_query_ticker and db_query_ticker != ticker:
            errors.append(_row(cid, "query_ticker_mismatch", "query ticker must match case ticker"))

    return {"ok": len(errors) == 0, "errors": errors, "warnings": warnings}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--holiday-config", required=True)
    args = parser.parse_args()

    result = lint_cases(Path(args.input), Path(args.db), Path(args.holiday_config))
    print(json.dumps(result, indent=2, ensure_ascii=False))
