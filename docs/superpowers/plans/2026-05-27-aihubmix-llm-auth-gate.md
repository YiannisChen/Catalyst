# Aihubmix LLM Integration + Auth Gate Implementation Plan

> **For agentic workers:** Use executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire a real LLM client (via aihubmix OpenAI-compatible proxy) into the attribution graph so Critic/Judge/Validator nodes can execute, add a frontend password gate to control access, and add a model selector dropdown.

**Architecture:** The graph factory in `dependencies.py` currently passes `llm=None`, crashing any node that calls `llm.invoke()`. We introduce a per-run LLM factory that creates a `ChatOpenAI` instance configured for aihubmix, keyed by `model_id`. The graph factory changes from a cached nullary function to accepting a `model_id` parameter. The frontend gets a password wall (sessionStorage-based, no backend auth) and a model selector dropdown that sends `model_id` in `CreateRunRequest`.

**Tech Stack:** Python (FastAPI, LangChain `ChatOpenAI`), TypeScript/React (Vite), aihubmix OpenAI-compatible API

---

## File Structure

| Action | Path | Responsibility |
|--------|------|---------------|
| **Create** | `packages/app/catalyst_app/llm_factory.py` | Build `ChatOpenAI` instances for aihubmix; model registry; env key loading |
| **Modify** | `packages/app/catalyst_app/dependencies.py` | Replace `llm=None` with per-run LLM construction via `llm_factory` |
| **Modify** | `packages/agents/catalyst_agents/runtime/runner.py` | Pass `model_id` from run state into `graph_factory()` |
| **Modify** | `packages/agents/catalyst_agents/runtime/service.py` | Thread `model_id` through `graph_factory` call signature |
| **Create** | `apps/workbench/src/components/auth/PasswordGate.tsx` | Full-screen password wall component |
| **Modify** | `apps/workbench/src/App.tsx` | Wrap app in `PasswordGate`, add model selector state, pass `model_id` to `createLiveRun` |
| **Modify** | `apps/workbench/src/components/context/ContextBar.tsx` | Add model selector `<select>` to the toolbar |
| **Modify** | `apps/workbench/src/styles.css` | Styles for password gate and model selector |
| **Create** | `scripts/verify_aihubmix_models.py` | Standalone script to test all 8 models against aihubmix API |
| **Create** | `packages/app/catalyst_app/llm_factory_test.py` | Unit tests for LLM factory |

---

### Task 1: Create the LLM Factory Module

**Files:**
- Create: `packages/app/catalyst_app/llm_factory.py`
- Test: `packages/app/catalyst_app/llm_factory_test.py`

- [ ] **Step 1: Write the failing test for `build_llm`**

Create `packages/app/catalyst_app/llm_factory_test.py`:

```python
import os
import pytest
from unittest.mock import patch


def test_build_llm_returns_chat_openai_instance():
    """build_llm returns a ChatOpenAI configured for aihubmix."""
    with patch.dict(os.environ, {"AIHUBMIX_API_KEY": "sk-test-key-123"}):
        from catalyst_app.llm_factory import build_llm
        llm = build_llm("gemini-2.5-flash-nothink")
        assert llm.model_name == "gemini-2.5-flash-nothink"
        assert "aihubmix.com" in str(llm.openai_api_base)


def test_build_llm_raises_without_api_key():
    """build_llm raises ValueError when AIHUBMIX_API_KEY is missing."""
    with patch.dict(os.environ, {}, clear=True):
        os.environ.pop("AIHUBMIX_API_KEY", None)
        from catalyst_app.llm_factory import build_llm
        with pytest.raises(ValueError, match="AIHUBMIX_API_KEY"):
            build_llm("gemini-2.5-flash-nothink")


def test_supported_models_returns_list():
    """SUPPORTED_MODELS contains the 8 expected model identifiers."""
    from catalyst_app.llm_factory import SUPPORTED_MODELS
    assert len(SUPPORTED_MODELS) == 8
    assert "gemini-2.5-flash-nothink" in SUPPORTED_MODELS
    assert "claude-opus-4-6" in SUPPORTED_MODELS


def test_default_model_is_gemini_flash():
    """DEFAULT_MODEL is gemini-2.5-flash-nothink for cost efficiency."""
    from catalyst_app.llm_factory import DEFAULT_MODEL
    assert DEFAULT_MODEL == "gemini-2.5-flash-nothink"


def test_build_llm_rejects_unknown_model():
    """build_llm raises ValueError for model IDs not in SUPPORTED_MODELS."""
    with patch.dict(os.environ, {"AIHUBMIX_API_KEY": "sk-test-key-123"}):
        from catalyst_app.llm_factory import build_llm
        with pytest.raises(ValueError, match="not supported"):
            build_llm("gpt-4-turbo-not-real")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd packages/app && python -m pytest catalyst_app/llm_factory_test.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'catalyst_app.llm_factory'`

- [ ] **Step 3: Implement the LLM factory**

Create `packages/app/catalyst_app/llm_factory.py`:

```python
"""LLM client factory for aihubmix OpenAI-compatible proxy.

Provides build_llm() which creates a LangChain ChatOpenAI instance configured
to use the aihubmix relay at https://aihubmix.com/v1.

The API key is read from the AIHUBMIX_API_KEY environment variable.
"""
from __future__ import annotations

import os

from langchain_openai import ChatOpenAI


AIHUBMIX_BASE_URL = "https://aihubmix.com/v1"

SUPPORTED_MODELS: list[str] = [
    "gemini-2.5-flash-nothink",
    "claude-opus-4-6",
    "deepseek-v4-flash",
    "qwen3.6-flash",
    "qwen-turbo",
    "deepseek-v3",
    "coding-minimax-m2.7-free",
    "qwen3.6-plus-preview-free",
]

DEFAULT_MODEL: str = "gemini-2.5-flash-nothink"


def _get_api_key() -> str:
    key = os.environ.get("AIHUBMIX_API_KEY", "").strip()
    if not key:
        raise ValueError(
            "AIHUBMIX_API_KEY environment variable is required but not set. "
            "Set it in your .env or shell environment."
        )
    return key


def build_llm(model_id: str | None = None) -> ChatOpenAI:
    """Create a ChatOpenAI instance targeting the aihubmix proxy.

    Args:
        model_id: One of SUPPORTED_MODELS. Defaults to DEFAULT_MODEL
                  when None or "runtime-default".

    Returns:
        A configured ChatOpenAI with the aihubmix base URL and API key.

    Raises:
        ValueError: If the model_id is not in SUPPORTED_MODELS or
                    AIHUBMIX_API_KEY is not set.
    """
    resolved_model = model_id if model_id and model_id != "runtime-default" else DEFAULT_MODEL

    if resolved_model not in SUPPORTED_MODELS:
        raise ValueError(
            f"Model '{resolved_model}' is not supported. "
            f"Choose from: {', '.join(SUPPORTED_MODELS)}"
        )

    api_key = _get_api_key()

    return ChatOpenAI(
        model=resolved_model,
        api_key=api_key,
        base_url=AIHUBMIX_BASE_URL,
        temperature=0.0,
        max_retries=2,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd packages/app && python -m pytest catalyst_app/llm_factory_test.py -v`
Expected: 5 passed

- [ ] **Step 5: Stage changes**

```bash
git add packages/app/catalyst_app/llm_factory.py packages/app/catalyst_app/llm_factory_test.py
```

---

### Task 2: Rewire `dependencies.py` — Per-Run LLM Injection

**Files:**
- Modify: `packages/app/catalyst_app/dependencies.py`

The key problem: `_graph_factory()` is cached with `@lru_cache` and always passes `llm=None`. The graph must be rebuilt per model because `build_attribution_graph` binds `llm` via `functools.partial` into the node closures at build time.

The fix: change `_graph_factory` from a nullary cached function to a function accepting `model_id`. Remove the `@lru_cache` (graph builds are cheap — the expensive deps like LanceDB table and embedding model are already cached in `RuntimeDependencyLoader`).

- [ ] **Step 1: Read current `dependencies.py` and understand the call chain**

The call chain is:
1. `get_live_run_service()` returns `LiveRunService(graph_factory=_graph_factory)`
2. `LiveRunService.run_one()` → `LiveRunRunner.run()` → `self.graph_factory()` → `_graph_factory()`
3. `_graph_factory()` calls `build_attribution_graph(llm=None)`

We need `graph_factory` to accept `model_id` so the runner can pass the per-run model.

- [ ] **Step 2: Modify `dependencies.py` to use `build_llm`**

Replace the `_graph_factory` function and update imports in `packages/app/catalyst_app/dependencies.py`:

```python
from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path

from catalyst_agents.graph import build_attribution_graph
from catalyst_agents.runtime.dependencies import RuntimeDependencyLoader
from catalyst_agents.runtime.service import LiveRunService
from catalyst_app.llm_factory import build_llm
from catalyst_app.workbench_store import WorkbenchStore


def _db_path_from_env() -> Path:
    raw = os.getenv("CATALYST_DB_PATH")
    if raw:
        return Path(raw)
    return Path(".local/live_runtime.db")


def _default_model_from_env() -> str:
    return os.getenv("CATALYST_DEFAULT_MODEL", "model-default")


@lru_cache(maxsize=1)
def get_runtime_dependency_loader() -> RuntimeDependencyLoader:
    return RuntimeDependencyLoader(
        sqlite_db_path=_db_path_from_env(),
        default_model=_default_model_from_env(),
    )


def _graph_factory(model_id: str | None = None):
    """Build a fresh attribution graph with a real LLM client.

    Called once per run — the model varies per user request so the graph
    cannot be globally cached.  Heavy deps (LanceDB, embedding model,
    reranker) are cached inside RuntimeDependencyLoader.
    """
    deps = get_runtime_dependency_loader().get_dependencies()
    llm = build_llm(model_id)
    return build_attribution_graph(
        use_critic=True,
        table=deps.lancedb_table,
        embedding_fn=deps.embedding_fn,
        reranker=deps.reranker,
        llm=llm,
    )


@lru_cache(maxsize=1)
def get_live_run_service() -> LiveRunService:
    return LiveRunService(
        db_path=_db_path_from_env(),
        graph_factory=_graph_factory,
    )


@lru_cache(maxsize=1)
def get_workbench_store() -> WorkbenchStore:
    return WorkbenchStore(db_path=_db_path_from_env())
```

- [ ] **Step 3: Verify the module imports cleanly**

Run: `cd packages/app && python -c "from catalyst_app.dependencies import _graph_factory; print('OK')"`
Expected: `OK` (no import errors)

- [ ] **Step 4: Stage changes**

```bash
git add packages/app/catalyst_app/dependencies.py
```

---

### Task 3: Thread `model_id` Through Runner and Service

**Files:**
- Modify: `packages/agents/catalyst_agents/runtime/runner.py`
- Modify: `packages/agents/catalyst_agents/runtime/service.py`

The runner currently calls `self.graph_factory()` with no arguments. It needs to pass `model_id` from the run state so that `_graph_factory(model_id)` creates the correct LLM.

- [ ] **Step 1: Modify `LiveRunRunner._invoke_graph` to pass `model_id`**

In `packages/agents/catalyst_agents/runtime/runner.py`, change line 110 from:

```python
graph = self.graph_factory()
```

to:

```python
graph = self.graph_factory(model_id=state.get("model_id"))
```

- [ ] **Step 2: Update the `graph_factory` type hint in `runner.py`**

In `LiveRunRunner.__init__`, change the type hint from:

```python
graph_factory: Callable[[], Any],
```

to:

```python
graph_factory: Callable[..., Any],
```

- [ ] **Step 3: Update the `graph_factory` type hint in `service.py`**

In `LiveRunService.__init__` (line 48 of `service.py`), change:

```python
graph_factory: Callable[[], Any],
```

to:

```python
graph_factory: Callable[..., Any],
```

- [ ] **Step 4: Verify the change compiles**

Run: `cd packages/agents && python -c "from catalyst_agents.runtime.runner import LiveRunRunner; print('OK')"`
Expected: `OK`

- [ ] **Step 5: Stage changes**

```bash
git add packages/agents/catalyst_agents/runtime/runner.py packages/agents/catalyst_agents/runtime/service.py
```

---

### Task 4: Add a `/api/models` Endpoint

**Files:**
- Modify: `packages/app/catalyst_app/routers/live_runs.py`
- Modify: `packages/app/catalyst_app/schemas.py`

The frontend needs to know which models are available. Add a lightweight GET endpoint.

- [ ] **Step 1: Add `ModelsResponse` schema**

In `packages/app/catalyst_app/schemas.py`, append at the bottom (before the last blank line):

```python
class ModelOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str
    label: str
    is_default: bool = False


class ModelsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    models: list[ModelOption]
    default_model_id: str
```

- [ ] **Step 2: Add the endpoint in `live_runs.py`**

In `packages/app/catalyst_app/routers/live_runs.py`, add these imports at the top (extend the existing import from schemas):

Add `ModelOption, ModelsResponse` to the import line from `catalyst_app.schemas`.

Add this import at the top level:

```python
from catalyst_app.llm_factory import SUPPORTED_MODELS, DEFAULT_MODEL
```

Add this endpoint after the `runtime_health` endpoint:

```python
@router.get("/models", response_model=ModelsResponse)
def list_models() -> ModelsResponse:
    options = [
        ModelOption(
            model_id=model_id,
            label=model_id,
            is_default=(model_id == DEFAULT_MODEL),
        )
        for model_id in SUPPORTED_MODELS
    ]
    return ModelsResponse(models=options, default_model_id=DEFAULT_MODEL)
```

- [ ] **Step 3: Verify the endpoint serves**

Run: `cd packages/app && python -c "from catalyst_app.routers.live_runs import router; print('Endpoint registered OK')"`
Expected: `Endpoint registered OK`

- [ ] **Step 4: Stage changes**

```bash
git add packages/app/catalyst_app/routers/live_runs.py packages/app/catalyst_app/schemas.py
```

---

### Task 5: Frontend — Password Gate Component

**Files:**
- Create: `apps/workbench/src/components/auth/PasswordGate.tsx`
- Modify: `apps/workbench/src/styles.css`

- [ ] **Step 1: Create the password gate component**

Create `apps/workbench/src/components/auth/PasswordGate.tsx`:

```tsx
import { type FormEvent, type ReactNode, useState } from 'react';

const SESSION_KEY = 'catalyst_auth';
const VALID_PASSWORD = 'catalyst2026';

function isAuthenticated(): boolean {
  return sessionStorage.getItem(SESSION_KEY) === 'true';
}

interface PasswordGateProps {
  children: ReactNode;
}

export function PasswordGate({ children }: PasswordGateProps) {
  const [authenticated, setAuthenticated] = useState(isAuthenticated);
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');

  if (authenticated) {
    return <>{children}</>;
  }

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (password === VALID_PASSWORD) {
      sessionStorage.setItem(SESSION_KEY, 'true');
      setAuthenticated(true);
      setError('');
    } else {
      setError('Incorrect password.');
      setPassword('');
    }
  }

  return (
    <div className="password-gate">
      <form className="password-gate-form" onSubmit={handleSubmit}>
        <h1 className="password-gate-title">Catalyst</h1>
        <p className="password-gate-subtitle">Attribution Workbench</p>

        <label htmlFor="gate-password" className="password-gate-label">
          Enter password to continue
        </label>
        <input
          id="gate-password"
          type="password"
          className="password-gate-input"
          value={password}
          placeholder="Password"
          autoFocus
          onChange={(e) => {
            setPassword(e.target.value);
            setError('');
          }}
        />

        {error && <p className="password-gate-error">{error}</p>}

        <button type="submit" className="password-gate-submit" disabled={password.length === 0}>
          Enter
        </button>
      </form>
    </div>
  );
}
```

- [ ] **Step 2: Add password gate styles**

Append the following to the end of `apps/workbench/src/styles.css`:

```css
/* Password Gate */
.password-gate {
  position: fixed;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--bg-app);
  z-index: 1000;
}

.password-gate-form {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-4);
  padding: var(--space-8);
  background: var(--bg-panel);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-panel);
  box-shadow: var(--elevation-2);
  min-width: 320px;
  max-width: 400px;
}

.password-gate-title {
  font-family: var(--font-ui);
  font-size: 28px;
  font-weight: 700;
  color: var(--text-primary);
  margin: 0;
  letter-spacing: -0.02em;
}

.password-gate-subtitle {
  font-size: 14px;
  color: var(--text-muted);
  margin: 0 0 var(--space-4) 0;
}

.password-gate-label {
  font-size: 13px;
  color: var(--text-secondary);
  align-self: flex-start;
}

.password-gate-input {
  width: 100%;
  padding: var(--space-3) var(--space-4);
  background: var(--bg-subtle);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-chip);
  color: var(--text-primary);
  font-family: var(--font-mono);
  font-size: 14px;
  outline: none;
  transition: border-color 0.15s;
}

.password-gate-input:focus {
  border-color: var(--primary);
  box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.25);
}

.password-gate-error {
  color: var(--status-request-failed);
  font-size: 13px;
  margin: 0;
}

.password-gate-submit {
  width: 100%;
  padding: var(--space-3) var(--space-4);
  background: var(--primary);
  color: var(--text-primary);
  border: none;
  border-radius: var(--radius-chip);
  font-family: var(--font-ui);
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  transition: background-color 0.15s;
}

.password-gate-submit:hover:not(:disabled) {
  background: var(--primary-hover);
}

.password-gate-submit:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
```

- [ ] **Step 3: Stage changes**

```bash
git add apps/workbench/src/components/auth/PasswordGate.tsx apps/workbench/src/styles.css
```

---

### Task 6: Frontend — Model Selector in ContextBar

**Files:**
- Modify: `apps/workbench/src/api/types.ts`
- Modify: `apps/workbench/src/api/client.ts`
- Modify: `apps/workbench/src/components/context/ContextBar.tsx`
- Modify: `apps/workbench/src/App.tsx`

- [ ] **Step 1: Add model types and API client function**

In `apps/workbench/src/api/types.ts`, append at the bottom:

```typescript
export interface ModelOption {
  model_id: string
  label: string
  is_default: boolean
}

export interface ModelsResponse {
  models: ModelOption[]
  default_model_id: string
}
```

In `apps/workbench/src/api/client.ts`, add `ModelsResponse` to the import from `./types`, then add:

```typescript
export function getModels(): Promise<ModelsResponse> {
  return request<ModelsResponse>('/models')
}
```

- [ ] **Step 2: Add model selector to ContextBar**

Modify `apps/workbench/src/components/context/ContextBar.tsx`:

Add `modelId` and `onModelChange` to the props interface:

```tsx
interface ContextBarProps {
  bootstrap: BootstrapState;
  ticker: string;
  tradeDate: string;
  query: string;
  modelId: string;
  onTickerChange: (ticker: string) => void;
  onTradeDateChange: (tradeDate: string) => void;
  onQueryChange: (query: string) => void;
  onModelChange: (modelId: string) => void;
}
```

Add `modelId` and `onModelChange` to the destructured params.

Import `ModelOption` from the types:

```tsx
import type { ModelOption } from '../../api/types';
```

Add a `models` prop or hard-code the list. For simplicity, hard-code the model list in the component (the `/api/models` endpoint is a nice-to-have but unnecessary for initial delivery):

Add this constant inside the file before the component:

```tsx
const MODEL_OPTIONS: ModelOption[] = [
  { model_id: 'gemini-2.5-flash-nothink', label: 'Gemini 2.5 Flash', is_default: true },
  { model_id: 'claude-opus-4-6', label: 'Claude Opus 4', is_default: false },
  { model_id: 'deepseek-v4-flash', label: 'DeepSeek V4 Flash', is_default: false },
  { model_id: 'qwen3.6-flash', label: 'Qwen 3.6 Flash', is_default: false },
  { model_id: 'qwen-turbo', label: 'Qwen Turbo', is_default: false },
  { model_id: 'deepseek-v3', label: 'DeepSeek V3', is_default: false },
  { model_id: 'coding-minimax-m2.7-free', label: 'Minimax M2.7 (Free)', is_default: false },
  { model_id: 'qwen3.6-plus-preview-free', label: 'Qwen 3.6 Plus (Free)', is_default: false },
];
```

Add the model selector `<select>` to the toolbar JSX, after the Query field and before the runtime health status:

```tsx
<div className="field">
  <label htmlFor="model">Model</label>
  <select
    id="model"
    value={modelId}
    onChange={(event) => onModelChange(event.target.value)}
  >
    {MODEL_OPTIONS.map((m) => (
      <option key={m.model_id} value={m.model_id}>
        {m.label}
      </option>
    ))}
  </select>
</div>
```

- [ ] **Step 3: Wire model state and PasswordGate into App.tsx**

In `apps/workbench/src/App.tsx`:

Add the import for PasswordGate:

```tsx
import { PasswordGate } from './components/auth/PasswordGate'
```

Add model state after the existing state declarations:

```tsx
const [modelId, setModelId] = useState('gemini-2.5-flash-nothink')
```

In the `handleRunAttribution` function, pass `model_id` to the request. Change the `createLiveRun` call from:

```tsx
const response = await createLiveRun({
  ticker,
  trade_date: tradeDate,
  query,
})
```

to:

```tsx
const response = await createLiveRun({
  ticker,
  trade_date: tradeDate,
  query,
  model_id: modelId,
})
```

Pass `modelId` and `onModelChange` to `ContextBar`:

```tsx
<ContextBar
  bootstrap={bootstrap}
  ticker={ticker}
  tradeDate={tradeDate}
  query={query}
  modelId={modelId}
  onTickerChange={setTicker}
  onTradeDateChange={setTradeDate}
  onQueryChange={setQuery}
  onModelChange={setModelId}
/>
```

Wrap the entire return JSX in `<PasswordGate>`:

```tsx
return (
  <PasswordGate>
    <div className="app-shell">
      {/* ... existing content unchanged ... */}
    </div>
  </PasswordGate>
)
```

- [ ] **Step 4: Stage changes**

```bash
git add apps/workbench/src/api/types.ts apps/workbench/src/api/client.ts apps/workbench/src/components/context/ContextBar.tsx apps/workbench/src/App.tsx
```

---

### Task 7: Model Verification Script

**Files:**
- Create: `scripts/verify_aihubmix_models.py`

This standalone script tests each of the 8 models against the aihubmix API. It sends a minimal prompt and reports success/failure/latency.

- [ ] **Step 1: Create the verification script**

Create `scripts/verify_aihubmix_models.py`:

```python
#!/usr/bin/env python3
"""Verify which aihubmix models are callable.

Sends a minimal prompt to each supported model and reports
success/failure with latency. Reads AIHUBMIX_API_KEY from env.

Usage:
    export AIHUBMIX_API_KEY="sk-..."
    python scripts/verify_aihubmix_models.py
"""
from __future__ import annotations

import os
import sys
import time

from openai import OpenAI


AIHUBMIX_BASE_URL = "https://aihubmix.com/v1"

MODELS = [
    "gemini-2.5-flash-nothink",
    "claude-opus-4-6",
    "deepseek-v4-flash",
    "qwen3.6-flash",
    "qwen-turbo",
    "deepseek-v3",
    "coding-minimax-m2.7-free",
    "qwen3.6-plus-preview-free",
]

TEST_PROMPT = "Reply with exactly one word: hello"


def verify_model(client: OpenAI, model_id: str) -> dict:
    start = time.perf_counter()
    try:
        response = client.chat.completions.create(
            model=model_id,
            messages=[{"role": "user", "content": TEST_PROMPT}],
            max_tokens=10,
            temperature=0.0,
        )
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        content = response.choices[0].message.content.strip() if response.choices else ""
        return {
            "model": model_id,
            "status": "OK",
            "latency_ms": elapsed_ms,
            "response": content[:50],
        }
    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return {
            "model": model_id,
            "status": "FAIL",
            "latency_ms": elapsed_ms,
            "error": str(exc)[:120],
        }


def main():
    api_key = os.environ.get("AIHUBMIX_API_KEY", "").strip()
    if not api_key:
        print("ERROR: AIHUBMIX_API_KEY environment variable is not set.")
        sys.exit(1)

    client = OpenAI(api_key=api_key, base_url=AIHUBMIX_BASE_URL)

    print(f"Testing {len(MODELS)} models against aihubmix...\n")
    print(f"{'Model':<35} {'Status':<8} {'Latency':<10} {'Response / Error'}")
    print("-" * 90)

    results = []
    for model_id in MODELS:
        result = verify_model(client, model_id)
        results.append(result)

        status_str = result["status"]
        latency_str = f"{result['latency_ms']}ms"
        detail = result.get("response", result.get("error", ""))
        print(f"{model_id:<35} {status_str:<8} {latency_str:<10} {detail}")

    ok_count = sum(1 for r in results if r["status"] == "OK")
    fail_count = len(results) - ok_count
    print(f"\nSummary: {ok_count} OK, {fail_count} FAIL out of {len(results)} models")

    if fail_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the verification script**

Run:
```bash
export AIHUBMIX_API_KEY="$(grep -oP 'aihubmix_api_key="\K[^"]+' packages/data-core/.env)"
python scripts/verify_aihubmix_models.py
```

Expected: Table output showing each model's status. Some may fail — that is expected and informational. Record which models succeeded.

- [ ] **Step 3: Stage the script**

```bash
git add scripts/verify_aihubmix_models.py
```

---

### Task 8: Set Up `AIHUBMIX_API_KEY` in Backend Environment

**Files:**
- Modify: `packages/app/.env` (create if needed)

The backend needs to read `AIHUBMIX_API_KEY` at runtime. The key exists in `packages/data-core/.env`. We need it in the backend's env as well.

- [ ] **Step 1: Create or update `packages/app/.env`**

```bash
# Check if .env exists
ls -la packages/app/.env 2>/dev/null

# If not, create it. Copy just the AIHUBMIX key.
grep "aihubmix_api_key" packages/data-core/.env
```

Create `packages/app/.env` with:

```
AIHUBMIX_API_KEY=sk-NSUAjLPfYEBwPRixE2820d6a65C64cA69d1d95CbF35c6a00
```

- [ ] **Step 2: Ensure `.env` is in `.gitignore`**

Run: `grep -n ".env" .gitignore`

If `.env` is not listed, add it. Verify: `.env` files must never be committed.

- [ ] **Step 3: Verify environment loading**

The FastAPI app needs to load `.env` at startup. Check if `python-dotenv` is used. If not, add a `load_dotenv()` call at the top of `main.py`:

Add to `packages/app/catalyst_app/main.py`, before all other imports:

```python
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
```

This is a soft dependency — if `python-dotenv` is installed it loads `.env`, otherwise the user must set env vars manually.

- [ ] **Step 4: Stage changes (only .py files — never stage .env)**

```bash
git add packages/app/catalyst_app/main.py
```

---

### Task 9: Integration Smoke Test

This task verifies the full chain works end-to-end.

- [ ] **Step 1: Start the backend and verify `/api/models` responds**

Run:
```bash
cd packages/app
AIHUBMIX_API_KEY="$(grep -oP 'aihubmix_api_key="\K[^"]+' ../data-core/.env)" \
  uvicorn catalyst_app.main:app --port 8000 &
sleep 3
curl -s http://localhost:8000/api/models | python -m json.tool
```

Expected: JSON response with 8 models, `default_model_id: "gemini-2.5-flash-nothink"`

- [ ] **Step 2: Start the frontend dev server**

Run:
```bash
cd apps/workbench
npm run dev &
```

Open the browser. Verify:
1. Password gate appears
2. Enter `catalyst2026` — workbench loads
3. Model selector dropdown is visible in the toolbar with 8 options
4. Default selection is "Gemini 2.5 Flash"

- [ ] **Step 3: Kill test servers**

```bash
kill %1 %2  # or however the background jobs are numbered
```

- [ ] **Step 4: Stage any final tweaks**

```bash
git status
# Stage any remaining modified files
```

---

### Task 10: Final Review and Cleanup

- [ ] **Step 1: Verify no AI traces in new code**

Run:
```bash
grep -rni "claude\|anthropic\|openai\|gpt\|co-authored" \
  packages/app/catalyst_app/llm_factory.py \
  packages/app/catalyst_app/llm_factory_test.py \
  apps/workbench/src/components/auth/PasswordGate.tsx \
  scripts/verify_aihubmix_models.py
```

Expected: No hits referencing AI tools (references to model names like `claude-opus-4-6` are fine — they are product names, not attribution).

The `langchain_openai` import and `ChatOpenAI` class name are acceptable — they are library imports, not AI attribution traces.

- [ ] **Step 2: Verify all files are staged**

Run: `git status`

Expected: All new/modified files are staged. No `.env` files staged. No unintended changes.

- [ ] **Step 3: Report staged changes**

List all staged files and their purpose. Do not commit — wait for user authorization.
