"""Explicit .env loader with canonical key normalization for self-hosted BYOK.

Loads .env files from repo root, packages/data-core, and packages/app with
correct priority (process env always wins).  Normalizes lowercase/alias keys
to canonical uppercase variants and writes them back to os.environ.

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
    "GLM_API_KEY":         ["glm_api_key", "GLM_API_KEY", "zai_api_key", "ZAI_API_KEY"],
    "GEMINI_API_KEY":      ["GEMINI_API_KEY"],
    "ANTHROPIC_API_KEY":   ["anthropic_api_key", "ANTHROPIC_API_KEY"],
}

# Provider → canonical env key name
_PROVIDER_ENV_MAP: dict[str, str] = {
    "aihubmix":    "AIHUBMIX_API_KEY",
    "siliconflow": "SILICONFLOW_API_KEY",
    "openai":      "OPENAI_API_KEY",
    "deepseek":    "DEEPSEEK_API_KEY",
    "glm":         "GLM_API_KEY",
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
    """Load .env files with correct priority.  Call once at app startup.

    Priority (highest wins):
      1. repo-root .env          (lowest)
      2. packages/data-core/.env
      3. packages/app/.env       (if exists)
      4. real os.environ         (highest — never overwritten)
    """
    repo_root = Path(__file__).resolve().parent.parent.parent.parent

    # Step 1 – parse every available .env file into a temp dict
    parsed: dict[str, str] = {}
    for env_path in (
        repo_root / ".env",
        repo_root / "packages" / "data-core" / ".env",
        repo_root / "packages" / "app" / ".env",
    ):
        parsed.update(_parse_dotenv(env_path))  # later file wins within dotenv tier

    # Step 2 – overlay real os.environ (highest priority)
    parsed.update(os.environ)

    # Step 3 – normalize aliases → canonical keys, write to os.environ
    for canonical, aliases in _ENV_KEY_ALIASES.items():
        for alias in aliases:
            val = parsed.get(alias)
            if val:
                os.environ[canonical] = val
                break  # first match wins

    # Step 4 – also set lowercase aliases for code that reads them directly
    for canonical, aliases in _ENV_KEY_ALIASES.items():
        val = os.environ.get(canonical)
        if val:
            for alias in aliases:
                os.environ.setdefault(alias, val)


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
