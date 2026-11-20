import type React from 'react';
import { Tabs } from '@/components/ui';
import type { AnswerModel, AnswerQuote, ProcessModel } from './model';
import { AnswerTab } from './AnswerTab';
import { ProcessTab } from './ProcessTab';
import { RunTab } from './RunTab';

export type AnswerViewProps = {
  answer: AnswerModel;
  process: ProcessModel;
  tab: 'answer' | 'process' | 'run';
  onTabChange: (tab: 'answer' | 'process' | 'run') => void;
  selectedQuoteId: string | null;
  quoteLinked?: boolean;
  onSelectQuote: (quote: AnswerQuote) => void;
  onBack: () => void;
  onRunAgain?: (capOverride?: number) => void;
  defaultCapUsd?: number;
  featured?: boolean;
};
export function AnswerView(props: AnswerViewProps): React.JSX.Element {
  const { answer, process, tab, onTabChange } = props;
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-4">
      <div className="polish-answer-tabs">
        <Tabs
          value={tab}
          onChange={(value) => onTabChange(value as AnswerViewProps['tab'])}
          items={[
            { id: 'answer', label: 'Answer' },
            { id: 'process', label: 'Process', count: process.quoteChecks.length || undefined },
            { id: 'run', label: 'Run' },
          ]}
        />
        <span className="type-caption font-numeric text-ink-300">
          {answer.meta.durationSeconds == null ? '' : `${answer.meta.durationSeconds.toFixed(1)} s`}
          {answer.meta.durationSeconds != null && answer.meta.costUsd != null ? ' · ' : ''}
          {answer.meta.costUsd == null
            ? ''
            : `${answer.meta.costHint ? '≤ ' : ''}$${answer.meta.costUsd.toFixed(3)}`}
        </span>
      </div>
      <div
        id={`tabpanel-${tab}`}
        role="tabpanel"
        aria-labelledby={`tab-${tab}`}
        className="flex min-h-0 flex-1 flex-col gap-4"
      >
        {tab === 'answer' && (
          <AnswerTab
            answer={answer}
            process={process}
            onShowProcess={() => onTabChange('process')}
            selectedQuoteId={props.selectedQuoteId}
            quoteLinked={props.quoteLinked}
            onSelectQuote={props.onSelectQuote}
            onBack={props.onBack}
            onRunAgain={props.onRunAgain}
            defaultCapUsd={props.defaultCapUsd}
          />
        )}
        {tab === 'process' && (
          <ProcessTab process={process} onRunAgain={props.onRunAgain} featured={props.featured} />
        )}
        {tab === 'run' && <RunTab answer={answer} />}
      </div>
    </div>
  );
}
