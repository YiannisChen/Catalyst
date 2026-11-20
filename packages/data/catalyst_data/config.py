from __future__ import annotations

import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_DB_PATH = Path("data") / "catalyst_dev.db"


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw is not None else default


def _csv_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if raw is None:
        return default
    values = tuple(part.strip() for part in raw.split(",") if part.strip())
    return values or default


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
CROSS_SOURCE_PRIORITY = _csv_env(
    "CATALYST_CROSS_SOURCE_PRIORITY",
    ("polygon_news", "fmp_news", "finnhub_company_news", "gdelt_news"),
)

# B6 model pins (Pre-B6 binding; GPU not invoked offline)
BGE_M3_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"
BGE_RERANKER_REVISION = "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"
BGE_M3_DIMENSION = 1024
