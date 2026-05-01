from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_DB_PATH = Path("data") / "catalyst_dev.db"


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw is not None else default


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw is not None else default


def _csv_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if raw is None:
        return default
    values = tuple(part.strip() for part in raw.split(",") if part.strip())
    return values or default


@dataclass
class RatePolicy:
    min_interval_sec: float  # minimum time between requests
    max_concurrent: int  # asyncio.Semaphore limit
    daily_budget: int | None  # None = unlimited


def db_path() -> Path:
    """Resolve the active Catalyst SQLite database path."""
    configured = Path(os.environ["CATALYST_DB_PATH"]).expanduser() if os.environ.get(
        "CATALYST_DB_PATH"
    ) else _DEFAULT_DB_PATH
    return configured if configured.is_absolute() else _REPO_ROOT / configured


RAG_MIN_CHAR_COUNT = _int_env("CATALYST_RAG_MIN_CHAR_COUNT", 200)
TARGET_LANGUAGE = os.environ.get("CATALYST_TARGET_LANGUAGE", "en")
TEMPLATE_SPAM_DUPLICATE_THRESHOLD = _int_env(
    "CATALYST_TEMPLATE_SPAM_DUPLICATE_THRESHOLD", 5
)
FALLBACK_PRICE_MOVE_THRESHOLD = _float_env(
    "CATALYST_FALLBACK_PRICE_MOVE_THRESHOLD", 0.03
)
FALLBACK_RETRY_THRESHOLD = _int_env("CATALYST_FALLBACK_RETRY_THRESHOLD", 3)
CROSS_SOURCE_PRIORITY = _csv_env(
    "CATALYST_CROSS_SOURCE_PRIORITY",
    ("polygon_news", "fmp_news", "finnhub_company_news", "gdelt_news"),
)


RATE_POLICIES = {
    "dev": {
        "polygon": RatePolicy(12.0, 1, None),  # 5/min
        "finnhub": RatePolicy(1.0, 3, None),  # 60/min
        "fred": RatePolicy(0.5, 3, None),  # 120/min
        "gdelt": RatePolicy(5.5, 1, None),  # sequential only
        "fmp": RatePolicy(1.0, 2, 250),  # 250/day
        "sec": RatePolicy(0.2, 3, None),  # 5/sec conservative
        "yfinance": RatePolicy(2.0, 1, None),  # treat gently
    },
    "test": {},  # all mocked, no network
}
