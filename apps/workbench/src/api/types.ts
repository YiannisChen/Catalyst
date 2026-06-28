export type RuntimeStatus =
  | 'QUEUED'
  | 'RUNNING'
  | 'SUCCEEDED'
  | 'PARTIAL'
  | 'INSUFFICIENT'
  | 'FAILED_SYSTEM'
  | 'FAILED_REQUEST'
  | 'CANCELLED'

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
  model_id?: string | null  // DEPRECATED — use model instead
  model?: ModelConfig | null
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
  model_id?: string | null  // DEPRECATED — use model instead
  model?: ModelConfig | null
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


// ── Session endpoint ──

export interface SessionResponse {
  ticker: string
  trade_date: string
  open: number | null
  high: number | null
  low: number | null
  close: number | null
  volume: number | null
  previous_close: number | null
  close_move_pct: number | null
  intraday_range: number | null
  source: string | null
  event_window_start: string | null
  event_window_end: string | null
  is_trading_day: boolean
}


// ── Workspace endpoint ──

export interface WorkspaceCause {
  text: string
  category?: string | null
  direction?: string | null
  confidence?: number | null
  evidence_ids: string[]
}

export interface WorkspaceResult {
  output_status?: string | null
  summary_md?: string | null
  grounding_rate?: number | null
  causes: WorkspaceCause[]
  validation_error?: string | null
  validator_attempts?: number | null
}

export interface WorkspaceEvidenceItem {
  id: string
  ticker?: string | null
  headline?: string | null
  snippet?: string | null
  source_type?: string | null
  reference_date?: string | null
  retrieval_score?: number | null
  rerank_score?: number | null
  critic_relevance?: number | null
  critic_category?: string | null
  critic_reasoning?: string | null
  temporal_match?: boolean | null
  event_specificity?: number | null
  temporal_alignment?: number | null
  evidence_granularity?: number | null
  conflict_signal?: number | null
  critic_decision?: string | null
  cited: boolean
}

export interface WorkspaceStage {
  id: string
  label: string
  status: string
  started_at?: string | null
  ended_at?: string | null
  duration_ms?: number | null
  input_tokens?: number | null
  output_tokens?: number | null
  cost_usd?: number | null
  summary?: string | null
  artifact_types: string[]
}

export interface WorkspaceDiagnostics {
  resolved_stage_count: number
  total_stage_count: number
  retrieved_count: number
  reranked_count: number
  graded_count: number
  cited_count: number
  top_evidence_score?: number | null
  total_tokens?: number | null
  total_cost_usd?: number | null
}

export interface WorkspaceFailure {
  status?: string | null
  message?: string | null
  source?: string | null
  node?: string | null
}

export interface WorkspaceResponse {
  run_id: string
  status: string
  ticker?: string | null
  trade_date?: string | null
  model_id?: string | null
  started_at?: string | null
  ended_at?: string | null
  runtime_ms?: number | null
  last_completed_node?: string | null
  predicted_next_node?: string | null
  result?: WorkspaceResult | null
  evidence: WorkspaceEvidenceItem[]
  stages: WorkspaceStage[]
  diagnostics?: WorkspaceDiagnostics | null
  failure?: WorkspaceFailure | null
}

// ── BYOK Credential Source ──

export type CredentialSource = 'server_env' | 'browser_key';

// ── BYOK Model Config ──

export interface ModelConfig {
  provider: string
  model_id: string
  api_key: string  // may be "" for server_env
  base_url?: string | null
  credential_source: CredentialSource
}

export interface ModelValidateRequest {
  provider: string
  model_id: string
  api_key: string  // may be "" for server_env
  base_url?: string | null
  credential_source: string
}

export interface ModelValidateResponse {
  ok: boolean
  provider: string
  model_id: string
  status: 'ready' | 'auth_failed' | 'request_failed' | 'timeout' | 'invalid'
  message?: string | null
  latency_ms?: number | null
}

// ── Model Catalog ──

export interface CatalogModel {
  id: string
  label: string
  tier?: string | null  // "value" | "quality" | "reasoning"
  recommended: boolean
  notes?: string | null
}

export interface CatalogProvider {
  id: string
  label: string
  executable: boolean
  env_key_configured: boolean
  default_model_id?: string | null
  models: CatalogModel[]
}

export interface ModelCatalogResponse {
  providers: CatalogProvider[]
}
