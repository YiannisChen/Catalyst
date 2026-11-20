"""Load the repository-root .env with canonical key normalization for BYOK.

Process environment variables take priority over the local file. Lowercase
and provider-specific aliases are normalized to canonical uppercase names.

Never logs, returns, or exposes API key values.
"""
from __future__ import annotations

import os
from pathlib import Path

# Canonical uppercase key → list of accepted aliases (first match wins)
_ENV_KEY_ALIASES: dict[str, list[str]] = {
    "AIHUBMIX_API_KEY":   ["aihubmix_api_key", "AIHUBMIX_API_KEY"],
    "SILICONFLOW_API_KEY": ["siliconflow_api_key", "SILICONFLOW_API_KEY"],
    "OPENAI_API_KEY":      ["openai_api_key", "OPENAI_API_KEY"],
    "DEEPSEEK_API_KEY":    ["deepseek_api_key", "DEEPSEEK_API_KEY"],
    "XAI_API_KEY":         ["xai_api_key", "XAI_API_KEY"],
    "QWEN_API_KEY":        ["qwen_api_key", "QWEN_API_KEY", "DASHSCOPE_API_KEY"],
    "GLM_API_KEY":         ["glm_api_key", "GLM_API_KEY", "zai_api_key", "ZAI_API_KEY"],
    "GEMINI_API_KEY":      ["GEMINI_API_KEY"],
    "ANTHROPIC_API_KEY":   ["anthropic_api_key", "ANTHROPIC_API_KEY"],
    "TAVILY_API_KEY":      ["tavily_api_key", "TAVILY_API_KEY"],
}

# Provider → canonical env key name
_PROVIDER_ENV_MAP: dict[str, str] = {
    "aihubmix":    "AIHUBMIX_API_KEY",
    "siliconflow": "SILICONFLOW_API_KEY",
    "openai":      "OPENAI_API_KEY",
    "deepseek":    "DEEPSEEK_API_KEY",
    "xai":        "XAI_API_KEY",
    "qwen":       "QWEN_API_KEY",
    "gemini":     "GEMINI_API_KEY",
    "anthropic":  "ANTHROPIC_API_KEY",
    "glm":         "GLM_API_KEY",
    "tavily":     "TAVILY_API_KEY",
}


def _parse_dotenv(path: Path) -> dict[str, str]:
    """Parse a dotenv file into a plain dict.  Never touches os.environ."""
    result: dict[str, str] = {}
    if not path.is_file():
        return result
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value:
            result[key] = value
    return result


def load_env_files() -> None:
    """Load the root .env as defaults; preserve the real process environment."""
    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    file_values = _parse_dotenv(repo_root / ".env")
    process_values = dict(os.environ)
    for key, value in file_values.items():
        os.environ.setdefault(key, value)

    # Resolve aliases in the process environment before considering file values.
    for canonical, aliases in _ENV_KEY_ALIASES.items():
        value = process_values.get(canonical)
        if not value:
            value = next((process_values.get(alias) for alias in aliases if process_values.get(alias)), None)
        if value is None:
            value = next((file_values.get(alias) for alias in aliases if file_values.get(alias)), None)
        if value:
            os.environ[canonical] = value

    # Keep aliases available for callers that still read them directly.
    for canonical, aliases in _ENV_KEY_ALIASES.items():
        val = os.environ.get(canonical)
        if val:
            for alias in aliases:
                if alias not in process_values:
                    os.environ[alias] = val


def get_provider_env_key(provider: str) -> str | None:
    """Return the canonical env-var name for *provider*, or None."""
    return _PROVIDER_ENV_MAP.get(provider)


def is_env_key_configured(provider: str) -> bool:
    """True when the provider's env key is present and non-empty.

    Never returns or logs the key value.
    """
    key_name = _PROVIDER_ENV_MAP.get(provider)
    if not key_name:
        return False
    return bool(os.environ.get(key_name, "").strip())
