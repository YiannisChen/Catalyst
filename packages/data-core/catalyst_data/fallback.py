"""FallbackPolicy — provider chain for failed primary fetches.

H3 owns this module.  Encodes polygon_news→finnhub_company_news chain.
Excludes AUTH/RATE_LIMIT/BUDGET_EXHAUSTED (operator errors, not provider issues).
SEC/FRED excluded (unique data).  YFinance/Tiingo diagnostic-only.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_FALLBACK_CHAIN: tuple[str, ...] = ("polygon_news", "finnhub_company_news")

EXCLUDED_ERROR_CLASSES: tuple[str, ...] = (
    "auth", "permission_paid", "rate_limit", "budget_exhausted",
)


# W1-C: per-source-type chains
OHLCV_FALLBACK_CHAIN: tuple[str, ...] = ("polygon_ohlcv", "yfinance_ohlcv")

@dataclass(frozen=True)
class FallbackPolicy:
    chain: tuple[str, ...] = DEFAULT_FALLBACK_CHAIN
    ohlcv_chain: tuple[str, ...] = OHLCV_FALLBACK_CHAIN
    excluded_error_classes: tuple[str, ...] = EXCLUDED_ERROR_CLASSES
    max_fallback_depth: int = 1

    def next_provider(self, current: str) -> str | None:
        chain = self.ohlcv_chain if current.endswith("_ohlcv") else self.chain
        """Return the next provider in the chain after *current*, or None."""
        try:
            idx = chain.index(current)
        except ValueError:
            return None
        if idx + 1 >= len(chain):
            return None
        if idx + 1 >= self.max_fallback_depth + 1:
            return None
        return chain[idx + 1]

    def should_attempt(self, error_class: str, depth: int) -> bool:
        """True if fallback should be attempted for this error class at this depth."""
        if depth >= self.max_fallback_depth:
            return False
        if error_class in self.excluded_error_classes:
            return False
        return True
