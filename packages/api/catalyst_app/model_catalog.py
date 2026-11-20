"""Sanitized, curated BYOK model and search-provider catalog."""
from __future__ import annotations

from catalyst_app.env_loader import is_env_key_configured
from catalyst_app.llm_factory import PROVIDER_BASE_URLS
from catalyst_app.schemas import (
    CatalogModel,
    CatalogProvider,
    ModelCatalogResponse,
    SearchProvider,
)


def _model(
    provider: str,
    model_id: str,
    label: str,
    *,
    tool_calling: bool = True,
    json_mode: bool = True,
    streaming_tool_args: bool = True,
    native_web_search: str = "undated",
    supported: bool = True,
    support_reason: str | None = None,
    evaluated: bool = False,
    eval_ref: str | None = None,
    tier: str | None = None,
    recommended: bool = False,
) -> CatalogModel:
    return CatalogModel(
        provider=provider,
        model_id=model_id,
        id=model_id,
        label=label,
        tool_calling=tool_calling,
        json_mode=json_mode,
        streaming_tool_args=streaming_tool_args,
        native_web_search=native_web_search,
        supported=supported,
        support_reason=support_reason,
        evaluated=evaluated,
        eval_ref=eval_ref,
        tier=tier,
        recommended=recommended,
    )


def _provider(
    provider_id: str,
    label: str,
    models: list[CatalogModel],
    *,
    default_model_id: str | None = None,
    executable: bool = True,
) -> CatalogProvider:
    return CatalogProvider(
        id=provider_id,
        label=label,
        executable=executable,
        env_key_configured=is_env_key_configured(provider_id),
        default_model_id=default_model_id,
        models=models,
    )


def build_catalog() -> ModelCatalogResponse:
    """Build a stable catalog without exposing provider credentials."""
    anthropic_reason = "Native Anthropic Messages adapter is not implemented."
    providers = [
        _provider("openai", "OpenAI", [
            _model("openai", "gpt-4o-mini", "GPT-4o Mini", tier="value", recommended=True),
            _model("openai", "gpt-4o", "GPT-4o", tier="quality"),
        ], default_model_id="gpt-4o-mini"),
        _provider("anthropic", "Anthropic", [
            _model("anthropic", "claude-sonnet-4-5", "Claude Sonnet 4.5", supported=False,
                   support_reason=anthropic_reason),
        ], executable=False),
        _provider("xai", "xAI", [
            _model("xai", "grok-3-mini", "Grok 3 Mini", tier="value", recommended=True),
            _model("xai", "grok-3", "Grok 3", tier="quality"),
        ], default_model_id="grok-3-mini"),
        _provider("deepseek", "DeepSeek", [
            _model("deepseek", "deepseek-flash", "DeepSeek Flash", evaluated=True,
                   eval_ref="/api/quality", tier="value"),
            _model("deepseek", "deepseek-v4-pro", "DeepSeek V4 Pro", tier="quality"),
            _model("deepseek", "deepseek-v4-flash", "DeepSeek V4 Flash", tier="value", recommended=True),
            _model("deepseek", "deepseek-chat", "DeepSeek Chat", tier="value"),
        ], default_model_id="deepseek-v4-flash"),
        # Model ids as configured in valuecell (DashScope compatible mode); qwen-flash replaces qwen-turbo.
        _provider("qwen", "Qwen", [
            _model("qwen", "qwen-flash", "Qwen Flash", tier="value", recommended=True),
            _model("qwen", "qwen-plus", "Qwen Plus", tier="value"),
            _model("qwen", "qwen3-max", "Qwen3 Max", tier="quality"),
        ], default_model_id="qwen-flash"),
        _provider("gemini", "Gemini (OpenAI shim, beta)", [
            _model("gemini", "gemini-2.5-flash", "Gemini 2.5 Flash", tier="value", recommended=True),
            _model("gemini", "gemini-2.5-pro", "Gemini 2.5 Pro", tier="quality"),
        ], default_model_id="gemini-2.5-flash"),
        # Keep existing configured OpenAI-compatible choices available while
        # exposing the same explicit capability contract for each model.
        _provider("aihubmix", "AIHubMix", [
            _model("aihubmix", "gemini-2.5-flash-nothink", "Gemini 2.5 Flash", native_web_search="none", tier="value", recommended=True),
            _model("aihubmix", "deepseek-v4-flash", "DeepSeek V4 Flash", native_web_search="none", tier="value"),
            _model("aihubmix", "claude-opus-4-6", "Claude Opus 4.6", native_web_search="none", tier="quality"),
            _model("aihubmix", "qwen3.6-flash", "Qwen3.6 Flash", native_web_search="none", tier="value"),
        ], default_model_id="gemini-2.5-flash-nothink"),
        # SiliconFlow ids carry the publisher prefix; these are the ones valuecell configures.
        _provider("siliconflow", "SiliconFlow", [
            _model("siliconflow", "deepseek-ai/DeepSeek-V3.1-Terminus", "DeepSeek V3.1 Terminus", native_web_search="none", tier="value", recommended=True),
            _model("siliconflow", "moonshotai/Kimi-K2-Instruct-0905", "Kimi K2 Instruct", native_web_search="none", tier="quality"),
            _model("siliconflow", "zai-org/GLM-4.5", "GLM-4.5", native_web_search="none", tier="value"),
        ], default_model_id="deepseek-ai/DeepSeek-V3.1-Terminus"),
        _provider("glm", "Z.AI / GLM", [
            _model("glm", "GLM-4.7-FlashX", "GLM-4.7 FlashX", native_web_search="none", tier="value", recommended=True),
            _model("glm", "GLM-5.2", "GLM-5.2", native_web_search="none", tier="quality"),
        ], default_model_id="GLM-4.7-FlashX"),
        CatalogProvider(
            id="custom_openai_compatible",
            label="Custom (OpenAI-compatible)",
            executable=True,
            env_key_configured=False,
            default_model_id=None,
            models=[],
        ),
    ]
    return ModelCatalogResponse(
        providers=providers,
        search_providers=[SearchProvider(
            id="tavily",
            date_filter=True,
            evaluated_for_attribution=False,
            role="leads",
        )],
    )


def catalog_provider_base_url(provider: str) -> str | None:
    """Return the catalog provider's known base URL, if it has one.

    ``custom_openai_compatible`` and unsupported catalog entries intentionally
    have no known host. Public BYOK callers cannot supply arbitrary endpoints.
    """
    provider_ids = {entry.id for entry in build_catalog().providers}
    if provider not in provider_ids:
        return None
    return PROVIDER_BASE_URLS.get(provider)


__all__ = ["build_catalog", "catalog_provider_base_url"]
