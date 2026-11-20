import type { JSX } from 'react';
import { useEffect, useMemo, useRef } from 'react';
import {
  useDataJobHistory,
  useDataOverview,
  useDataSchedule,
  useLatestDataJob,
  useNotifyAdmin,
  useNotifyAdminStatus,
  useOptions,
  useQuota,
  useStartDataUpdate,
} from '@/api/queries';
import { StateNotice } from '@/components/catalyst';
import { Button, Panel, Skeleton } from '@/components/ui';
import { buildCoverage } from '@/features/data/coverage';
import { isLatestJob } from '@/features/data/model';
import { errorText, isForbidden } from '@/features/data/dataPage';
import type { ApiError } from '@/features/data/dataPage';
import { Header } from '@/features/data/Header';
import { Freshness } from '@/features/data/Freshness';
import { UpdateActions } from '@/features/data/UpdateActions';
import { IdleUpdate } from '@/features/data/IdleUpdate';
import { RunningUpdate } from '@/features/data/RunningUpdate';
import { CoverageSection } from '@/features/data/CoverageSection';
import { Stores } from '@/features/data/Stores';

export default function Data(): JSX.Element {
  const jobQuery = useLatestDataJob((query) =>
    query.state.data && isLatestJob(query.state.data) && query.state.data.running ? 5_000 : 60_000,
  );
  const history = useDataJobHistory();
  const overview = useDataOverview(60);
  const schedule = useDataSchedule();
  const notifyStatus = useNotifyAdminStatus();
  const options = useOptions();
  const quota = useQuota();
  const startUpdate = useStartDataUpdate();
  const notify = useNotifyAdmin();
  const running = Boolean(jobQuery.data && isLatestJob(jobQuery.data) && jobQuery.data.running);
  const wasRunning = useRef(false);
  useEffect(() => {
    if (wasRunning.current && !running) void history.refetch();
    wasRunning.current = running;
  }, [running, history.refetch]);
  const latest = jobQuery.data && isLatestJob(jobQuery.data) ? jobQuery.data : undefined;
  const tickers = options.data?.tickers.map((ticker) => ticker.ticker) ?? [];
  const coverageRows = useMemo(
    () => buildCoverage(overview.data?.coverage ?? [], tickers),
    [overview.data, options.data],
  );
  const pricesStale = Boolean(
    overview.data &&
    (!overview.data.freshness.prices_through ||
      overview.data.coverage[0]?.session_date !== overview.data.freshness.prices_through),
  );
  const headlinesStale = Boolean(
    overview.data &&
    (!overview.data.freshness.headlines_through ||
      overview.data.coverage.some(
        (s) => overview.data!.freshness.headlines_through! < s.session_date && !s.tickers_with_news.length,
      )),
  );
  const behind = pricesStale || headlinesStale;
  const notifyError = notify.error as ApiError | undefined;
  const nextAllowedAt =
    notifyStatus.data?.next_allowed_at ??
    (notifyError?.status === 429 ? notifyError.next_allowed_at : undefined);
  const actions = (
    <UpdateActions
      admin={quota.data?.tier === 'admin'}
      running={running}
      interrupted={Boolean(latest && ['RUNNING', 'PENDING'].includes(latest.status) && !latest.running)}
      start={() => startUpdate.mutate()}
      startPending={startUpdate.isPending}
      startError={startUpdate.error}
      notifyAvailable={notifyStatus.data?.available === true}
      nextAllowedAt={nextAllowedAt}
      notify={(reason) => notify.mutate({ reason })}
      notifyPending={notify.isPending}
      notifyError={notify.error}
      notifiedAt={notify.data?.next_allowed_at ?? undefined}
    />
  );
  const accessDenied = [
    overview.error,
    jobQuery.error,
    history.error,
    schedule.error,
    notifyStatus.error,
  ].some(isForbidden);
  return (
    <main className="mx-auto flex h-full w-full max-w-7xl flex-col gap-4 overflow-auto px-5 py-6">
      <Header job={jobQuery.data} stale={behind} overview={overview.data} />
      {accessDenied && (
        <StateNotice
          tone="down"
          title="Admin token needed"
          body="A data read endpoint still requires the admin token."
          action={{ label: 'Add token', to: '/settings#access' }}
        />
      )}
      <Freshness overview={overview.data} job={jobQuery.data} tickerCount={tickers.length} />
      {overview.isLoading ? (
        <Panel title="1 · Update" className="h-auto">
          <div className="flex flex-col gap-3">
            <Skeleton height={20} />
            <Skeleton height={90} />
          </div>
        </Panel>
      ) : overview.data ? (
        running && latest ? (
          <RunningUpdate overview={overview.data} job={latest} actions={actions} />
        ) : (
          <IdleUpdate
            overview={overview.data}
            job={latest}
            history={history.data?.items ?? []}
            schedule={schedule.data}
            actions={actions}
          />
        )
      ) : (
        <section className="flex items-center justify-between rounded-lg border border-line-100 bg-bg-100 px-4 py-5 text-body-sm text-ink-300">
          <span>{errorText(overview.error)}</span>
          <Button variant="ghost" size="sm" onClick={() => void overview.refetch()}>
            Retry
          </Button>
        </section>
      )}
      {overview.data ? (
        <>
          <CoverageSection overview={overview.data} rows={coverageRows} tickers={tickers} job={latest} />
          <Stores overview={overview.data} stale={headlinesStale} />
        </>
      ) : (
        <div className="grid gap-4">
          <Panel title="2 · Coverage" className="h-auto">
            <Skeleton height={160} />
          </Panel>
          <Panel title="3 · What is stored" className="h-auto">
            <Skeleton height={120} />
          </Panel>
        </div>
      )}
    </main>
  );
}
