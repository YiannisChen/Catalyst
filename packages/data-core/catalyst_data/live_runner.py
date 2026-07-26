"""Gated live-runner helpers — construct real connectors and inject into pipeline.

Guard order (per 3F.1 plan §C):
  1. --confirm short-circuit (exit 0, friendly message)
  2. Frozen DB refusal (realpath)
  3. Environment key check
  4. Redacted config echo
  5. Build connectors + inject fetch_fn

All keys are redacted — full keys never logged.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Callable

from catalyst_data.config import RatePolicy
from catalyst_data.rate_limiter import TokenBucketLimiter


def _redact_key(key: str | None) -> str:
    if not key:
        return "[not set]"
    if len(key) < 8:
        return "[hidden]"
    return f"{key[:4]}...[REDACTED]...{key[-4:]}"


def _echo_redacted_config(
    polygon_key: str | None = None,
    finnhub_key: str | None = None,
    fred_key: str | None = None,
    sec_user_agent: str | None = None,
) -> None:
    print("=== Live Config (keys redacted) ===")
    if polygon_key is not None:
        print(f"  POLYGON_API_KEY: {_redact_key(polygon_key)}")
    if finnhub_key is not None:
        print(f"  FINNHUB_API_KEY: {_redact_key(finnhub_key)}")
    if fred_key is not None:
        print(f"  FRED_API_KEY:    {_redact_key(fred_key)}")
    if sec_user_agent is not None:
        print(f"  SEC_USER_AGENT:  {sec_user_agent}")
    print()


def _check_env_key(var_name: str) -> str:
    value = os.environ.get(var_name, "").strip()
    if not value:
        raise ValueError(
            f"Environment variable {var_name} is not set or empty. "
            f"Set it before running --live."
        )
    return value


# ---------------------------------------------------------------------------
# Builder functions — one per provider
# ---------------------------------------------------------------------------

def build_polygon_fetcher(*, trust_env: bool = True) -> tuple[Callable, TokenBucketLimiter]:
    """Build a Polygon.io news fetcher with rate limiter (async)."""
    key = _check_env_key("POLYGON_API_KEY")
    policy = RatePolicy(min_interval_sec=12.0, max_concurrent=1, daily_budget=None)
    limiter = TokenBucketLimiter(policy)

    import httpx
    client = httpx.AsyncClient(timeout=30.0, trust_env=trust_env)
    from catalyst_data.connectors.polygon import create_polygon_fetcher
    fetcher = create_polygon_fetcher(api_key=key, limiter=limiter, client=client)

    _echo_redacted_config(polygon_key=key)
    return fetcher, limiter


def build_finnhub_fetcher(*, trust_env: bool = True) -> tuple[Callable, TokenBucketLimiter]:
    """Build a Finnhub company-news fetcher with rate limiter (async)."""
    key = _check_env_key("FINNHUB_API_KEY")
    policy = RatePolicy(min_interval_sec=1.0, max_concurrent=3, daily_budget=None)
    limiter = TokenBucketLimiter(policy)

    import httpx
    client = httpx.AsyncClient(timeout=30.0, trust_env=trust_env)
    from catalyst_data.connectors.finnhub import create_finnhub_fetcher
    fetcher_ns = create_finnhub_fetcher(api_key=key, limiter=limiter, client=client)

    _echo_redacted_config(finnhub_key=key)
    return fetcher_ns.fetch, limiter


def build_sec_fetcher(*, trust_env: bool = True) -> tuple[Any, TokenBucketLimiter]:
    """Build an SEC EDGAR fetcher with rate limiter (async)."""
    user_agent = _check_env_key("SEC_USER_AGENT")
    policy = RatePolicy(min_interval_sec=0.2, max_concurrent=3, daily_budget=None)
    limiter = TokenBucketLimiter(policy)

    import httpx
    client = httpx.AsyncClient(timeout=60.0, headers={"User-Agent": user_agent}, trust_env=trust_env)
    from catalyst_data.connectors.sec import create_sec_fetcher
    fetcher_ns = create_sec_fetcher(user_agent=user_agent, limiter=limiter, client=client)

    _echo_redacted_config(sec_user_agent=user_agent)
    return fetcher_ns, limiter


def build_fred_fetcher(*, trust_env: bool = True) -> tuple[Callable, TokenBucketLimiter]:
    """Build a FRED macro fetcher with rate limiter (async)."""
    key = _check_env_key("FRED_API_KEY")
    policy = RatePolicy(min_interval_sec=0.5, max_concurrent=3, daily_budget=None)
    limiter = TokenBucketLimiter(policy)

    import httpx
    client = httpx.AsyncClient(timeout=30.0, trust_env=trust_env)
    from catalyst_data.connectors.fred import create_fred_fetcher
    fetcher = create_fred_fetcher(api_key=key, limiter=limiter, client=client)

    _echo_redacted_config(fred_key=key)
    return fetcher, limiter


# ---------------------------------------------------------------------------
# Guard runner — shared by all --live CLI commands
# ---------------------------------------------------------------------------

def run_live_guard(
    db_path: str,
    *,
    required_keys: list[str] | None = None,
    confirm: bool = False,
) -> None:
    """Execute the ordered guard sequence for --live.

    1. --confirm short-circuit (BEFORE key checks)
    2. Frozen DB refusal (realpath)
    3. Environment key check (only for requested sources)
    4. Redacted config echo (done by build_*_fetcher functions)
    """
    # Guard 1: --confirm short-circuit FIRST
    if not confirm:
        print("Add --confirm to execute live network calls.")
        print("Without --confirm, this is a dry-run preview only.")
        sys.exit(0)

    # Guard 2: Frozen DB refusal
    resolved = os.path.realpath(db_path)
    frozen_real = os.path.realpath(
        str(Path(__file__).resolve().parent.parent.parent.parent
            / "data" / "catalyst_eval_frozen_v2.db")
    )
    if resolved == frozen_real:
        raise RuntimeError(
            "Refusing to write to frozen eval DB. "
            "Use the dev DB: data/catalyst_dev_ws4b.db"
        )

    # Guard 3: Environment key check
    if required_keys:
        for var_name in required_keys:
            _check_env_key(var_name)

    # Guard 4: redacted config echo is done by build_*_fetcher functions
