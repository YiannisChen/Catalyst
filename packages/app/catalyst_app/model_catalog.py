"""Sanitized model catalog for the self-hosted BYOK workbench.

Returns provider/model metadata (ids, labels, tiers).  Never includes
env key values.  The catalog is curated — not live-fetched — to keep
the self-hosted surface stable and not dependent on external API calls.
"""
from __future__ import annotations

from catalyst_app.env_loader import is_env_key_configured
from catalyst_app.schemas import CatalogModel, CatalogProvider, ModelCatalogResponse


def build_catalog() -> ModelCatalogResponse:
    """Return the curated model catalog with env-key status per provider."""
    return ModelCatalogResponse(providers=[
        CatalogProvider(
            id="aihubmix",
            label="AIHubMix",
            env_key_configured=is_env_key_configured("aihubmix"),
            default_model_id="gemini-2.5-flash-nothink",
            models=[
                CatalogModel(id="gemini-2.5-flash-nothink", label="Gemini 2.5 Flash", tier="value", recommended=True),
                CatalogModel(id="deepseek-v4-flash", label="DeepSeek V4 Flash", tier="value"),
                CatalogModel(id="claude-opus-4-6", label="Claude Opus 4.6", tier="quality"),
                CatalogModel(id="qwen3.6-flash", label="Qwen3.6 Flash", tier="value"),
            ],
        ),
        CatalogProvider(
            id="siliconflow",
            label="SiliconFlow",
            env_key_configured=is_env_key_configured("siliconflow"),
            default_model_id="deepseek-v4-flash",
            models=[
                CatalogModel(id="deepseek-v4-flash", label="DeepSeek V4 Flash", tier="value", recommended=True),
                CatalogModel(id="Qwen/Qwen3.6", label="Qwen3.6", tier="value"),
                CatalogModel(id="Pro/deepseek-ai/DeepSeek-V3", label="DeepSeek V3", tier="quality"),
            ],
        ),
        CatalogProvider(
            id="openai",
            label="OpenAI",
            env_key_configured=is_env_key_configured("openai"),
            default_model_id="gpt-4o-mini",
            models=[
                CatalogModel(id="gpt-4o-mini", label="GPT-4o Mini", tier="value", recommended=True),
                CatalogModel(id="gpt-4o", label="GPT-4o", tier="quality"),
            ],
        ),
        CatalogProvider(
            id="deepseek",
            label="DeepSeek",
            env_key_configured=is_env_key_configured("deepseek"),
            default_model_id="deepseek-v4-flash",
            models=[
                CatalogModel(id="deepseek-v4-flash", label="DeepSeek V4 Flash", tier="value", recommended=True),
                CatalogModel(id="deepseek-chat", label="DeepSeek Chat", tier="value"),
            ],
        ),
        CatalogProvider(
            id="glm",
            label="Z.AI / GLM",
            env_key_configured=is_env_key_configured("glm"),
            default_model_id="GLM-4.7-FlashX",
            models=[
                CatalogModel(id="GLM-4.7-FlashX", label="GLM-4.7 FlashX", tier="value", recommended=True),
                CatalogModel(id="GLM-5.2", label="GLM-5.2", tier="quality"),
            ],
        ),
        CatalogProvider(
            id="custom_openai_compatible",
            label="Custom (OpenAI-compatible)",
            executable=True,
            env_key_configured=False,
            default_model_id=None,
            models=[],
        ),
    ])
