"""Shared retry-with-backoff for synchronous LangGraph nodes.

Provides ``invoke_with_retries`` which wraps an LLM ``.invoke()`` call with
exponential backoff and event-loop safety checks.

LangGraph runs sync nodes in a thread-pool executor, so ``time.sleep`` is
safe in normal operation. However, if a sync node is accidentally called
from the async event-loop thread, ``time.sleep`` would block the entire
loop.  ``_safe_sleep`` detects this and raises ``RuntimeError`` immediately
so the misconfiguration is surfaced at dev time rather than silently
degrading throughput in production.

Spec reference: Section 4.3 — Shared retry logic for Critic & Judge.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 0.1
_JSON_REPAIR_HINT = (
    "\n\nIMPORTANT: Previous response had invalid JSON. "
    "Return ONLY the JSON object, keep reasoning brief (max 15 words per chunk)."
)


def _safe_sleep(seconds: float) -> None:
    """Sleep for *seconds*, but refuse to block a running event loop.

    Raises:
        RuntimeError: If called from within a running ``asyncio`` event loop.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — safe to block.
        loop = None

    if loop is not None:
        raise RuntimeError(
            f"_safe_sleep({seconds}) called from a running asyncio event loop. "
            "Blocking sleep would freeze the loop. Either run this node in a "
            "thread-pool executor or convert to an async node."
        )
    time.sleep(seconds)


def invoke_with_retries(
    llm: Any,
    prompt: str,
    *,
    parse_fn: Callable[[str], dict],
    node_name: str = "node",
    retry_prompt_fn: Callable[[str, int], str] | None = None,
) -> tuple[Any, dict]:
    """Invoke *llm* with retries, parsing the response on each attempt.

    Args:
        llm:        LLM client with ``.invoke(prompt) -> response``.
        prompt:     The prompt string to send.
        parse_fn:   ``(response.content: str) -> dict``. Must raise on bad output.
        node_name:  Label used in log messages.

    Returns:
        ``(response, parsed_dict)`` on success.

    Raises:
        RuntimeError: If all attempts are exhausted. The ``__cause__`` is the
            last exception encountered.
    """
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        effective_prompt = prompt
        if attempt > 0 and retry_prompt_fn is not None:
            effective_prompt = retry_prompt_fn(prompt, attempt)
        elif attempt > 0 and _is_jsonish_error(last_error):
            effective_prompt = prompt + _JSON_REPAIR_HINT
        try:
            response = llm.invoke(effective_prompt)
            parsed = parse_fn(response.content)
            return response, parsed
        except Exception as exc:
            last_error = exc
            logger.warning(
                "%s attempt %d/%d failed: %s",
                node_name, attempt + 1, MAX_RETRIES, exc,
            )
            if attempt < MAX_RETRIES - 1:
                _safe_sleep(BASE_BACKOFF_SECONDS * (2 ** attempt))

    raise RuntimeError(
        f"{node_name} failed after {MAX_RETRIES} attempts: {last_error}"
    ) from last_error


def _is_jsonish_error(exc: Exception | None) -> bool:
    if exc is None:
        return False
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    return (
        "json" in name
        or "decode" in name
        or "validationerror" in name
        or "invalid json" in msg
        or "unterminated string" in msg
    )
