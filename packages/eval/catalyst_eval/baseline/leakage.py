"""Baseline leakage and secret scan (M1-7).

Conservative regex scanner for the sealed baseline report. Flags secret-like
key names, live provider key values, and golden-case answer fields/markers.
Violation strings identify the path and matched pattern but never embed the
secret or golden value.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

_SECRET_KEY = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|credential|authorization)"
)
_GOLDEN_KEY = re.compile(r"(?i)(oracle_|golden|gold_answer|expected_answer)")
_SECRET_VALUE = re.compile(
    r"(?i)\b(sk-[a-z0-9_-]{8,}|"
    r"(deepseek|openai|anthropic|aihubmix|siliconflow|gemini|glm|zai)_api_key\s*[:=]\s*\S+|"
    r"(?:api[_-]?key|token|secret|password|credential|authorization)"
    r"\s+(?:is\s+)?\S{6,})"
)


def _walk(
    node: Any,
    path: str,
    violations: list[str],
    golden_answer_markers: Iterable[str],
) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            child_path = f"{path}.{key}" if path else str(key)
            key_text = str(key)
            if _SECRET_KEY.search(key_text):
                violations.append(f"{child_path}: key name matches secret pattern")
            if _GOLDEN_KEY.search(key_text):
                violations.append(f"{child_path}: key name matches golden-case pattern")
            _walk(value, child_path, violations, golden_answer_markers)
        return
    if isinstance(node, list):
        for index, item in enumerate(node):
            _walk(item, f"{path}[{index}]", violations, golden_answer_markers)
        return
    if isinstance(node, str):
        if _SECRET_VALUE.search(node):
            violations.append(f"{path}: value matches provider-key pattern")
        for marker in golden_answer_markers:
            if marker and marker in node:
                violations.append(f"{path}: value embeds a golden-case answer marker")
        return


def scan_baseline_report(
    report: dict,
    *,
    golden_answer_markers: Iterable[str] = (),
) -> list[str]:
    """Return leakage violation strings for a baseline report dict.

    Findings identify paths/patterns only; secret or golden values are never
    included in the returned violations.
    """
    violations: list[str] = []
    _walk(report, "$", violations, tuple(golden_answer_markers))
    return violations


__all__ = ["scan_baseline_report"]
