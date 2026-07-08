from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RunStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    INSUFFICIENT = "INSUFFICIENT"
    FAILED_SYSTEM = "FAILED_SYSTEM"
    FAILED_REQUEST = "FAILED_REQUEST"


class TraceStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    INSUFFICIENT = "INSUFFICIENT"
    FAILED_SYSTEM = "FAILED_SYSTEM"
    FAILED_REQUEST = "FAILED_REQUEST"
    SUFFICIENT = "SUFFICIENT"
    SYSTEM_ERROR = "SYSTEM_ERROR"


class ArtifactType(str, Enum):
    RETRIEVED_CHUNKS = "retrieved_chunks"
    RERANKED_CHUNKS = "reranked_chunks"
    GRADED_EVIDENCE = "graded_evidence"
    ALL_GRADED_CHUNKS = "all_graded_chunks"
    CRITIC_DECISION = "critic_decision"
    RAW_LLM_RESPONSE = "raw_llm_response"
    STATE_SNAPSHOT = "state_snapshot"
    ERROR_SNAPSHOT = "error_snapshot"
    JUDGE_CAUSES = "judge_causes"
    JUDGE_SUMMARY = "judge_summary"
    VALIDATOR_DECISION = "validator_decision"


class HealthStatus(str, Enum):
    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"


class FailurePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: RunStatus
    sub_reason: str | None = None
    message: str | None = None
    source: str | None = None
    node: str | None = None
    field: str | None = None
    retryable: bool = False


class ErrorPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    details: dict[str, Any] | None = None


class RetryMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_run_id: str | None = None
    retry_count: int = 0
    retryable: bool = False


class TerminalResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary_md: str | None = None
    output_status: RunStatus | None = None
    total_cost_usd: float | None = None


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: str = Field(min_length=1)
    trade_date: str
    query: str | None = None
    model_id: str | None = None  # DEPRECATED - use model instead
    model: "ModelConfig | None" = None
    config: str = "mcj_full"

    @field_validator("query")
    @classmethod
    def _validate_query(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("query must not be blank")
        return text

    @model_validator(mode="after")
    def _validate_model_source(self) -> "CreateRunRequest":
        if self.model is None and self.model_id is None:
            raise ValueError("Either model_id (legacy) or model (BYOK) must be provided")
        return self


class CreateRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: RunStatus
    queued_at: datetime | None = None
    model_id: str | None = None
    failure: FailurePayload | None = None
    error: ErrorPayload | None = None
    retry: RetryMetadata | None = None


class RunSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: RunStatus
    ticker: str | None = None
    trade_date: str | None = None
    model_id: str | None = None
    queued_at: datetime | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    last_completed_node: str | None = None
    predicted_next_node: str | None = None
    terminal_result: TerminalResult | None = None
    failure: FailurePayload | None = None
    retry: RetryMetadata | None = None


class RunEventResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    trace_id: str | None = None
    event_seq: int
    node: str
    status_before: TraceStatus | None = None
    status_after: TraceStatus | None = None
    model_id: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    latency_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    error: FailurePayload | None = None


class ArtifactResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    run_id: str
    event_seq: int
    node: str
    artifact_type: ArtifactType
    created_at: datetime | None = None
    payload: dict[str, Any] = Field(alias="payload_json")


class RetryRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str | None = None  # DEPRECATED — use model instead
    model: ModelConfig | None = None


class RetryRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    run_id: str | None = None
    status: RunStatus | None = None
    retry: RetryMetadata | None = None
    failure: FailurePayload | None = None
    error: ErrorPayload | None = None


class HealthComponent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: HealthStatus
    message: str | None = None
    model: str | None = None
    path: str | None = None
    table: str | None = None
    vector_dim: int | None = None


class RuntimeHealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: HealthStatus
    sqlite: HealthComponent
    lancedb: HealthComponent
    embedding: HealthComponent
    reranker: HealthComponent
    default_model: HealthComponent
    errors: list[dict[str, Any]] = Field(default_factory=list)


class TickersResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbols: list[str]
    count: int


class OhlcvCandle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    date: str
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: float | None = None
    source: str | None = None


class OhlcvResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    candles: list[OhlcvCandle]
    count: int


class RangeLocalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_date: str | None
    max_date: str | None
    ticker_count: int
    row_count: int


TerminalRunStatus = Literal[
    RunStatus.SUCCEEDED,
    RunStatus.PARTIAL,
    RunStatus.INSUFFICIENT,
    RunStatus.FAILED_SYSTEM,
    RunStatus.FAILED_REQUEST,
]


class NewsItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str
    ticker: str
    reference_date: str
    published_utc: str | None = None
    title: str
    source_line: str
    snippet: str


class NewsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: str
    trade_date: str
    items: list[NewsItem]
    count: int


class FundamentalsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: str
    reference_date: str | None = None
    metrics: dict[str, str]


class ModelOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str
    label: str
    is_default: bool = False


class ModelsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    models: list[ModelOption]
    default_model_id: str


# ── Session endpoint ──

class SessionResponse(BaseModel):
    """Selected-session data derived from real OHLCV."""
    model_config = ConfigDict(extra="forbid")

    ticker: str
    trade_date: str
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: float | None = None
    previous_close: float | None = None
    close_move_pct: float | None = None
    intraday_range: float | None = None
    source: str | None = None
    event_window_start: str | None = None
    event_window_end: str | None = None
    is_trading_day: bool = False


# ── Workspace endpoint ──

class WorkspaceCause(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    category: str | None = None
    direction: str | None = None
    confidence: float | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class WorkspaceResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_status: str | None = None
    summary_md: str | None = None
    grounding_rate: float | None = None
    causes: list[WorkspaceCause] = Field(default_factory=list)
    validation_error: str | None = None
    validator_attempts: int | None = None


class WorkspaceEvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    ticker: str | None = None
    headline: str | None = None
    snippet: str | None = None
    source_type: str | None = None
    reference_date: str | None = None
    retrieval_score: float | None = None
    rerank_score: float | None = None
    critic_relevance: float | None = None
    critic_category: str | None = None
    critic_reasoning: str | None = None
    temporal_match: bool | None = None
    event_specificity: float | None = None
    temporal_alignment: float | None = None
    evidence_granularity: float | None = None
    conflict_signal: float | None = None
    critic_decision: str | None = None  # accepted | rejected | ungraded
    cited: bool = False


class WorkspaceStage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    status: str  # pending | active | complete | warning | error | skipped
    started_at: str | None = None
    ended_at: str | None = None
    duration_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    summary: str | None = None
    artifact_types: list[str] = Field(default_factory=list)


class WorkspaceDiagnostics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolved_stage_count: int = 0
    total_stage_count: int = 0
    retrieved_count: int = 0
    reranked_count: int = 0
    graded_count: int = 0
    cited_count: int = 0
    top_evidence_score: float | None = None
    total_tokens: int | None = None
    total_cost_usd: float | None = None


class WorkspaceFailure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str | None = None
    message: str | None = None
    source: str | None = None
    node: str | None = None


class WorkspaceResponse(BaseModel):
    """Projected workspace for the v4 workbench UI."""
    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: str
    ticker: str | None = None
    trade_date: str | None = None
    model_id: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    runtime_ms: int | None = None
    last_completed_node: str | None = None
    predicted_next_node: str | None = None

    result: WorkspaceResult | None = None
    evidence: list[WorkspaceEvidenceItem] = Field(default_factory=list)
    stages: list[WorkspaceStage] = Field(default_factory=list)
    diagnostics: WorkspaceDiagnostics | None = None
    failure: WorkspaceFailure | None = None

# ── BYOK Credential Source ──

class CredentialSource(str, Enum):
    """How the runtime API key is supplied."""
    SERVER_ENV = "server_env"
    BROWSER_KEY = "browser_key"


# ── BYOK Model Config ──

class ModelConfig(BaseModel):
    """BYOK model configuration supplied at runtime.

    provider + model_id + base_url + credential_source are persisted to
    agent_runs.config as non-secret metadata.  api_key is NEVER persisted --
    it is held in the in-memory RuntimeCredentialStore keyed by run_id.
    """
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(default="openai", min_length=1)
    model_id: str = Field(min_length=1)
    api_key: str = ""  # optional when credential_source="server_env"
    base_url: str | None = None
    credential_source: CredentialSource = CredentialSource.BROWSER_KEY

    @model_validator(mode="after")
    def _validate_credential_source(self) -> "ModelConfig":
        if self.credential_source == CredentialSource.BROWSER_KEY and not self.api_key:
            raise ValueError("api_key is required when credential_source is browser_key")
        return self


class ModelValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    api_key: str = ""  # optional for server_env
    base_url: str | None = None
    credential_source: CredentialSource = CredentialSource.BROWSER_KEY


class ModelValidateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    provider: str
    model_id: str
    status: str  # "ready" | "auth_failed" | "request_failed" | "timeout" | "invalid"
    message: str | None = None
    latency_ms: int | None = None



# ── Model Catalog ──

class CatalogModel(BaseModel):
    """A single model entry in the provider catalog."""
    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    tier: str | None = None  # "value" | "quality" | "reasoning"
    recommended: bool = False
    notes: str | None = None


class CatalogProvider(BaseModel):
    """A provider entry in the model catalog."""
    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    executable: bool = True
    env_key_configured: bool = False
    default_model_id: str | None = None
    models: list[CatalogModel] = Field(default_factory=list)


class ModelCatalogResponse(BaseModel):
    """Sanitized model catalog — never includes env key values."""
    model_config = ConfigDict(extra="forbid")

    providers: list[CatalogProvider] = Field(default_factory=list)
