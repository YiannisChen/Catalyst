import type {
  ArtifactResponse,
  ArtifactType,
  CreateRunRequest,
  CreateRunResponse,
  ModelsResponse,
  OhlcvResponse,
  RangeLocalResponse,
  RetryRunRequest,
  RetryRunResponse,
  RunEventResponse,
  RunSummaryResponse,
  RuntimeHealthResponse,
  TickersResponse,
} from './types'

const viteEnv = (import.meta as unknown as { env?: Record<string, string | undefined> }).env
const API_BASE_URL = viteEnv?.VITE_API_BASE_URL ?? '/api'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers ?? {}),
    },
    ...init,
  })

  if (!response.ok) {
    const text = await response.text()
    throw new Error(`API request failed (${response.status}): ${text}`)
  }

  return (await response.json()) as T
}

export function getTickers(): Promise<TickersResponse> {
  return request<TickersResponse>('/tickers')
}

export function getOhlcv(
  ticker: string,
  params?: { start_date?: string; end_date?: string },
): Promise<OhlcvResponse> {
  const query = new URLSearchParams()
  if (params?.start_date) query.set('start_date', params.start_date)
  if (params?.end_date) query.set('end_date', params.end_date)
  const suffix = query.toString() ? `?${query.toString()}` : ''
  return request<OhlcvResponse>(`/ohlcv/${encodeURIComponent(ticker)}${suffix}`)
}

export function getRangeLocal(): Promise<RangeLocalResponse> {
  return request<RangeLocalResponse>('/range-local')
}

export function getModels(): Promise<ModelsResponse> {
  return request<ModelsResponse>('/models')
}

export function createLiveRun(payload: CreateRunRequest): Promise<CreateRunResponse> {
  return request<CreateRunResponse>('/live-runs', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function getLiveRun(runId: string): Promise<RunSummaryResponse> {
  return request<RunSummaryResponse>(`/live-runs/${encodeURIComponent(runId)}`)
}

export function getLiveRunEvents(runId: string, afterSeq?: number): Promise<RunEventResponse[]> {
  const query = afterSeq !== undefined ? `?after_seq=${afterSeq}` : ''
  return request<RunEventResponse[]>(`/live-runs/${encodeURIComponent(runId)}/events${query}`)
}

export function getLiveRunArtifacts(
  runId: string,
  filters?: { event_seq?: number; artifact_type?: ArtifactType },
): Promise<ArtifactResponse[]> {
  const query = new URLSearchParams()
  if (filters?.event_seq !== undefined) query.set('event_seq', String(filters.event_seq))
  if (filters?.artifact_type) query.set('artifact_type', filters.artifact_type)
  const suffix = query.toString() ? `?${query.toString()}` : ''
  return request<ArtifactResponse[]>(`/live-runs/${encodeURIComponent(runId)}/artifacts${suffix}`)
}

export function retryLiveRun(runId: string, payload: RetryRunRequest): Promise<RetryRunResponse> {
  return request<RetryRunResponse>(`/live-runs/${encodeURIComponent(runId)}/retry`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function cancelLiveRun(runId: string): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(`/live-runs/${encodeURIComponent(runId)}/cancel`, {
    method: 'POST',
  })
}

export function cancelAllRuns(): Promise<{ ok: boolean; cancelled: number }> {
  return request<{ ok: boolean; cancelled: number }>('/live-runs/cancel-all', {
    method: 'POST',
  })
}

export function getRuntimeHealth(): Promise<RuntimeHealthResponse> {
  return request<RuntimeHealthResponse>('/health/runtime')
}

export interface NewsItem {
  asset_id: string
  ticker: string
  reference_date: string
  published_utc: string | null
  title: string
  source_line: string
  snippet: string
}

export interface NewsResponse {
  ticker: string
  trade_date: string
  items: NewsItem[]
  count: number
}

export interface FundamentalsResponse {
  ticker: string
  reference_date: string | null
  metrics: Record<string, string>
}

export function getNews(
  ticker: string,
  tradeDate: string,
  windowDays = 3,
): Promise<NewsResponse> {
  const q = new URLSearchParams({ trade_date: tradeDate, window_days: String(windowDays) })
  return request<NewsResponse>(`/news/${encodeURIComponent(ticker)}?${q}`)
}

export function getFundamentals(
  ticker: string,
  tradeDate: string,
): Promise<FundamentalsResponse> {
  const q = new URLSearchParams({ trade_date: tradeDate })
  return request<FundamentalsResponse>(`/fundamentals/${encodeURIComponent(ticker)}?${q}`)
}

export function validateModel(
  payload: import('./types').ModelValidateRequest,
): Promise<import('./types').ModelValidateResponse> {
  return request<import('./types').ModelValidateResponse>('/models/validate', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function getWorkspace(runId: string): Promise<import('./types').WorkspaceResponse> {
  return request<import('./types').WorkspaceResponse>(`/live-runs/${encodeURIComponent(runId)}/workspace`)
}
export function getCatalog(): Promise<import('./types').ModelCatalogResponse> {
  return request<import('./types').ModelCatalogResponse>('/models/catalog')
}

// ── V1.1 SSE stream client (M6-10) ──

import type { PublicRunEvent, RunEventType } from './types'

export function buildStreamUrl(runId: string, lastAppliedSequence: number, apiBase = API_BASE_URL): string {
  const base = `${apiBase}/live-runs/${encodeURIComponent(runId)}/stream`
  if (lastAppliedSequence > 0) {
    return `${base}?last_event_id=${encodeURIComponent(runId)}:${lastAppliedSequence}`
  }
  return base
}

export function parseSseFrame(frame: string): PublicRunEvent {
  const dataLine = frame
    .split('\n')
    .find((line) => line.startsWith('data: '))
  if (!dataLine) {
    throw new Error('SSE frame missing data line')
  }
  const parsed = JSON.parse(dataLine.slice('data: '.length)) as PublicRunEvent
  if (!parsed.run_id || typeof parsed.sequence !== 'number') {
    throw new Error('SSE frame is not a public run event')
  }
  return parsed
}

const ALL_EVENT_TYPES: RunEventType[] = [
  'run.accepted',
  'stage.started',
  'evidence.retrieved',
  'evidence.reranked',
  'evidence.assessed',
  'followup.started',
  'answer.started',
  'answer.delta',
  'answer.completed',
  'assurance.completed',
  'run.completed',
  'run.failed',
  'run.cancelled',
]

export interface SseStreamHandlers {
  onEvent: (event: PublicRunEvent) => void
  onError?: (error: Error) => void
}

/**
 * Connect to the persisted replay + live tail. The native EventSource
 * reconnects with Last-Event-ID after a browser disconnect, so the server
 * replays from the last applied sequence; a browser disconnect never cancels
 * a run (Final TSD §18.5).
 */
export function connectRunStream(
  runId: string,
  lastAppliedSequence: number,
  handlers: SseStreamHandlers,
): () => void {
  const es = new EventSource(buildStreamUrl(runId, lastAppliedSequence))
  const onMessage = (event: MessageEvent<string>) => {
    try {
      handlers.onEvent(parseSseFrame(event.data))
    } catch (err) {
      // Heartbeat comments and malformed frames are non-persisted transport.
      handlers.onError?.(err instanceof Error ? err : new Error('invalid SSE frame'))
    }
  }
  for (const eventType of ALL_EVENT_TYPES) {
    es.addEventListener(eventType, onMessage)
  }
  es.onerror = () => {
    handlers.onError?.(new Error('SSE stream error'))
  }
  return () => es.close()
}

export function getLiveRunV1(runId: string): Promise<import('./types').RunDTO> {
  return request<import('./types').RunDTO>(`/live-runs/${encodeURIComponent(runId)}`)
}

export function getArtifactPage(
  runId: string,
  filters?: { limit?: number; offset?: number; artifact_type?: string },
): Promise<import('./types').ArtifactRefPage> {
  const query = new URLSearchParams()
  if (filters?.limit !== undefined) query.set('limit', String(filters.limit))
  if (filters?.offset !== undefined) query.set('offset', String(filters.offset))
  if (filters?.artifact_type) query.set('artifact_type', filters.artifact_type)
  const suffix = query.toString() ? `?${query.toString()}` : ''
  return request<import('./types').ArtifactRefPage>(
    `/live-runs/${encodeURIComponent(runId)}/artifacts${suffix}`,
  )
}
