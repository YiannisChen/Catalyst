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

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? '/api'

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
