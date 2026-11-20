import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, normalizeApiError, type ApiError } from './client';
import type { components } from './schema';

function unwrap<T>(result: { data?: T; error?: unknown; response: { status: number } }): T {
  if (!result.data) throw normalizeApiError(result.error, result.response.status);
  return result.data;
}

export const queryKeys = {
  options: ['api', 'options'] as const,
  rangeLocal: ['api', 'range-local'] as const,
  ohlcv: (ticker: string, start: string, end: string) => ['api', 'ohlcv', ticker, start, end] as const,
  session: (ticker: string, date: string) => ['api', 'session', ticker, date] as const,
  sessionNews: (ticker: string, date: string, runId?: string | null) =>
    ['api', 'session-news', ticker, date, runId ?? ''] as const,
  runsForSession: (ticker: string, date: string) => ['api', 'runs', ticker, date] as const,
  runsForTicker: (ticker: string) => ['api', 'run-history', 'ticker', ticker] as const,
  recentRuns: (limit: number) => ['api', 'recent-runs', limit] as const,
  runView: (runId: string) => ['api', 'run-view', runId] as const,
  modelsCatalog: ['api', 'models-catalog'] as const,
  quota: ['api', 'quota'] as const,
  runHistoryAll: ['api', 'run-history'] as const,
  runHistory: (filter: RunHistoryFilter) =>
    [
      'api',
      'run-history',
      filter.ticker ?? '',
      filter.outcome ?? '',
      filter.cursor ?? '',
      filter.limit,
    ] as const,
  latestDataJob: ['api', 'data-job-latest'] as const,
  dataJobHistory: ['api', 'data-job-history'] as const,
  dataSchedule: ['api', 'data-schedule'] as const,
  notifyAdminStatus: ['api', 'notify-admin-status'] as const,
  dataOverview: (sessions: number) => ['api', 'data-overview', sessions] as const,
  featured: ['api', 'featured'] as const,
  featuredDetail: (caseId: string) => ['api', 'featured', caseId] as const,
  featuredReplay: (caseId: string) => ['api', 'featured', caseId, 'replay'] as const,
};
export type RunHistoryFilter = { ticker?: string; outcome?: string; cursor?: string; limit: number };
export function useOptions() {
  return useQuery<components['schemas']['OptionsResponse'], ApiError>({
    queryKey: queryKeys.options,
    staleTime: 5 * 60_000,
    queryFn: async () => {
      const result = await api.GET('/api/options');
      return unwrap(result);
    },
  });
}

export function useQuota() {
  return useQuery<components['schemas']['QuotaResponse'], ApiError>({
    queryKey: queryKeys.quota,
    queryFn: async () => {
      const result = await api.GET('/api/quota');
      return unwrap(result);
    },
  });
}

export type ValidateKeyInput = {
  provider: string;
  model_id: string;
  key: string;
};

export function useValidateKey() {
  return useMutation<components['schemas']['ModelValidateResponse'], ApiError, ValidateKeyInput>({
    mutationFn: async ({ provider, model_id, key }) => {
      const result = await api.POST('/api/models/validate', {
        body: { provider, model_id },
        params: { header: { 'X-Provider-Key': key } },
      });
      return unwrap(result);
    },
  });
}

export function useCreateRun() {
  // The API returns a new run (RunAcceptedResponse) or an existing cached one (PublicCacheRunDTO / RunDTO);
  // all carry run_id.
  return useMutation<
    | components['schemas']['RunAcceptedResponse']
    | components['schemas']['PublicCacheRunDTO']
    | components['schemas']['RunDTO'],
    ApiError,
    { body: components['schemas']['CreateRunRequest']; turnstileToken?: string }
  >({
    mutationFn: async ({ body, turnstileToken }) => {
      const result = await api.POST('/api/live-runs', {
        body,
        ...(turnstileToken ? { headers: { 'X-Turnstile-Token': turnstileToken } } : {}),
      });
      return unwrap(result);
    },
  });
}

export function useCancelRun() {
  return useMutation<components['schemas']['CancelResponse'], ApiError, string>({
    mutationFn: async (runId) => {
      const result = await api.POST('/api/live-runs/{run_id}/cancel', {
        params: { path: { run_id: runId } },
      });
      return unwrap(result);
    },
  });
}

const unfinishedLifecycles = new Set(['ACCEPTED', 'RUNNING', 'CANCEL_REQUESTED']);
// pollUntilFinished: after the live stream ends, the saved view can lag behind by a moment; poll until it is final.
export function useRunView(runId: string | null, { pollUntilFinished = false } = {}) {
  return useQuery<components['schemas']['RunViewDTO'], ApiError>({
    queryKey: queryKeys.runView(runId ?? ''),
    enabled: Boolean(runId),
    staleTime: 60_000,
    refetchInterval: (query) => {
      const lifecycle = query.state.data?.run.lifecycle;
      return pollUntilFinished && (!lifecycle || unfinishedLifecycles.has(lifecycle)) ? 1000 : false;
    },
    retry: (failureCount, error) => error.status !== 404 && failureCount < 3,
    queryFn: async () => {
      if (!runId) throw new Error('A run id is required');
      const result = await api.GET('/api/live-runs/{run_id}/view', {
        params: { path: { run_id: runId } },
      });
      return unwrap(result);
    },
  });
}
export function useRangeLocal() {
  return useQuery<components['schemas']['RangeLocalResponse'], ApiError>({
    queryKey: queryKeys.rangeLocal,
    queryFn: async () => {
      const result = await api.GET('/api/range-local');
      return unwrap(result);
    },
  });
}
export function useOhlcv(ticker: string, start: string, end: string) {
  return useQuery<components['schemas']['OhlcvResponse'], ApiError>({
    queryKey: queryKeys.ohlcv(ticker, start, end),
    enabled: Boolean(ticker && start && end),
    queryFn: async () => {
      const result = await api.GET('/api/ohlcv/{ticker}', {
        params: { path: { ticker }, query: { start_date: start, end_date: end } },
      });
      return unwrap(result);
    },
  });
}
export function useSession(ticker: string, date: string) {
  return useQuery<components['schemas']['SessionResponse'], ApiError>({
    queryKey: queryKeys.session(ticker, date),
    enabled: Boolean(ticker && date),
    queryFn: async () => {
      const result = await api.GET('/api/session/{ticker}', {
        params: { path: { ticker }, query: { trade_date: date } },
      });
      return unwrap(result);
    },
  });
}
// A busy session has several hundred headlines; the API pages them 100 at a time, oldest first.
// One page stopped before the open, so in-session news (and the run's evidence) never showed.
const SESSION_NEWS_MAX_PAGES = 6;

export function useSessionNews(ticker: string, date: string, runId?: string | null) {
  return useQuery<components['schemas']['SessionNewsResponse'], ApiError>({
    queryKey: queryKeys.sessionNews(ticker, date, runId),
    enabled: Boolean(ticker && date),
    queryFn: async () => {
      let merged: components['schemas']['SessionNewsResponse'] | null = null;
      let cursor: string | undefined;
      for (let page = 0; page < SESSION_NEWS_MAX_PAGES; page += 1) {
        const result = await api.GET('/api/sessions/{ticker}/{session_date}/news', {
          params: {
            path: { ticker, session_date: date },
            // run_id marks the items this run used as evidence
            query: { limit: 100, ...(cursor ? { cursor } : {}), ...(runId ? { run_id: runId } : {}) },
          },
        });
        const data = unwrap(result);
        merged = merged ? { ...data, items: [...merged.items, ...data.items] } : data;
        cursor = data.next_cursor ?? undefined;
        if (!cursor) break;
      }
      return merged!;
    },
  });
}
export function useRunsForSession(ticker: string, date: string) {
  return useQuery<components['schemas']['RunHistoryResponse'], ApiError>({
    queryKey: queryKeys.runsForSession(ticker, date),
    enabled: Boolean(ticker && date),
    queryFn: async () => {
      const result = await api.GET('/api/live-runs', { params: { query: { ticker, session_date: date } } });
      return unwrap(result);
    },
  });
}
export function useRunsForTicker(ticker: string) {
  return useQuery<components['schemas']['RunHistoryResponse'], ApiError>({
    queryKey: queryKeys.runsForTicker(ticker),
    enabled: Boolean(ticker),
    queryFn: async () => {
      const result = await api.GET('/api/live-runs', { params: { query: { ticker, limit: 100 } } });
      return unwrap(result);
    },
  });
}
export function useRecentRuns(limit = 5) {
  return useQuery<components['schemas']['RunHistoryResponse'], ApiError>({
    queryKey: queryKeys.recentRuns(limit),
    queryFn: async () => {
      const result = await api.GET('/api/live-runs', { params: { query: { limit } } });
      return unwrap(result);
    },
  });
}
export function useModelsCatalog() {
  return useQuery<components['schemas']['ModelCatalogResponse'], ApiError>({
    queryKey: queryKeys.modelsCatalog,
    staleTime: 5 * 60_000,
    queryFn: async () => {
      const result = await api.GET('/api/models/catalog');
      return unwrap(result);
    },
  });
}
export function useRunHistory(filter: RunHistoryFilter) {
  return useQuery<components['schemas']['RunHistoryResponse'], ApiError>({
    queryKey: queryKeys.runHistory(filter),
    queryFn: async () => {
      const result = await api.GET('/api/live-runs', {
        params: {
          query: {
            limit: filter.limit,
            ...(filter.ticker ? { ticker: filter.ticker } : {}),
            ...(filter.outcome ? { outcome: filter.outcome } : {}),
            ...(filter.cursor ? { cursor: filter.cursor } : {}),
          },
        },
      });
      return unwrap(result);
    },
  });
}
export function useLatestDataJob(
  refetchInterval?:
    | number
    | false
    | ((query: {
        state: { data?: components['schemas']['LatestJobResponse'] | components['schemas']['NoJobResponse'] };
      }) => number | false),
) {
  return useQuery<
    components['schemas']['LatestJobResponse'] | components['schemas']['NoJobResponse'],
    ApiError
  >({
    queryKey: queryKeys.latestDataJob,
    staleTime: 60_000,
    refetchInterval,
    queryFn: async () => {
      const result = await api.GET('/api/data/jobs/latest');
      return unwrap(result);
    },
  });
}
export function useDataJobHistory() {
  return useQuery<components['schemas']['DataJobHistoryResponse'], ApiError>({
    queryKey: queryKeys.dataJobHistory,
    queryFn: async () => {
      const result = await api.GET('/api/data/jobs/history', { params: { query: { limit: 30 } } });
      return unwrap(result);
    },
  });
}
export function useDataSchedule() {
  return useQuery<components['schemas']['DataScheduleResponse'], ApiError>({
    queryKey: queryKeys.dataSchedule,
    queryFn: async () => {
      const result = await api.GET('/api/data/schedule');
      return unwrap(result);
    },
  });
}
export function useNotifyAdminStatus() {
  return useQuery<components['schemas']['NotifyAdminStatusResponse'], ApiError>({
    queryKey: queryKeys.notifyAdminStatus,
    queryFn: async () => {
      const result = await api.GET('/api/data/notify-admin');
      return unwrap(result);
    },
  });
}
export function useNotifyAdmin() {
  const queryClient = useQueryClient();
  return useMutation<
    components['schemas']['NotifyAdminResponse'],
    ApiError,
    components['schemas']['NotifyAdminRequest']
  >({
    mutationFn: async (body) => {
      const result = await api.POST('/api/data/notify-admin', { body });
      return unwrap(result);
    },
    onSettled: () => void queryClient.invalidateQueries({ queryKey: queryKeys.notifyAdminStatus }),
  });
}
// Admin only: store sizes, free disk, search index and per-session coverage.
export function useDataOverview(sessions = 60) {
  return useQuery<components['schemas']['DataOverviewResponse'], ApiError>({
    queryKey: queryKeys.dataOverview(sessions),
    staleTime: 60_000,
    retry: (failureCount, error) => error.status !== 403 && error.status !== 401 && failureCount < 2,
    queryFn: async () => {
      const result = await api.GET('/api/data/overview', { params: { query: { sessions } } });
      return unwrap(result);
    },
  });
}
// Admin only: starts the nightly update now. Refreshes the latest job afterwards.
export function useStartDataUpdate() {
  const queryClient = useQueryClient();
  return useMutation<components['schemas']['UpdateStartedResponse'], ApiError>({
    mutationFn: async () => {
      const result = await api.POST('/api/data/update');
      return unwrap(result);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.latestDataJob });
      void queryClient.invalidateQueries({ queryKey: queryKeys.dataJobHistory });
    },
  });
}
export function useFeaturedList() {
  return useQuery<components['schemas']['FeaturedListDTO'], ApiError>({
    queryKey: queryKeys.featured,
    staleTime: 5 * 60_000,
    queryFn: async () => {
      const result = await api.GET('/api/featured');
      return unwrap(result);
    },
  });
}
export function useFeaturedDetail(caseId: string | null) {
  return useQuery<components['schemas']['FeaturedDetailDTO'], ApiError>({
    queryKey: queryKeys.featuredDetail(caseId ?? ''),
    enabled: Boolean(caseId),
    staleTime: Infinity,
    queryFn: async () => {
      if (!caseId) throw new Error('A case id is required');
      const result = await api.GET('/api/featured/{case_id}', { params: { path: { case_id: caseId } } });
      return unwrap(result);
    },
  });
}
export function useFeaturedReplay(caseId: string | null) {
  return useQuery<components['schemas']['FeaturedReplayDTO'], ApiError>({
    queryKey: queryKeys.featuredReplay(caseId ?? ''),
    enabled: Boolean(caseId),
    staleTime: Infinity,
    queryFn: async () => {
      if (!caseId) throw new Error('A case id is required');
      const result = await api.GET('/api/featured/{case_id}/replay', {
        params: { path: { case_id: caseId } },
      });
      return unwrap(result);
    },
  });
}
