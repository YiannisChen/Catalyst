from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_DB_PATH = Path("data") / "catalyst_dev.db"


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
