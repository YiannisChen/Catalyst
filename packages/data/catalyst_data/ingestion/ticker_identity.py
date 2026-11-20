"""Importable time-aware ticker-to-CIK identities.

This module deliberately ships without seeded identity rows. The existing repository CIK map has
current ticker/CIK pairs only, so it cannot establish issuer names or historical validity intervals.
Add rows only from a verified source using :meth:`TickerIdentityRegistry.from_csv`.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

_CIK_RE = re.compile(r"^\d{1,10}$")
_CSV_COLUMNS = ("ticker", "cik", "issuer_name", "valid_from", "valid_to")


def normalize_ticker(ticker: str) -> str:
    """Uppercase a symbol and normalize class-share dots to dashes (``ABC.A`` → ``ABC-A``)."""
    normalized = ticker.strip().upper().replace(".", "-")
    if not normalized or not re.fullmatch(r"[A-Z0-9][A-Z0-9-]*", normalized):
        raise ValueError(f"invalid ticker: {ticker!r}")
    return normalized


def _iso_date(value: str, field: str) -> date:
    try:
        parsed = date.fromisoformat(value.strip())
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an ISO date (YYYY-MM-DD)") from exc
    return parsed


def _normalize_cik(cik: str) -> str:
    value = str(cik).strip()
    if not _CIK_RE.fullmatch(value):
        raise ValueError(f"invalid CIK: {cik!r}")
    return value.zfill(10)


@dataclass(frozen=True, slots=True)
class TickerIdentity:
    """One ticker's issuer identity over the half-open interval [valid_from, valid_to)."""

    ticker: str
    cik: str
    issuer_name: str
    valid_from: date
    valid_to: date | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "ticker", normalize_ticker(self.ticker))
        object.__setattr__(self, "cik", _normalize_cik(self.cik))
        name = self.issuer_name.strip()
        if not name:
            raise ValueError("issuer_name must not be empty")
        object.__setattr__(self, "issuer_name", name)
        if not isinstance(self.valid_from, date) or isinstance(self.valid_from, datetime):
            raise ValueError("valid_from must be a date")
        if self.valid_to is not None:
            if not isinstance(self.valid_to, date) or isinstance(self.valid_to, datetime):
                raise ValueError("valid_to must be a date or None")
            if self.valid_to <= self.valid_from:
                raise ValueError("valid_to must be later than valid_from")


class AmbiguousTickerIdentityError(ValueError):
    """Raised if multiple identities unexpectedly match one ticker and date."""


class TickerIdentityRegistry:
    """Validated identity rows, without collapsing distinct market-data ticker symbols."""

    def __init__(self, rows: Iterable[TickerIdentity] = ()) -> None:
        self._rows = tuple(rows)
        self._validate_intervals()

    @property
    def rows(self) -> tuple[TickerIdentity, ...]:
        return self._rows

    @classmethod
    def from_csv(cls, path: str | Path) -> "TickerIdentityRegistry":
        """Load verified rows from a CSV with the exact identity columns.

        ``valid_to`` is exclusive; an empty value means the interval remains open. This importer
        validates structure and conflicts, but does not establish that the supplied facts are true.
        """
        with Path(path).open(newline="", encoding="utf-8-sig") as stream:
            reader = csv.DictReader(stream)
            if tuple(reader.fieldnames or ()) != _CSV_COLUMNS:
                raise ValueError(f"CSV columns must be exactly: {', '.join(_CSV_COLUMNS)}")
            rows = []
            for line, values in enumerate(reader, start=2):
                try:
                    rows.append(TickerIdentity(
                        ticker=values["ticker"],
                        cik=values["cik"],
                        issuer_name=values["issuer_name"],
                        valid_from=_iso_date(values["valid_from"], "valid_from"),
                        valid_to=_iso_date(values["valid_to"], "valid_to") if values["valid_to"].strip() else None,
                    ))
                except (AttributeError, TypeError, ValueError) as exc:
                    raise ValueError(f"invalid ticker identity CSV row {line}: {exc}") from exc
        return cls(rows)

    def _validate_intervals(self) -> None:
        by_ticker: dict[str, list[TickerIdentity]] = {}
        for row in self._rows:
            by_ticker.setdefault(row.ticker, []).append(row)
        for ticker, rows in by_ticker.items():
            ordered = sorted(rows, key=lambda row: row.valid_from)
            for previous, current in zip(ordered, ordered[1:]):
                if previous.valid_to is None or current.valid_from < previous.valid_to:
                    raise ValueError(f"overlapping validity intervals for ticker {ticker}")

    def resolve(self, ticker: str, *, as_of: date) -> TickerIdentity | None:
        """Resolve exactly the supplied market symbol at an explicit historical date."""
        if not isinstance(as_of, date) or isinstance(as_of, datetime):
            raise ValueError("as_of must be an explicit date")
        normalized = normalize_ticker(ticker)
        matches = [row for row in self._rows
                   if row.ticker == normalized
                   and row.valid_from <= as_of
                   and (row.valid_to is None or as_of < row.valid_to)]
        if len(matches) > 1:
            raise AmbiguousTickerIdentityError(
                f"multiple identities match ticker {normalized} at {as_of.isoformat()}"
            )
        return matches[0] if matches else None


__all__ = [
    "AmbiguousTickerIdentityError",
    "TickerIdentity",
    "TickerIdentityRegistry",
    "normalize_ticker",
]
