export type RuntimeStatus =
  | 'QUEUED'
  | 'RUNNING'
  | 'SUCCEEDED'
  | 'PARTIAL'
  | 'INSUFFICIENT'
  | 'FAILED_SYSTEM'
  | 'FAILED_REQUEST'

export type TraceStatus = RuntimeStatus | 'SUFFICIENT' | 'SYSTEM_ERROR'

export type ArtifactType =
  | 'retrieved_chunks'
  | 'reranked_chunks'
  | 'graded_evidence'
  | 'all_graded_chunks'
  | 'critic_decision'
  | 'raw_llm_response'
  | 'state_snapshot'
  | 'error_snapshot'
  | 'judge_causes'
  | 'judge_summary'
  | 'validator_decision'

export type HealthStatus = 'ready' | 'degraded' | 'failed'

export interface FailurePayload {
  status: RuntimeStatus
  sub_reason?: string | null
  message?: string | null
  source?: string | null
  node?: string | null
  field?: string | null
  retryable: boolean
}

export interface RetryMetadata {
  parent_run_id?: string | null
  retry_count: number
  retryable: boolean
}

export interface CreateRunRequest {
  ticker: string
  trade_date: string
  query?: string | null
  model_id?: string | null
  config?: string
}

export interface CreateRunResponse {
  run_id: string
  status: RuntimeStatus
  queued_at?: string | null
  model_id?: string | null
  failure?: FailurePayload | null
}

export interface RunSummaryResponse {
  run_id: string
  status: RuntimeStatus
  ticker?: string | null
  trade_date?: string | null
  model_id?: string | null
  queued_at?: string | null
  started_at?: string | null
  ended_at?: string | null
  last_completed_node?: string | null
  predicted_next_node?: string | null
  failure?: FailurePayload | null
  retry?: RetryMetadata | null
}

export interface RunEventResponse {
  run_id: string
  trace_id?: string | null
  event_seq: number
  node: string
  status_before?: TraceStatus | null
  status_after?: TraceStatus | null
  model_id?: string | null
  started_at?: string | null
  ended_at?: string | null
  latency_ms?: number | null
  input_tokens?: number | null
  output_tokens?: number | null
  cost_usd?: number | null
}

export interface ArtifactResponse {
  run_id: string
  event_seq: number
  node: string
  artifact_type: ArtifactType
  created_at?: string | null
  payload: Record<string, unknown>
}

export interface RetryRunRequest {
  model_id?: string | null
}

export interface RetryRunResponse {
  ok: boolean
  run_id?: string | null
  status?: RuntimeStatus | null
  retry?: RetryMetadata | null
  failure?: FailurePayload | null
}

export interface HealthComponent {
  status: HealthStatus
  message?: string | null
  model?: string | null
  path?: string | null
  table?: string | null
  vector_dim?: number | null
}

export interface RuntimeHealthResponse {
  status: HealthStatus
  sqlite: HealthComponent
  lancedb: HealthComponent
  embedding: HealthComponent
  reranker: HealthComponent
  default_model: HealthComponent
  errors: Array<Record<string, unknown>>
}

export interface TickersResponse {
  symbols: string[]
  count: number
}

export interface OhlcvCandle {
  symbol: string
  date: string
  open: number | null
  high: number | null
  low: number | null
  close: number | null
  volume: number | null
  source: string | null
}

export interface OhlcvResponse {
  symbol: string
  candles: OhlcvCandle[]
  count: number
}

export interface RangeLocalResponse {
  min_date: string | null
  max_date: string | null
  ticker_count: number
  row_count: number
}

export interface ModelOption {
  model_id: string
  label: string
  is_default: boolean
}

export interface ModelsResponse {
  models: ModelOption[]
  default_model_id: string
}
