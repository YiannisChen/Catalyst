"""Shared defaults for ingestion runtime stores.

Path resolution is deliberately side-effect-free: it never creates directories or
opens stores. Clean checkouts use a local runtime root; a detected canonical legacy
store keeps the full family on its existing root.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

CORE_DATA_DIR = Path("data/m9")
LOCAL_DATA_DIR = Path(".local/data/ingestion")
RUNTIME_DATA_DIR_ENV = "CATALYST_RUNTIME_DATA_DIR"
STORE_ENV = {
    "market_db": "CATALYST_MARKET_CONTEXT_DB",
    "bodies_db": "CATALYST_NEWS_BODIES_DB",
    "jobs_db": "CATALYST_UPDATE_JOBS_DB",
    "archive_db": "CATALYST_HEADLINE_ARCHIVE_DB",
    "web_search_db": "CATALYST_WEB_SEARCH_DB",
    "corp_actions_db": "CATALYST_CORP_ACTIONS_DB",
    "news_categories_db": "CATALYST_NEWS_CATEGORIES_DB",
    "media_db": "CATALYST_MEDIA_DB",
    "sec_submissions_dir": "CATALYST_SEC_SUBMISSIONS_DIR",
    "sec_earnings_cache": "CATALYST_SEC_EARNINGS_CACHE",
}
STORE_FILENAMES = {
    "market_db": "market_context.sqlite3",
    "bodies_db": "news_bodies_v2.sqlite3",
    "jobs_db": "update_jobs.sqlite3",
    "archive_db": "headline_archive.sqlite3",
    "web_search_db": "web_search.sqlite3",
    "corp_actions_db": "corp_actions.sqlite3",
    "news_categories_db": "news_categories.sqlite3",
    "media_db": "media_cache.sqlite3",
    "sec_submissions_dir": "sec_submissions",
    "sec_earnings_cache": "sec_earnings",
}
LEGACY_DATA_MARKERS = (*STORE_FILENAMES.values(), "sec_company_tickers.json")


def repository_root() -> Path:
    """Return the checkout root from this module's installed source path."""
    return Path(__file__).resolve().parents[4]


def runtime_data_dir(
    repo: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Resolve the shared ingestion data root without touching disk.

    ``CATALYST_RUNTIME_DATA_DIR`` has highest priority. Without it, the resolver
    keeps the full store family under ``repo/data/m9`` if any known runtime store,
    cache directory, or ticker-map marker exists there; otherwise clean checkouts
    use ``repo/.local/data/ingestion``.
    Relative root overrides are anchored to ``repo`` (or the source checkout root),
    while per-store overrides keep their existing cwd-relative semantics.
    ``CATALYST_DATA_DIR`` remains the separate daily status-artifact setting.
    """
    root = Path(repo) if repo is not None else repository_root()
    env = os.environ if environ is None else environ
    configured = env.get(RUNTIME_DATA_DIR_ENV, "").strip()
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_absolute() else root / path
    legacy_root = root / CORE_DATA_DIR
    if any((legacy_root / marker).exists() for marker in LEGACY_DATA_MARKERS):
        return legacy_root
    return root / LOCAL_DATA_DIR


def default_sec_tickers_path(
    repo: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Return the SEC ticker map path under the configured runtime data root."""
    return runtime_data_dir(repo, environ) / "sec_company_tickers.json"


def runtime_store_path(
    store: str,
    repo: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Resolve one store, honoring its individual override before the root."""
    try:
        env_name = STORE_ENV[store]
        filename = STORE_FILENAMES[store]
    except KeyError as exc:
        raise ValueError(f"unknown core runtime store: {store}") from exc

    env = os.environ if environ is None else environ
    configured = env.get(env_name, "").strip()
    if configured:
        return Path(configured).expanduser()
    return runtime_data_dir(repo, env) / filename
