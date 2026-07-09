"""
JudgeBase — shared infrastructure for LLM-based evaluation judges.

Cache key: SHA-256(case_id || "::" || arm || "::" || metric || "::" || rubric_version || "::" || prompt)
Cache path: packages/eval/eval_cache/judge_cache.json (tracked, JSON dodges *.sqlite gitignore)
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Cache key construction
# ---------------------------------------------------------------------------

def make_cache_key(
    case_id: str,
    arm: str,
    metric: str,
    rubric_version: str,
    prompt: str,
) -> str:
    """SHA-256 of the composite key: (case_id, arm, metric, rubric_version, prompt).

    A rubric_version change MUST invalidate cache. This function is the single
    source of truth for cache key derivation.
    """
    composite = f"{case_id}::{arm}::{metric}::{rubric_version}::{prompt}"
    return hashlib.sha256(composite.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Cache path
# ---------------------------------------------------------------------------

_CACHE_DIR = Path(__file__).resolve().parents[2] / "eval_cache"
_CACHE_PATH = _CACHE_DIR / "judge_cache.json"


def _load_cache() -> dict[str, Any]:
    """Load judge cache from disk. Returns empty dict on any failure."""
    try:
        if _CACHE_PATH.exists():
            return json.loads(_CACHE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _save_cache(cache: dict[str, Any]) -> None:
    """Atomically write judge cache to disk."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, indent=2, sort_keys=True))
    tmp.replace(_CACHE_PATH)


# ---------------------------------------------------------------------------
# JudgeBase
# ---------------------------------------------------------------------------

class JudgeBase:
    """Base class for LLM-based evaluation judges with caching.

    Subclasses set name, criteria, evaluation_steps, rubric, and call
    self._judge_with_cache(prompt) to get cached or fresh LLM verdicts.
    """

    name: str = ""
    criteria: str = ""
    evaluation_steps: list[str] = []
    rubric: dict[str, float] = {}
    rubric_version: str = "1.0"

    def __init__(self, judge_fn: Callable[[str], dict] | None = None):
        """Initialize with optional fixture judge_fn for testing.

        Args:
            judge_fn: Callable that takes a prompt string and returns a dict.
                      If None, live LLM must be configured by the caller.
        """
        self._judge_fn = judge_fn
        self._cache: dict[str, Any] = _load_cache()
        self._cache_hits = 0
        self._cache_misses = 0

    def _cached_judge(self, prompt: str, case_id: str, arm: str = "C") -> dict:
        """Judge with caching: check cache, call LLM on miss, persist.

        Args:
            prompt: The full LLM prompt string.
            case_id: Golden event ID.
            arm: Experiment arm identifier.

        Returns:
            Parsed judge verdict dict. Includes cache_hit=True on cache hit.
        """
        key = make_cache_key(
            case_id=case_id, arm=arm, metric=self.name,
            rubric_version=self.rubric_version, prompt=prompt,
        )

        if key in self._cache:
            self._cache_hits += 1
            entry = dict(self._cache[key])
            entry["cache_hit"] = True
            return entry

        self._cache_misses += 1
        if self._judge_fn is None:
            raise RuntimeError(
                f"No judge_fn configured for {self.name}; cannot evaluate without LLM."
            )

        verdict = self._judge_fn(prompt)
        verdict["cache_hit"] = False
        self._cache[key] = dict(verdict)
        _save_cache(self._cache)
        return verdict

    def flush_cache(self) -> None:
        """Persist current in-memory cache to disk."""
        _save_cache(self._cache)

    @property
    def cache_stats(self) -> dict[str, int]:
        return {"hits": self._cache_hits, "misses": self._cache_misses}
