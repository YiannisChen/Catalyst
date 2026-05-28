from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


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
    model_id: str | None = None
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

    model_id: str | None = None


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
