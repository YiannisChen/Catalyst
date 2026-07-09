# BYOK Multi-Provider Model Layer — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the single-provider `llm_factory.py` with a multi-provider BYOK model layer: provider YAMLs, unified `model_resolver.py`, credential plane with deployment-mode gating, cost tri-state, and prompt-byte invariance. Retire `_PROVIDER_DEFAULTS` and `SUPPORTED_MODELS` into declarative YAML configs.

**Architecture:** Provider catalog moves from hardcoded Python dicts (`_PROVIDER_DEFAULTS`, `SUPPORTED_MODELS`, `model_catalog.py`) into `packages/app/catalyst_app/providers/*.yaml`. A new `model_resolver.py` is the single entry point for both agent and judge LLM construction: `resolve(provider, model_id, credentials, deployment_mode, role, exclude_family) → (client, ModelCapabilities, pricing, model_meta)`. Credential plane adds `CATALYST_DEPLOYMENT_MODE local|hosted` with hosted rejecting `server_env` (403). ModelCapabilities type lives in agents (`structured_output_policy.py` — S2 Phase 6/A4). Cost is tri-state: `known` (in pricing table), `estimated` (fallback price), `unknown` (BYOK agent path tolerates, judge/eval path RAISES).

**Tech Stack:** Python 3.12+, PyYAML (stdlib-replaceable with json if no-deps rule active), httpx (existing), Pydantic v2, LangChain ChatOpenAI.

**Design Refs (normative):** BYOK Design §A–§I + Amendments 1–4.

**Process rule:** CLAUDE.md for writing-plans: strictly exclude all code blocks. Focus on logic flow, schema definitions, contract interfaces.

---

## Preconditions (stated in plan, verified before execution)

0. **BYOK base commits first.** The uncommitted BYOK base (ModelConfig, CredentialSource, RuntimeCredentialStore, model_catalog.py, provider_validator.py, env_loader.py, per-run key flow in live_runs.py) must be committed as its own change before this layer touches those files.

1. **dependencies.py shared-ownership.** This plan modifies `_graph_factory` in `dependencies.py` (additive — pass `model_resolver` result through). Must coexist with A6 isolation (`CATALYST_ALLOW_SQL_FALLBACK`). No deletion of A6 logic.

2. **S1 baseline pack lands first.** The S1 eval ruler (judge cache at `packages/eval/eval_cache/judge_cache.json`, v1_3 golden sets, three-arm harness) must be committed before this layer — the prompt-byte invariance invariant depends on S1 cache keys surviving intact.

---

## Phase 0: Real-Run Verification Evidence

### 0.1: llm_factory.py current state
- `_PROVIDER_DEFAULTS`: hardcoded dict with 5 entries (openai, aihubmix, deepseek, siliconflow, glm)
- `SUPPORTED_MODELS`: hardcoded list of 8 model IDs
- `DEFAULT_MODEL`: `"gemini-2.5-flash-nothink"`
- `build_llm()`: `ChatOpenAI(model=resolved_model, api_key=key, base_url=url, temperature=0.0, max_retries=2, timeout=90)`

### 0.2: model_catalog.py current state
- 6 providers: aihubmix, siliconflow, openai, deepseek, glm, custom_openai_compatible
- `Custom (OpenAI-compatible)` provider has `executable=True`, `models=[]`, `default_model_id=None`
- aihubmix models: gemini-2.5-flash-nothink, deepseek-v4-flash, claude-opus-4-6, qwen3.6-flash

### 0.3: cost_tracker.py MODEL_PRICING
- 12 entries: claude-sonnet-4-20250514, gpt-4o, claude-haiku-4-5-20251001, gemini-2.5-flash, gemini-2.5-flash-nothink, claude-opus-4-6, deepseek-v4-flash, qwen3.6-flash, qwen-turbo, deepseek-v3, coding-minimax-m2.7-free, qwen3.6-plus-preview-free
- `_ZERO_PRICING` fallback: `{"input": 0.0, "output": 0.0}` — S2 changed this to RAISE for judge path

### 0.4: Credential plane (exists)
- `RuntimeCredentialStore`: thread-safe in-memory dict, keyed by run_id, never persisted
- `CredentialSource`: `SERVER_ENV` | `BROWSER_KEY`
- `live_runs.py:71-113`: per-run registration: extract api_key → register in credential_store AFTER run_id created
- `ModelConfig.api_key` is `""` by default, required only when `credential_source=BROWSER_KEY`

### 0.5: What does NOT exist yet
- `packages/app/catalyst_app/providers/` — directory absent
- `packages/app/catalyst_app/model_resolver.py` — file absent
- `packages/agents/catalyst_agents/structured_output_policy.py` — file absent (S2 Phase 6)
- `CATALYST_DEPLOYMENT_MODE` — env var not referenced in app code
- `ModelCapabilities` type — not defined

### 0.6: Prompt-byte invariance anchor
- S1 judge cache key: `SHA-256(case_id || "::" || arm || "::" || metric || "::" || rubric_version || "::" || prompt)`
- The `prompt` portion is the raw LLM prompt string. Any change to how `build_llm` formats the request (extra_headers, model name normalization, base_url) that alters the prompt bytes would invalidate S1 cache. The INVARIANT is: BYOK must not alter prompt bytes.

---

## Dependencies & CORE-vs-STRONG Boundary

- **Dependencies:** PyYAML (if allowed; otherwise json for YAML-as-JSON fallback). No other new deps.
- **CORE:** Tasks 1–10.
- **STRONG:** Task 11 (OpenRouter gateway), Task 12 (read-only replay demo §G).

---

## Decision Points (flagged for human sign-off)

| ID | Decision | Context |
|---|---|---|
| M1 | Provider YAML format: YAML or JSON? | YAML is more readable for multi-line defaults; JSON needs no new dep. Proposal: YAML with PyYAML; fallback: JSON with stdlib json. |
| M2 | aihubmix preset: byte-for-byte `SUPPORTED_MODELS` or canonical model IDs only? | Current `SUPPORTED_MODELS` has 8 entries including free-tier models (qwen-turbo, coding-minimax, qwen3.6-plus-preview-free). Provider YAML must reproduce exactly. |
| M3 | Judge model pin: which provider/model for eval judge? | S1 judge is configurable via `judge_config.json`. BYOK must not alter the judge path's model resolution. Pin in plan: judge uses `model_resolver.resolve(role="judge")` and excludes the agent's model family. |
| M4 | `anthropic.yaml` ships `enabled: false` — when does it become `true`? | Claude via OpenRouter only for now. Anthropic direct API requires separate API key format. |
| M5 | `moonshot.yaml` ships `enabled: false` — under what conditions enable? | Kimi/Moonshot models require provider verification. |
| Judge pin | Eval judge model family exclusion | See M3. Judge must use a model from a DIFFERENT family than the agent-under-test (cross-family independence). Resolved via `model_resolver.resolve(role="judge", exclude_family=agent_family)`. |

---

## Task 1: Provider YAML Catalog

**Files:**
- Create: `packages/app/catalyst_app/providers/deepseek.yaml`
- Create: `packages/app/catalyst_app/providers/google.yaml`
- Create: `packages/app/catalyst_app/providers/openai.yaml`
- Create: `packages/app/catalyst_app/providers/zhipu.yaml`
- Create: `packages/app/catalyst_app/providers/moonshot.yaml`
- Create: `packages/app/catalyst_app/providers/openrouter.yaml`
- Create: `packages/app/catalyst_app/providers/openai_compatible.yaml`
- Create: `packages/app/catalyst_app/providers/aihubmix.yaml`
- Create: `packages/app/catalyst_app/providers/anthropic.yaml`

**Schema (each YAML file):**
- `provider_id`: str — matches `schemas.CatalogProvider.id`
- `label`: str — display name
- `enabled`: bool — `true` for active, `false` for shipped-but-disabled
- `base_url`: str — default API endpoint
- `env_key`: str — canonical env var name (from `env_loader._PROVIDER_ENV_MAP`)
- `default_model_id`: str | null
- `extra_headers`: dict[str, str] | null — provider-wide headers (e.g. Google `x-goog-api-key`)
- `models`: list[ModelEntry]

**ModelEntry schema:**
- `id`: str — API model ID (e.g. `deepseek-v4-flash`, `gemini-2.5-flash`, `gpt-4o-mini`, `GLM-4.7-FlashX`, `GLM-5.2`)
- `label`: str — display label
- `tier`: `value` | `quality` | `reasoning`
- `recommended`: bool
- `pricing`: {input: float, output: float} — USD per 1M tokens; `null` for unknown
- `capabilities`: ModelCapabilitiesRef — structured output tier, context window, etc.
- `notes`: str | null

**CapabilitiesRef schema:**
- `supports_structured_output`: bool — native JSON schema support
- `supports_json_mode`: bool — system-message JSON mode
- `output_tier`: `NATIVE_SCHEMA` | `JSON_MODE` | `PROMPTED_JSON` — derived from the above two
- `max_output_tokens`: int | null — context window limit

**Provider-specific model IDs (verified against model_catalog.py + SUPPORTED_MODELS):**

| Provider | Model ID | Tier |
|---|---|---|
| deepseek | `deepseek-v4-flash` | value |
| deepseek | `deepseek-chat` | value |
| google | `gemini-2.5-flash` | value |
| openai | `gpt-4o-mini` | value |
| openai | `gpt-4o` | quality |
| zhipu | `GLM-4.7-FlashX` | value |
| zhipu | `GLM-5.2` | quality |
| moonshot | `moonshot-v1-8k` | value |
| openrouter | `openai/gpt-4o-mini` | value |
| openrouter | `anthropic/claude-sonnet-4-20250514` | quality |
| openai_compatible | (user-supplied) | — |
| aihubmix | `gemini-2.5-flash-nothink` | value |
| aihubmix | `deepseek-v4-flash` | value |
| aihubmix | `claude-opus-4-6` | quality |
| aihubmix | `qwen3.6-flash` | value |
| aihubmix | `qwen-turbo` | value (free) |
| aihubmix | `deepseek-v3` | quality |
| aihubmix | `coding-minimax-m2.7-free` | value (free) |
| aihubmix | `qwen3.6-plus-preview-free` | value (free) |
| anthropic | (empty — enabled: false) | — |

**aihubmix preset invariant:** The aihubmix.yaml models list MUST be byte-for-byte identical to the current `SUPPORTED_MODELS` list (all 8 entries). This preserves backward compatibility for existing AIHubMix users.

**moonshot.yaml + anthropic.yaml:** Ship with `enabled: false`. Model entries present but gated. Enabling is a future operator decision (M4, M5).

**TEST INTENT:**
1. Every YAML parses to a valid ProviderConfig dict with required fields
2. aihubmix models list exactly matches `SUPPORTED_MODELS` (set equality + count)
3. Every model with `pricing: null` has `pricing_status: unknown` resolved
4. `openai_compatible.yaml` has `models: []` and `default_model_id: null` (user supplies)
5. `anthropic.yaml` and `moonshot.yaml` have `enabled: false`

---

## Task 2: Provider Catalog Loader

**Files:**
- Create: `packages/app/catalyst_app/provider_catalog.py`

**Contract:**
- `load_providers() -> dict[str, ProviderConfig]`: walks `providers/*.yaml`, parses each, returns dict keyed by `provider_id`. Skips `enabled: false` files. Validates schema on load.
- `get_provider(provider_id: str) -> ProviderConfig`: lookup with clear error on unknown ID.
- `list_enabled_providers() -> list[str]`: returns enabled provider IDs for catalog endpoints.

**ProviderConfig (Pydantic):**
- `provider_id: str`
- `label: str`
- `base_url: str`
- `env_key: str`
- `default_model_id: str | None`
- `extra_headers: dict[str, str] | None`
- `models: list[ModelEntry]`

**ModelEntry (Pydantic):**
- `id: str`
- `label: str`
- `tier: Literal["value", "quality", "reasoning"]`
- `recommended: bool = False`
- `pricing: dict | None` — `{input: float, output: float}` or null
- `capabilities: ModelCapabilitiesRef`
- `notes: str | None`

**ModelCapabilitiesRef (Pydantic):**
- `supports_structured_output: bool = False`
- `supports_json_mode: bool = False`
- `output_tier: Literal["NATIVE_SCHEMA", "JSON_MODE", "PROMPTED_JSON"]` — derived field
- `max_output_tokens: int | None = None`

**TEST INTENT:**
1. `load_providers()` returns 7 enabled entries (deepseek, google, openai, zhipu, openrouter, openai_compatible, aihubmix) — anthropic + moonshot excluded
2. Unknown provider ID raises `ProviderNotFoundError`
3. Malformed YAML raises `ProviderConfigError` with filename
4. Round-trip: parsed ProviderConfig.model_dump() re-parses to identical config

---

## Task 3: Retire `model_catalog.py` — Derive from Provider YAMLs

**Files:**
- Modify: `packages/app/catalyst_app/model_catalog.py`

**Change:** `build_catalog()` reads from `provider_catalog.load_providers()` instead of hardcoded `CatalogProvider` lists. The output `ModelCatalogResponse` schema is unchanged.

**Mapping:** Each `ProviderConfig` → `CatalogProvider`:
- `id` ← `provider_id`
- `label` ← `label`
- `executable` ← `True` (always, for enabled providers)
- `env_key_configured` ← `is_env_key_configured(provider_id)` (existing function)
- `default_model_id` ← `default_model_id`
- `models` ← each `ModelEntry` → `CatalogModel(id, label, tier, recommended, notes)`

**TEST INTENT:**
1. `build_catalog()` returns same `ModelCatalogResponse` schema as current
2. Provider count equals `len(load_providers())`
3. aihubmix models match current `model_catalog.py` aihubmix entries
4. `custom_openai_compatible` still present with `executable=True, models=[]`
5. env_key_configured reflects actual env state (mockable)

---

## Task 4: Model Capabilities Type (S2 Phase 6 / A4 — do ONCE)

**Files:**
- Create: `packages/agents/catalyst_agents/structured_output_policy.py`

**IMPORTANT:** This is S2 Phase 6 (Amendment 4). It is implemented here because the BYOK layer needs the `ModelCapabilities` type for `model_resolver.py`. Do NOT implement the full S2 structured-output pipeline — only the TYPE DEFINITION and the `select_output_tier` function. The actual plumbing through `build_attribution_graph` is S2's responsibility.

**Contract:**
- `class OutputTier(str, Enum)`: `NATIVE_SCHEMA`, `JSON_MODE`, `PROMPTED_JSON`
- `class ModelCapabilities(BaseModel)`: `output_tier: OutputTier`, `supports_structured_output: bool`, `supports_json_mode: bool`, `max_output_tokens: int | None`
- `def select_output_tier(supports_structured_output: bool, supports_json_mode: bool) -> OutputTier`: decision function — NATIVE_SCHEMA if structured output supported, else JSON_MODE if json mode supported, else PROMPTED_JSON
- Default (None or unknown model) → `PROMPTED_JSON` = no behavior change for existing models

**Why here and not in S2:** The `model_resolver.py` (Task 5) returns `ModelCapabilities` alongside the client. `dependencies.py` (Task 8) imports the type from agents to pass through `build_attribution_graph`. The type lives in agents because it's an agent concern — the app only imports and passes it.

**TEST INTENT:**
1. `select_output_tier(True, True) → NATIVE_SCHEMA`
2. `select_output_tier(False, True) → JSON_MODE`
3. `select_output_tier(False, False) → PROMPTED_JSON`
4. `ModelCapabilities(output_tier=PROMPTED_JSON)` is the default/fallback
5. Round-trip: `ModelCapabilities.model_validate(cap.model_dump())` is identity

---

## Task 5: Unified Model Resolver

**Files:**
- Create: `packages/app/catalyst_app/model_resolver.py`

**Contract — single entry point:**

Function signature: resolve(provider: str, model_id: str, credentials: CredentialContext, deployment_mode: "local" | "hosted", role: "agent" | "judge", exclude_family: str | None = None) → ResolvedModel

**CredentialContext:**
- `source: CredentialSource` (SERVER_ENV | BROWSER_KEY)
- `api_key: str | None` — from `RuntimeCredentialStore` or env
- `env_key_name: str` — canonical env var name for the provider

**ResolvedModel:**
- `client: ChatOpenAI` — the configured LangChain client
- `capabilities: ModelCapabilities` — from `structured_output_policy.py`
- `pricing: PricingInfo` — tri-state
- `meta: ModelMeta` — provider + model_id + base_url (for logging/audit)

**PricingInfo (tri-state):**
- `status: Literal["known", "estimated", "unknown"]`
- `input_price: float | None` — per 1M tokens
- `output_price: float | None`
- Resolution: look up model in provider YAML `pricing` field → `known`; if missing but provider has a default pricing fallback → `estimated`; otherwise → `unknown`

**ModelMeta:**
- `provider: str`
- `model_id: str`
- `base_url: str`
- `resolved_at: str` — ISO-8601
- `family: str` — derived from provider (e.g. "deepseek", "google", "openai")

**Resolution logic:**
1. Load provider config via `provider_catalog.get_provider(provider)`
2. Validate `model_id` exists in provider's model list (or `openai_compatible` allows any)
3. Resolve credentials: if `deployment_mode == "hosted"` and `credentials.source == SERVER_ENV` → raise `HostedCredentialError` (403)
4. If `role == "judge"` and pricing is `unknown` → raise `UnknownModelPricingError` (S2 invariant)
5. If `exclude_family` is set and `provider` matches → raise `FamilyExcludedError`
6. Construct `ChatOpenAI` client with `base_url`, `api_key`, `extra_headers` (merged from provider YAML)
7. Return `ResolvedModel`

**Existing `build_llm` function:** Retained as a thin wrapper that calls `resolve()` for backward compatibility. The `_PROVIDER_DEFAULTS` dict and `SUPPORTED_MODELS` list are DELETED — they are superseded by the YAML catalog.

**TEST INTENT:**
1. `resolve("deepseek", "deepseek-v4-flash", ...)` returns client with base_url `https://api.deepseek.com/v1`
2. `resolve("openai_compatible", "any-model", ..., base_url="http://localhost:8080/v1")` accepts any model_id
3. Unknown provider raises `ProviderNotFoundError`
4. Unknown model_id on non-openai_compatible provider raises `ModelNotFoundError`
5. `deployment_mode="hosted" + credential_source=SERVER_ENV` raises `HostedCredentialError`
6. `role="judge" + pricing=unknown` raises `UnknownModelPricingError`
7. `exclude_family="google"` rejects `provider="google"` with `FamilyExcludedError`
8. `extra_headers` from provider YAML merged into ChatOpenAI `default_headers`
9. Free-tier model (pricing 0.0/0.0) returns `pricing.status="known"` with `input_price=0.0`

---

## Task 6: Credential Plane — Deployment Mode

**Files:**
- Modify: `packages/app/catalyst_app/dependencies.py` (add `_deployment_mode_from_env`)
- Modify: `packages/app/catalyst_app/routers/live_runs.py` (add deployment-mode gate at route entry)

**Contract:**
- `CATALYST_DEPLOYMENT_MODE`: env var, values `local` | `hosted`. Default: `local`.
- `_deployment_mode_from_env() -> Literal["local", "hosted"]`: reads env, validates, returns.
- In `live_runs.py` create_run + retry_run: if `deployment_mode == "hosted"` and `request.model.credential_source == CredentialSource.SERVER_ENV` → return `403 Forbidden` with `FailurePayload(code="hosted_rejects_server_env")`. The operator's server keys must NEVER serve visitor traffic.
- Browser-held keys: continue through existing `credential_store.register(run_id, api_key=runtime_key)` flow (unchanged).
- Symmetric with A6: same deployment-mode gate applies to A6's `CATALYST_ALLOW_SQL_FALLBACK` if that flag is server-env-controlled.

**TEST INTENT:**
1. `deployment_mode="hosted" + credential_source=SERVER_ENV` → 403 with `hosted_rejects_server_env`
2. `deployment_mode="hosted" + credential_source=BROWSER_KEY` → proceeds normally
3. `deployment_mode="local" + credential_source=SERVER_ENV` → proceeds (operator use)
4. Env var absent → defaults to `local`
5. Invalid env var value → raises on startup

---

## Task 7: Retire `_PROVIDER_DEFAULTS` and `SUPPORTED_MODELS`

**Files:**
- Modify: `packages/app/catalyst_app/llm_factory.py`

**Change:** Delete `_PROVIDER_DEFAULTS` dict, `SUPPORTED_MODELS` list. `build_llm()` becomes a thin wrapper:

`build_llm(model_id, *, provider, api_key, base_url) → ChatOpenAI`:
1. Calls `model_resolver.resolve(provider, model_id, credentials, deployment_mode, role="agent")`
2. Returns `resolved.client`

The legacy env-only path (`AIHUBMIX_API_KEY` fallback) is preserved via the aihubmix provider YAML's `env_key` field — `model_resolver` reads the env key from the provider config, not from hardcoded `_get_api_key()`.

**Backward compatibility:** All existing callers of `build_llm` must continue to work:
- `dependencies.py:_graph_factory` calls `build_llm(model_id, provider, api_key, base_url)` — unchanged signature
- `provider_validator.py:validate_provider` calls `build_llm` for validation — unchanged

**TEST INTENT:**
1. `_PROVIDER_DEFAULTS` is no longer importable from `llm_factory`
2. `SUPPORTED_MODELS` is no longer importable from `llm_factory`
3. `build_llm(model_id="deepseek-v4-flash", provider="deepseek", api_key="sk-test")` returns ChatOpenAI with correct base_url
4. Legacy `build_llm("gemini-2.5-flash-nothink")` (no provider) resolves via `DEFAULT_MODEL` + aihubmix provider
5. All existing app tests that use `build_llm` continue to pass

---

## Task 8: Wire Model Resolver into Dependency Graph

**Files:**
- Modify: `packages/app/catalyst_app/dependencies.py`

**Change in `_graph_factory`:**

Current flow: build_llm(model_id, provider, api_key, base_url) → ChatOpenAI → build_attribution_graph(llm=llm, ...)

New flow: model_resolver.resolve(provider, model_id, credentials, deployment_mode, role="agent") → ResolvedModel(client, capabilities, pricing, meta) → build_attribution_graph(llm=client, llm_capabilities=capabilities, ...)

**Additive only — must coexist with A6:**
- `_graph_factory` already accepts `model: dict | str | None` and `api_key: str | None`
- Add `model_resolver.resolve()` call in the `isinstance(model, dict)` branch
- Pass `capabilities` to `build_attribution_graph(llm_capabilities=...)` using the existing `functools.partial` pattern for nodes that need structured output (judge)
- A6's `CATALYST_ALLOW_SQL_FALLBACK` path is unchanged

**No prompt-byte change:**
- The `llm` client passed to `build_attribution_graph` produces the SAME prompt bytes as before (same model, same base_url, same ChatOpenAI parameters)
- Extra headers (from provider YAML) are HTTP-layer only — they do NOT alter the prompt string
- The S1 judge cache key (`rubric_version || prompt`) is unaffected

**TEST INTENT:**
1. `_graph_factory(model={"provider": "deepseek", "model_id": "deepseek-v4-flash"}, api_key="sk-test")` builds a graph successfully
2. `llm_capabilities` is passed to `build_attribution_graph` when using BYOK path
3. A6 `CATALYST_ALLOW_SQL_FALLBACK` path still works
4. Legacy `model_id` string path still works (backward compat)

---

## Task 9: Cost Tri-State — Track Cost With Status

**Files:**
- Modify: `packages/agents/catalyst_agents/cost_tracker.py`

**Contract — cost tri-state per §E:**

Add `cost_status` field to each cost_breakdown entry:
- `known`: model is in MODEL_PRICING (or provider YAML pricing)
- `estimated`: model has a fallback/default pricing (same provider family)
- `unknown`: model has no pricing data at all

**Behavior change from current `_ZERO_PRICING` fallback:**

| Path | Model Pricing | Action |
|---|---|---|
| Agent (BYOK) | unknown | Record tokens normally, set `cost_status="unknown"`, `cost_usd=0.0`. Do NOT raise. |
| Judge/Eval | unknown | Raise `UnknownModelPricingError` at graph-build (S2 invariant — fail-fast). |

The `MODEL_PRICING` dict is MOVED into provider YAMLs. `track_cost()` reads pricing from a new module-level `_pricing_lookup` dict populated at startup from `provider_catalog`. Models with `pricing: null` in YAML get `cost_status: unknown`.

**How this preserves S2:** S2 requires unknown pricing to RAISE in the judge/eval path. The BYOK agent path must tolerate unknown pricing (visitor brings their own model, we don't know its cost). The distinction is made at `model_resolver.resolve(role=...)` — the judge path rejects unknown pricing, the agent path accepts it.

**TEST INTENT:**
1. `track_cost(state, "judge", response)` with known model → `cost_status="known"`, `cost_usd > 0`
2. `track_cost(state, "critic", response)` with unknown model → `cost_status="unknown"`, `cost_usd=0.0`, no raise
3. `model_resolver.resolve(role="judge", ...)` with unknown pricing → raises `UnknownModelPricingError`
4. `model_resolver.resolve(role="agent", ...)` with unknown pricing → returns `pricing.status="unknown"`, no raise
5. Free-tier model (0.0/0.0 in YAML) → `cost_status="known"`, `cost_usd=0.0`

---

## Task 10: Prompt-Byte Invariance Verification

**Files:**
- Test: `packages/app/tests/test_byok_prompt_invariance.py`

**Contract:** The BYOK layer must not alter the prompt bytes sent to the LLM. This is verified by:
1. Run `build_llm(legacy_path)` with the current aihubmix default → capture the prompt string
2. Run `model_resolver.resolve("aihubmix", "gemini-2.5-flash-nothink", ...)` → capture the prompt string via the same code path
3. Assert byte-identical prompts

The verification covers:
- `ChatOpenAI` is constructed with identical `model`, `temperature`, `max_retries`, `timeout` parameters
- Extra headers from provider YAML do NOT appear in the prompt (HTTP layer only)
- Model name normalization does NOT alter the `model` field passed to ChatOpenAI

**TEST INTENT:**
1. Legacy `build_llm("gemini-2.5-flash-nothink")` and resolved `build_llm(provider="aihubmix", model_id="gemini-2.5-flash-nothink")` produce ChatOpenAI with identical `.model` attribute
2. Extra headers are present in `ChatOpenAI.default_headers` but NOT in `.model` string
3. S1 judge cache test passes with BYOK-resolved judge LLM (same cache hit for identical prompt)

---

## Task 11 (STRONG): OpenRouter Gateway

**Files:**
- Modify: `packages/app/catalyst_app/providers/openrouter.yaml`

**Contract:** OpenRouter is a gateway provider — it proxies to other model families. The `openrouter.yaml` model entries use OpenRouter's model path convention (`openai/gpt-4o-mini`, `anthropic/claude-sonnet-4-20250514`).

- `base_url`: `https://openrouter.ai/api/v1`
- `extra_headers`: `{"HTTP-Referer": "...", "X-Title": "Catalyst"}` (OpenRouter requires these)
- `model_id` format: `{provider}/{model}` — OpenRouter convention
- `pricing`: use OpenRouter's published rates

**Family exclusion for OpenRouter:** When `exclude_family="openai"`, models with prefix `openai/` are excluded. The `ModelMeta.family` for OpenRouter models is derived from the prefix.

**TEST INTENT:**
1. `resolve("openrouter", "openai/gpt-4o-mini", ...)` returns client with base_url pointing to OpenRouter
2. Extra headers include `HTTP-Referer` and `X-Title`
3. `exclude_family="openai"` rejects `openai/gpt-4o-mini` but accepts `anthropic/claude-sonnet-4-20250514`

---

## Task 12 (STRONG): Read-Only Replay Demo (§G)

**Files:**
- Create: `packages/app/catalyst_app/routers/replay.py` (or extend existing)

**Contract:** Visitors without API keys can replay pre-run traces. This is additive — does not modify existing routes.

- New endpoint: `GET /live-runs/{run_id}/replay` — returns the workspace projection for a completed run
- No API key required (read-only, no LLM calls)
- Reuses existing `workspace_projection.py` (which already joins artifacts into `WorkspaceResponse`)
- Reuses existing trace artifacts (stored in `.local/` or configured path)
- No-key visitors see the full workspace in read-only mode

**TEST INTENT:**
1. Replay of completed run returns `WorkspaceResponse` with all stages and evidence
2. Replay does not call any LLM (no API key needed)
3. Replay of non-existent run returns 404
4. Replay of running/incomplete run returns appropriate status

---

## Post-Implementation Verification

1. `pytest packages/app/tests/ packages/eval/tests/ packages/agents/tests/ -q` — all pass
2. `_PROVIDER_DEFAULTS` and `SUPPORTED_MODELS` deleted from `llm_factory.py`
3. All 9 provider YAMLs parse successfully via `provider_catalog.load_providers()`
4. `model_catalog.build_catalog()` returns same schema as pre-BKOK
5. Legacy `build_llm("gemini-2.5-flash-nothink")` still works
6. BYOK `build_llm(provider="deepseek", model_id="deepseek-v4-flash", api_key="sk-...")` works
7. `CATALYST_DEPLOYMENT_MODE=hosted` rejects `server_env` credentials (403)
8. Prompt-byte invariance: same prompt bytes for legacy vs resolved paths
9. S1 judge cache keys survive (no rubric_version or prompt change)
10. No API keys in logs, responses, or persisted artifacts (verify with `test_no_secret_leak.py`)
