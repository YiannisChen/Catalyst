import { Fragment, useEffect } from 'react';
import type React from 'react';
import { Badge, Panel as Surface } from '@/components/ui';
import type { components } from '@/api/schema';
import { historyOutcome } from '@/features/history/outcome';
import { AttributionSkeleton } from './AttributionSkeleton';
import { ExistingRun } from './ExistingRun';
import { OrdinaryDay, unexplainableBadge, unexplainableKind } from './OrdinaryDay';
import { ReadyToRun } from './ReadyToRun';
import { StartState } from './StartState';

type Run = components['schemas']['RunHistoryItem'];
type Session = components['schemas']['SessionResponse'];
export type AttributionPanelProps = {
  date: string;
  /** False for a day under 2%: its news shows, but it cannot be explained. */
  explainable: boolean;
  nonTradingDate: boolean;
  runParam: string | null;
  newestRun: Run | undefined;
  coverageStart: string | null;
  daySuggestions?: { date: string; movePct: number }[];
  onPickDay?: (date: string) => void;
  isLoading: boolean;
  ticker: string;
  modelLine: string;
  tickerRuns: Run[];
  recentRuns: Run[];
  onOpenRun: (run: Run) => void;
  session: Session | undefined;
  move: number;
  gap: number | null;
  intraday: number | null;
  newsCount: number | null; // null while the session's news is still loading
  overnightCount: number;
  inSessionCount: number;
  candles: { date: string; volume: number }[];
  cutoffDate: string | undefined;
  avgCost: number | null;
  avgSeconds: number | null;
  onUpdateRun: (run: string | null) => void;
  onExplain: () => void;
  onChangeModel: () => void;
  runError: string | null;
  quotaLimit?: number;
  anonymousVisitor?: boolean;
  analysisCredential: 'browser_key' | 'server_env' | 'none';
  freeRunsRemaining?: number;
  quotaReached?: boolean;
  onAddKey?: () => void;
  starting: boolean;
  retryable: boolean;
  runningContent?: React.ReactNode;
  runningActions?: React.ReactNode;
};
export function AttributionPanel(props: AttributionPanelProps): React.JSX.Element {
  const { date, runParam, newestRun } = props;
  // A day that already has an answer opens it straight away; only failed or stopped runs keep the card.
  const openRunId =
    date &&
    !runParam &&
    newestRun &&
    newestRun.lifecycle_status !== 'FAILED' &&
    newestRun.lifecycle_status !== 'CANCELLED'
      ? newestRun.run_id
      : null;
  const { onUpdateRun } = props;
  useEffect(() => {
    if (openRunId) onUpdateRun(openRunId);
  }, [openRunId, onUpdateRun]);
  const actions = date
    ? props.runParam && props.runningActions
      ? [<Fragment key="running">{props.runningActions}</Fragment>]
      : [
          newestRun ? (
            <Badge key="run" tone={historyOutcome(newestRun).tone}>
              {historyOutcome(newestRun).label}
            </Badge>
          ) : props.explainable ? (
            <Badge key="none">Not explained yet</Badge>
          ) : (
            <Badge key="ordinary">
              {props.nonTradingDate
                ? 'No trading session'
                : unexplainableBadge[unexplainableKind(date, props.move, props.coverageStart)]}
            </Badge>
          ),
        ]
    : [];
  return (
    <Surface title="Attribution" actions={actions} className="h-full">
      <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-auto">
        {runParam && date ? (
          (props.runningContent ?? null)
        ) : !date ? (
          <StartState
            ticker={props.ticker}
            tickerRuns={props.tickerRuns}
            recentRuns={props.recentRuns}
            onOpenRun={props.onOpenRun}
            suggestions={props.daySuggestions}
            onPickDay={props.onPickDay}
          />
        ) : props.isLoading ? (
          <AttributionSkeleton />
        ) : newestRun &&
          (newestRun.lifecycle_status === 'FAILED' || newestRun.lifecycle_status === 'CANCELLED') ? (
          <ExistingRun
            run={newestRun}
            onOpen={() => props.onUpdateRun(newestRun.run_id)}
            onRunAgain={props.onExplain}
            starting={props.starting}
            runError={props.runError}
          />
        ) : newestRun ? (
          <AttributionSkeleton />
        ) : !props.explainable ? (
          <OrdinaryDay
            ticker={props.ticker}
            date={date}
            move={props.move}
            tradingDay={!props.nonTradingDate}
            newsCount={props.newsCount}
            coverageStart={props.coverageStart}
          />
        ) : (
          <ReadyToRun
            ticker={props.ticker}
            date={date}
            session={props.session}
            move={props.move}
            gap={props.gap}
            intraday={props.intraday}
            newsCount={props.newsCount}
            overnightCount={props.overnightCount}
            inSessionCount={props.inSessionCount}
            candles={props.candles}
            cutoffDate={props.cutoffDate}
            modelLine={props.modelLine}
            anonymousVisitor={Boolean(props.anonymousVisitor)}
            analysisCredential={props.analysisCredential}
            freeRunsRemaining={props.freeRunsRemaining}
            quotaLimit={props.quotaLimit}
            avgCost={props.avgCost}
            avgSeconds={props.avgSeconds}
            onExplain={props.onExplain}
            onChangeModel={props.onChangeModel}
            error={props.runError}
            quotaReached={props.quotaReached}
            onAddKey={props.onAddKey}
            starting={props.starting}
            retryable={props.retryable}
          />
        )}
      </div>
    </Surface>
  );
}
