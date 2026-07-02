from __future__ import annotations

from catalyst_data.config import RATE_POLICIES

_DEV_POLICIES = RATE_POLICIES["dev"]

POLYGON = {
    "rate_per_min": int(round(60.0 / _DEV_POLICIES["polygon"].min_interval_sec)),
    "burst": 5,
    "concurrency": _DEV_POLICIES["polygon"].max_concurrent,
}

FMP = {
    "rate_per_day": _DEV_POLICIES["fmp"].daily_budget,
    "concurrency": _DEV_POLICIES["fmp"].max_concurrent,
}

FRED = {
    "rate_per_min": int(round(60.0 / _DEV_POLICIES["fred"].min_interval_sec)),
    "concurrency": _DEV_POLICIES["fred"].max_concurrent,
}


FINNHUB = {
    "rate_per_min": int(round(60.0 / _DEV_POLICIES["finnhub"].min_interval_sec)),
    "concurrency": _DEV_POLICIES["finnhub"].max_concurrent,
}
SEC = {
    "rate_per_min": int(round(60.0 / _DEV_POLICIES["sec"].min_interval_sec)),
    "concurrency": _DEV_POLICIES["sec"].max_concurrent,
}
