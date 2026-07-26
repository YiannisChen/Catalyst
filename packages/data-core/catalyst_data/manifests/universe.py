from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping

from catalyst_data.pipeline.fred_manifest import FETCHED_SERIES
from catalyst_data.trading_calendar import calendar_trading_days

RATIFIED_TICKERS: tuple[str, ...] = (
    "AAPL", "AMD", "AMZN", "GOOGL", "JPM", "META", "MSFT", "NVDA", "TSLA", "UNH",
    "INTC", "QCOM", "TSM", "MU", "LRCX", "ASML", "DELL", "HPQ", "CRM", "ADBE",
    "NOW", "ORCL", "PINS", "RDDT", "SNAP", "WMT", "TGT", "COST", "DASH", "F",
    "LCID", "GM", "RIVN", "C", "GS", "BAC", "MS", "CNC", "HUM", "CI",
)

SOURCE_ORDER: tuple[str, ...] = (
    "polygon_ohlcv",
    "polygon_news",
    "finnhub_company_news",
    "sec_filings",
    "fmp_fundamentals",
    "fred_macro",
)

DEFAULT_ENDPOINTS: dict[str, tuple[str, ...]] = {
    "polygon_ohlcv": ("ohlcv",),
    "polygon_news": ("news",),
    "finnhub_company_news": ("company-news",),
    "sec_filings": ("sec_submissions",),
    "fmp_fundamentals": ("income_statement", "balance_sheet", "cash_flow"),
    "fred_macro": (),
}


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_identity(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


@dataclass(frozen=True)
class SourceWindows:
    degraded_start: str
    degraded_end: str
    canonical_start: str
    canonical_end: str

    def __post_init__(self) -> None:
        ds = date.fromisoformat(self.degraded_start)
        de = date.fromisoformat(self.degraded_end)
        cs = date.fromisoformat(self.canonical_start)
        ce = date.fromisoformat(self.canonical_end)
        if not (ds <= de < cs <= ce):
            raise ValueError("source windows must satisfy degraded.start <= degraded.end < canonical.start <= canonical.end")

    @classmethod
    def for_latest_complete_session(cls, latest_complete_session: str) -> "SourceWindows":
        return cls(
            degraded_start="2025-01-02",
            degraded_end="2025-07-31",
            canonical_start="2025-08-01",
            canonical_end=latest_complete_session,
        )

    def to_identity(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class SourceCell:
    stage: str
    source_type: str
    endpoint_name: str
    subject: str
    window_start: str
    window_end: str
    date_domain: str
    provider_profile_version: str
    page_cap: int | None
    item_cap: int | None
    cell_id: str

    @classmethod
    def create(
        cls,
        stage: str,
        source_type: str,
        endpoint_name: str,
        subject: str,
        window_start: str,
        window_end: str,
        date_domain: str,
        provider_profile_version: str,
        *,
        page_cap: int | None = None,
        item_cap: int | None = None,
    ) -> "SourceCell":
        identity = {
            "stage": stage,
            "source_type": source_type,
            "endpoint_name": endpoint_name,
            "subject": subject,
            "window_start": window_start,
            "window_end": window_end,
            "date_domain": date_domain,
            "provider_profile_version": provider_profile_version,
            "page_cap": page_cap,
            "item_cap": item_cap,
        }
        return cls(cell_id=sha256_identity(identity), **identity)

    def to_identity(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class SourceScope:
    source_type: str
    subjects: tuple[str, ...]
    date_domain: str
    start_date: str
    end_date: str
    stage: str
    request_window_days: int
    provider_profile_version: str = "v1"
    page_cap: int | None = None
    request_cap: int | None = None
    item_cap: int | None = None
    endpoint_names: tuple[str, ...] | None = None

    def to_identity(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "subjects": list(self.subjects),
            "date_domain": self.date_domain,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "stage": self.stage,
            "request_window_days": self.request_window_days,
            "provider_profile_version": self.provider_profile_version,
            "page_cap": self.page_cap,
            "request_cap": self.request_cap,
            "item_cap": self.item_cap,
            "endpoint_names": list(self._endpoint_names()),
        }

    def _endpoint_names(self, subject: str | None = None) -> tuple[str, ...]:
        if self.endpoint_names is not None:
            return tuple(self.endpoint_names)
        if self.source_type == "fred_macro":
            return (subject,) if subject is not None else tuple(self.subjects)
        return DEFAULT_ENDPOINTS.get(self.source_type, (self.source_type,))

    def _windows(self) -> list[tuple[str, str]]:
        if self.date_domain == "as_of":
            if self.start_date != self.end_date:
                raise ValueError("as_of scopes require start_date == end_date")
            return [(self.end_date, self.end_date)]
        if self.date_domain in {"trading_sessions", "calendar_days"}:
            start = date.fromisoformat(self.start_date)
            end = date.fromisoformat(self.end_date)
            windows: list[tuple[str, str]] = []
            cur = start
            while cur <= end:
                win_end = min(end, cur + timedelta(days=self.request_window_days - 1))
                windows.append((cur.isoformat(), win_end.isoformat()))
                cur = win_end + timedelta(days=1)
            return windows
        raise ValueError(f"unknown date domain: {self.date_domain}")

    def expand_cells(self) -> list[SourceCell]:
        cells: list[SourceCell] = []
        for subject in self.subjects:
            for window_start, window_end in self._windows():
                for endpoint_name in self._endpoint_names(subject):
                    cells.append(
                        SourceCell.create(
                            self.stage,
                            self.source_type,
                            endpoint_name,
                            subject,
                            window_start,
                            window_end,
                            self.date_domain,
                            self.provider_profile_version,
                            page_cap=self.page_cap,
                            item_cap=self.item_cap,
                        )
                    )
        return cells


@dataclass(frozen=True)
class UniverseSpec:
    schema_version: str
    universe_name: str
    tickers: list[str]
    companies: dict[str, dict[str, Any]]
    source_policy_expectations: dict[str, Any]
    semantic_hash: str

    def compute_semantic_hash(self) -> str:
        payload = {
            "schema_version": self.schema_version,
            "universe_name": self.universe_name,
            "tickers": self.tickers,
            "companies": self.companies,
            "source_policy_expectations": self.source_policy_expectations,
        }
        return sha256_identity(payload)


@dataclass(frozen=True)
class UniverseManifest:
    schema_version: str
    runtime_manifest_id: str
    universe_spec_semantic_hash: str
    tickers: list[str]
    discovery_artifact_hashes: dict[str, str]
    observed_provider_capabilities: dict[str, Any]
    source_windows: dict[str, str]
    coverage_status_summary: dict[str, Any]
    approved_at: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _validate_hex64(value: str, field: str) -> None:
    if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise ValueError(f"{field} must be lowercase sha256 hex")


def load_universe_spec(path: Path) -> UniverseSpec:
    raw = json.loads(Path(path).read_text())
    for field in ("schema_version", "universe_name", "tickers", "companies", "source_policy_expectations", "semantic_hash"):
        if field not in raw:
            raise ValueError(f"UniverseSpec missing {field}")
    spec = UniverseSpec(
        schema_version=raw["schema_version"],
        universe_name=raw["universe_name"],
        tickers=list(raw["tickers"]),
        companies=dict(raw["companies"]),
        source_policy_expectations=dict(raw["source_policy_expectations"]),
        semantic_hash=raw["semantic_hash"],
    )
    if spec.tickers != list(RATIFIED_TICKERS):
        raise ValueError("UniverseSpec ticker order differs from ratified 40-name contract")
    if len(set(spec.tickers)) != 40:
        raise ValueError("UniverseSpec tickers must be unique")
    for ticker in spec.tickers:
        company = spec.companies.get(ticker)
        if not isinstance(company, dict):
            raise ValueError(f"missing company record for {ticker}")
        for field in ("legal_name", "cik", "sector", "peer_group", "issuer_class", "filing_form_profile"):
            if not company.get(field):
                raise ValueError(f"missing {field} for {ticker}")
        legal_name = str(company["legal_name"]).strip()
        if legal_name == f"{ticker} issuer" or legal_name.lower().endswith(" issuer"):
            raise ValueError(f"placeholder legal_name for {ticker}")
        cik = str(company["cik"])
        if not (cik.isdigit() and 1 <= len(cik) <= 10):
            raise ValueError(f"invalid CIK for {ticker}")
    expected = spec.compute_semantic_hash()
    if spec.semantic_hash != expected:
        raise ValueError("UniverseSpec semantic hash drift")
    return spec


def build_universe_manifest(
    spec: UniverseSpec,
    *,
    discovery_artifact_hashes: Mapping[str, str],
    observed_provider_capabilities: Mapping[str, object],
    source_windows: SourceWindows,
    coverage_status_summary: Mapping[str, object],
    approved_at,
    created_at,
) -> UniverseManifest:
    for key, digest in discovery_artifact_hashes.items():
        _validate_hex64(str(digest), f"discovery_artifact_hashes.{key}")
    approved = approved_at.isoformat() if hasattr(approved_at, "isoformat") else str(approved_at)
    created = created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at)
    identity = {
        "schema_version": "1.0.0",
        "universe_spec_semantic_hash": spec.semantic_hash,
        "tickers": spec.tickers,
        "discovery_artifact_hashes": dict(sorted(discovery_artifact_hashes.items())),
        "observed_provider_capabilities": observed_provider_capabilities,
        "source_windows": source_windows.to_identity(),
        "coverage_status_summary": coverage_status_summary,
    }
    return UniverseManifest(
        schema_version="1.0.0",
        runtime_manifest_id=sha256_identity(identity),
        universe_spec_semantic_hash=spec.semantic_hash,
        tickers=spec.tickers,
        discovery_artifact_hashes=dict(sorted(discovery_artifact_hashes.items())),
        observed_provider_capabilities=dict(observed_provider_capabilities),
        source_windows=source_windows.to_identity(),
        coverage_status_summary=dict(coverage_status_summary),
        approved_at=approved,
        created_at=created,
    )


def build_b2o_source_scopes(spec: UniverseSpec, source_windows: SourceWindows) -> dict[str, SourceScope]:
    tickers = tuple(spec.tickers)
    return {
        "polygon_ohlcv": SourceScope("polygon_ohlcv", tickers, "trading_sessions", source_windows.degraded_start, source_windows.canonical_end, "market", 90),
        "polygon_news": SourceScope("polygon_news", tickers, "calendar_days", source_windows.degraded_start, source_windows.canonical_end, "evidence", 7, page_cap=20, item_cap=1000),
        "finnhub_company_news": SourceScope("finnhub_company_news", tickers, "calendar_days", source_windows.canonical_start, source_windows.canonical_end, "evidence", 7, item_cap=250),
        "sec_filings": SourceScope("sec_filings", tickers, "as_of", source_windows.canonical_end, source_windows.canonical_end, "evidence", 1),
        "fmp_fundamentals": SourceScope("fmp_fundamentals", tickers, "as_of", source_windows.canonical_end, source_windows.canonical_end, "evidence", 1),
        "fred_macro": SourceScope("fred_macro", tuple(sorted(FETCHED_SERIES)), "as_of", source_windows.canonical_end, source_windows.canonical_end, "evidence", 1),
    }


def terminal_complete(status: str | None, is_complete: int | bool | None) -> bool:
    return status in {"success", "success_empty"} and bool(is_complete)
