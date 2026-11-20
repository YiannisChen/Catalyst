import type React from 'react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useRunView } from '@/api/queries';
import { toAnswerModel, toProcessModel } from '@/lib/answer';
import { AnswerHeaderActions } from '@/features/workbench/answer/AnswerHeaderActions';
import { AnswerSkeleton } from '@/features/workbench/answer/AnswerSkeleton';
import { AnswerView } from '@/features/workbench/answer/AnswerView';
import type { AnswerQuote } from '@/features/workbench/answer/model';
import { getRunDefaults } from '@/lib/storage';
import { DEFAULT_COST_CAP_USD } from '@/lib/run';
import { RunningPanel, type RunningPanelProps } from './RunningPanel';

type AnswerTabId = 'answer' | 'process' | 'run';
const unfinished = new Set(['ACCEPTED', 'RUNNING', 'CANCEL_REQUESTED']);

export type RunContentProps = Omit<RunningPanelProps, 'onHeaderActions' | 'onTerminal'> & {
  tab: AnswerTabId;
  onTabChange: (tab: AnswerTabId) => void;
  newsTitles: ReadonlyMap<string, string>;
  selectedQuoteId: string | null;
  quoteLinked?: boolean;
  onSelectQuote: (quote: AnswerQuote) => void;
  onRunAgain: (capOverride?: number) => void;
  onHeaderActions: (actions: React.ReactNode | null) => void;
};

// A run in the URL is either still going (live stream) or finished (saved view).
// The saved view decides; while it does not exist yet, the live stream is shown.
// Mount with key={runId} so the live/ended flags reset per run.
export function RunContent({
  tab,
  onTabChange,
  newsTitles,
  selectedQuoteId,
  quoteLinked,
  onSelectQuote,
  onRunAgain,
  onHeaderActions,
  ...running
}: RunContentProps): React.JSX.Element {
  const [streamEnded, setStreamEnded] = useState(false);
  const [live, setLive] = useState(false);
  const view = useRunView(running.runId, { pollUntilFinished: streamEnded });
  const finished = view.data && !unfinished.has(view.data.run.lifecycle);
  const showLive = !finished && (live || !view.isLoading);

  useEffect(() => {
    if (showLive) setLive(true);
  }, [showLive]);
  const answer = useMemo(
    () => (finished && view.data ? toAnswerModel(view.data) : null),
    [finished, view.data],
  );
  const process = useMemo(
    () => (finished && view.data ? toProcessModel(view.data, newsTitles) : null),
    [finished, view.data, newsTitles],
  );

  const endStream = useCallback(() => setStreamEnded(true), []);

  // onRunAgain is a new function on most renders (its useMutation dependency is), and the header
  // effect below sets the parent's state: depending on it directly re-ran the effect without end.
  const runAgain = useRef(onRunAgain);
  useEffect(() => {
    runAgain.current = onRunAgain;
  });
  useEffect(() => {
    if (answer && process) {
      onHeaderActions(
        <AnswerHeaderActions
          answer={answer}
          process={process}
          tab={tab}
          onRunAgain={() => runAgain.current()}
        />,
      );
    }
  }, [answer, process, tab, onHeaderActions]);

  if (answer && process) {
    return (
      <AnswerView
        answer={answer}
        process={process}
        tab={tab}
        onTabChange={onTabChange}
        selectedQuoteId={selectedQuoteId}
        quoteLinked={quoteLinked}
        onSelectQuote={onSelectQuote}
        onBack={running.onBack}
        onRunAgain={answer.failureMessage ? onRunAgain : undefined}
        defaultCapUsd={getRunDefaults().costCapUsd ?? DEFAULT_COST_CAP_USD}
      />
    );
  }
  // Once the live panel is up, keep it mounted while the saved view refetches: remounting would replay the stream.
  if (!showLive) return <AnswerSkeleton />;
  return <RunningPanel {...running} onHeaderActions={onHeaderActions} onTerminal={endStream} />;
}
