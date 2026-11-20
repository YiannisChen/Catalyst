import type React from 'react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useFeaturedDetail, useFeaturedReplay } from '@/api/queries';
import { Badge, Button } from '@/components/ui';
import { toAnswerModel, toProcessModel } from '@/lib/answer';
import { AnswerHeaderActions } from '@/features/workbench/answer/AnswerHeaderActions';
import { AnswerSkeleton } from '@/features/workbench/answer/AnswerSkeleton';
import { AnswerView } from '@/features/workbench/answer/AnswerView';
import type { AnswerQuote } from '@/features/workbench/answer/model';
import { RunningPanel } from './RunningPanel';

type Tab = 'answer' | 'process' | 'run';
export function ReplayContent({
  caseId,
  ticker,
  date,
  tab,
  newsTitles,
  selectedQuoteId,
  quoteLinked,
  onSelectQuote,
  onTabChange,
  onBack,
  onHeaderActions,
}: {
  caseId: string;
  ticker: string;
  date: string;
  tab: Tab;
  newsTitles: ReadonlyMap<string, string>;
  selectedQuoteId: string | null;
  quoteLinked?: boolean;
  onSelectQuote: (quote: AnswerQuote) => void;
  onTabChange: (tab: Tab) => void;
  onBack: () => void;
  onHeaderActions: (actions: React.ReactNode | null) => void;
}): React.JSX.Element {
  const detail = useFeaturedDetail(caseId);
  const replay = useFeaturedReplay(caseId);
  const [skipped, setSkipped] = useState(false);
  const [playbackFinished, setPlaybackFinished] = useState(false);
  const answer = useMemo(() => (detail.data ? toAnswerModel(detail.data.view) : null), [detail.data]);
  const process = useMemo(
    () => (detail.data ? toProcessModel(detail.data.view, newsTitles) : null),
    [detail.data, newsTitles],
  );
  const finished = skipped || playbackFinished || replay.data?.events.length === 0;
  // Stable props: a fresh source object each render rebuilt RunningPanel's header actions,
  // which set the page's state and rendered this again, without end.
  const replaySource = useMemo(
    () =>
      replay.data
        ? ({ kind: 'replay', replay: replay.data, skipped, onSkip: () => setSkipped(true) } as const)
        : null,
    [replay.data, skipped],
  );
  const onPlaybackFinished = useCallback(() => setPlaybackFinished(true), []);
  const noop = useCallback(() => undefined, []);

  useEffect(() => {
    if (!answer || !process || !finished) return;
    onHeaderActions(
      <div className="flex items-center gap-2">
        <Badge tone="neutral">Replay</Badge>
        <AnswerHeaderActions answer={answer} process={process} tab={tab} />
      </div>,
    );
  }, [answer, process, finished, tab, onHeaderActions]);

  if (detail.isError || replay.isError)
    return (
      <div className="flex flex-col gap-3">
        <p className="m-0 type-body-sm text-ink-200">Could not load this featured replay.</p>
        <div className="flex gap-2">
          <Button
            variant="secondary"
            onClick={() => {
              void detail.refetch();
              void replay.refetch();
            }}
          >
            Retry
          </Button>
          <Button variant="ghost" onClick={onBack}>
            Back
          </Button>
        </div>
      </div>
    );
  if (!detail.data || !replaySource || !answer || !process) return <AnswerSkeleton />;
  if (finished)
    return (
      <AnswerView
        answer={answer}
        process={process}
        tab={tab}
        onTabChange={onTabChange}
        selectedQuoteId={selectedQuoteId}
        quoteLinked={quoteLinked}
        onSelectQuote={onSelectQuote}
        onBack={onBack}
        featured
      />
    );

  // Replays only consume the public replay DTO; they never call POST /api/live-runs or any provider.
  return (
    <RunningPanel
      runId={detail.data.run_id}
      ticker={ticker}
      date={date}
      onBack={onBack}
      onRetry={noop}
      onChooseRun={noop}
      retryChoice={null}
      onHeaderActions={onHeaderActions}
      onTerminal={onPlaybackFinished}
      source={replaySource}
    />
  );
}
