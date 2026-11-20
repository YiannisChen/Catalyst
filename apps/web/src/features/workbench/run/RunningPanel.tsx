import type React from 'react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { Badge, Button } from '@/components/ui';

import { queryKeys, useCancelRun } from '@/api/queries';
import type { PublicRunEvent } from '@/api/sse';
import type { components } from '@/api/schema';
import { useReplayStream } from './useReplayStream';
import { AnswerSkeleton } from '@/features/workbench/answer/AnswerSkeleton';
import { useRunStream } from './useRunStream';
import type { RunDialogProps } from './RunDialog';
import { RunKnownSoFar } from './RunKnownSoFar';
import { RunSteps } from './RunSteps';
import { StateNotice } from '@/components/catalyst';

type RunChoice = Parameters<RunDialogProps['onSubmit']>[0];
export type RunningPanelProps = {
  runId: string;
  ticker: string;
  date: string;
  onBack: () => void;
  onRetry: (choice: RunChoice, capOverride?: number) => void;
  onChooseRun: () => void;
  retryChoice: RunChoice | null;
  onHeaderActions: (actions: React.ReactNode | null) => void;
  onTerminal?: () => void;
  source?:
    | { kind: 'live'; runId: string }
    | {
        kind: 'replay';
        replay: components['schemas']['FeaturedReplayDTO'];
        skipped: boolean;
        onSkip: () => void;
      };
};

function outcomeLabel(events: PublicRunEvent[]): string | null {
  const completed = [...events].reverse().find((event) => event.event_type === 'run.completed');
  const outcome =
    completed?.payload && typeof completed.payload === 'object'
      ? (completed.payload as Record<string, unknown>).result_status
      : null;
  if (typeof outcome !== 'string') return null;
  if (outcome === 'ANSWERED') return 'Confirmed';
  if (outcome === 'PARTIAL') return 'Reported';
  if (outcome === 'TIER_ONLY') return 'Ordinary move';
  return 'Completed';
}

const stageNames = {
  accepted: 'Accepted',
  retrieve: 'Retrieved',
  assess: 'Assessing',
  verify: 'Checking',
  answer: 'Writing',
} as const;

export function RunningPanel({
  runId,
  ticker,
  date,
  onBack,
  onRetry,
  onChooseRun,
  retryChoice,
  onHeaderActions,
  onTerminal,
  source = { kind: 'live', runId },
}: RunningPanelProps): React.JSX.Element {
  const liveStream = useRunStream(source.kind === 'live' ? source.runId : null);
  const replayStream = useReplayStream(
    source.kind === 'replay' ? source.replay : undefined,
    source.kind === 'replay' && source.skipped,
  );
  const { events, progress, status } = source.kind === 'live' ? liveStream : replayStream;
  const { isPending: cancelPending, mutateAsync: cancel } = useCancelRun();
  const queryClient = useQueryClient();
  const [stopping, setStopping] = useState(false);
  const [now, setNow] = useState(Date.now());
  const [lastArrival, setLastArrival] = useState<number | null>(null);
  const running = source.kind === 'replay' ? !replayStream.done : progress.terminal === null;
  // Server time between the first and latest event, plus local time since the latest one arrived:
  // immune to clock skew and to the replay burst after a reload.
  const firstAt = events[0] ? Date.parse(events[0].emitted_at) : NaN;
  const latest = events.at(-1);
  const latestAt = latest ? Date.parse(latest.emitted_at) : NaN;
  const serverSpan = Number.isFinite(firstAt) && Number.isFinite(latestAt) ? latestAt - firstAt : 0;

  useEffect(() => {
    if (events.length) setLastArrival(Date.now());
  }, [events.length]);

  useEffect(() => {
    if (!running || lastArrival === null) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 100);
    return () => window.clearInterval(timer);
  }, [running, lastArrival]);

  useEffect(() => {
    if (!progress.terminal) return;
    onTerminal?.();
    if (progress.terminal.status === 'done') {
      void queryClient.invalidateQueries({ queryKey: queryKeys.runsForSession(ticker, date) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.recentRuns(5) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.runHistoryAll });
    }
  }, [progress.terminal?.status, queryClient, ticker, date, onTerminal, source.kind]);

  const elapsed =
    source.kind === 'replay'
      ? replayStream.elapsed
      : lastArrival === null
        ? 0
        : (Math.max(0, serverSpan) + Math.max(0, now - lastArrival)) / 1000;
  const facts = progress.facts;
  const relativeRows = useMemo(
    () =>
      (facts?.relative ?? []).flatMap((fact, index) =>
        fact.value === null
          ? []
          : [
              {
                label: fact.label,
                value: fact.value,
                kind: (['target', 'sector', 'peers', 'market'] as const)[index] ?? 'market',
              },
            ],
      ),
    [facts],
  );
  const targetReturn = facts?.relative[0]?.value;
  const sectorReturn = facts?.relative[1]?.value;
  const excess =
    facts?.excess ?? (targetReturn != null && sectorReturn != null ? targetReturn - sectorReturn : undefined);

  const stop = useCallback(async () => {
    setStopping(true);
    try {
      await cancel(runId);
    } catch {
      setStopping(false);
    }
  }, [cancel, runId]);

  const outcome = outcomeLabel(events);
  // Depend on the parts of source, not the object: callers may build it inline each render.
  const sourceKind = source.kind;
  const onSkip = source.kind === 'replay' ? source.onSkip : undefined;
  const headerActions = useMemo(
    () =>
      sourceKind === 'replay' ? (
        running ? (
          <div className="flex items-center gap-2">
            <Badge tone="info">
              Replay · <span className="font-numeric">{elapsed.toFixed(1)}</span> s
            </Badge>
            <Button variant="ghost" onClick={onSkip}>
              Skip to answer
            </Button>
          </div>
        ) : (
          <Badge tone="neutral">Replay</Badge>
        )
      ) : progress.terminal?.status === 'done' ? (
        <Badge tone="info">{outcome ?? 'Completed'}</Badge>
      ) : running ? (
        <div className="flex items-center gap-2">
          <Badge className="run-status-badge" tone="info" dot>
            {stageNames[progress.steps.find((step) => step.status === 'active')?.id ?? 'assess']} ·{' '}
            <span className="font-numeric">{elapsed.toFixed(1)}</span> s
          </Badge>
          <Button variant="secondary" disabled={stopping || cancelPending} onClick={() => void stop()}>
            {stopping ? 'Stopping…' : 'Stop'}
          </Button>
        </div>
      ) : null,
    [sourceKind, onSkip, progress.terminal?.status, outcome, running, elapsed, stopping, cancelPending, stop],
  );

  useEffect(() => {
    onHeaderActions(headerActions);
  }, [headerActions, onHeaderActions]);

  if (progress.terminal?.status === 'done') {
    return <AnswerSkeleton />;
  }

  if (progress.terminal?.status === 'failed' || progress.terminal?.status === 'cancelled') {
    const reason =
      progress.terminal.status === 'failed' ? progress.terminal.message : 'This run was stopped.';
    // Retry a cost-capped run with twice the cap, never above the $2.00 maximum.
    const retryCap =
      progress.terminal.status === 'failed' && progress.terminal.cap != null
        ? Math.min(2, progress.terminal.cap * 2)
        : null;
    return (
      <div className="flex flex-col gap-3">
        {progress.terminal.status === 'failed' && progress.terminal.costCap ? (
          <StateNotice
            tone="warn"
            title="Run stopped at the cost cap"
            body={
              progress.terminal.spent != null && progress.terminal.cap != null
                ? `It spent $${progress.terminal.spent.toFixed(3)} of $${progress.terminal.cap.toFixed(2)}. Nothing was published.`
                : progress.terminal.cap != null
                  ? `It reached its $${progress.terminal.cap.toFixed(2)} limit. Nothing was published.`
                  : 'The run reached its cost limit. Nothing was published.'
            }
            action={
              retryChoice && retryCap != null
                ? {
                    label: `Retry with $${retryCap.toFixed(2)}`,
                    onClick: () => onRetry(retryChoice, retryCap),
                  }
                : retryChoice
                  ? { label: 'Try again', onClick: () => onRetry(retryChoice) }
                  : { label: 'Choose a run', onClick: onChooseRun }
            }
            secondaryAction={{ label: 'Back', onClick: onBack }}
          />
        ) : (
          <StateNotice
            tone="down"
            title="Run stopped"
            body={reason}
            action={
              retryChoice
                ? { label: 'Try again', onClick: () => onRetry(retryChoice) }
                : { label: 'Choose a run', onClick: onChooseRun }
            }
            secondaryAction={{ label: 'Back', onClick: onBack }}
          />
        )}
      </div>
    );
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-auto">
      {source.kind === 'live' && stopping && <p className="m-0 type-caption text-ink-300">Stopping…</p>}
      {source.kind === 'live' && status === 'error' && (
        <p className="m-0 type-caption text-warn">Could not reconnect to this run.</p>
      )}
      <RunSteps steps={progress.steps} />
      <RunKnownSoFar
        facts={facts}
        relativeRows={relativeRows}
        excess={excess}
        candidate={progress.candidate}
        reading={progress.reading}
        readCount={progress.readCount}
        checked={Boolean(progress.finding)}
      />
      <section className="run-answer-note" aria-label="Answer">
        {progress.finding ? (
          <div className="flex flex-col gap-2 rounded-md border border-line-100 bg-bg-200 p-3">
            <Badge tone={progress.finding.supported ? 'up' : 'warn'}>
              {progress.finding.supported ? 'Cause confirmed on review' : 'Cause not confirmed on review'}
            </Badge>
            <p className="m-0 type-body-sm text-ink-100">{progress.finding.reason}</p>
            <p className="m-0 type-caption text-ink-300">
              The full answer with its sources follows once the source times are checked.
            </p>
          </div>
        ) : (
          <p className="m-0 type-caption text-ink-300">The answer appears once its quotes are checked.</p>
        )}
      </section>
    </div>
  );
}
