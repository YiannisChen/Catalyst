import type React from 'react';
import { useState } from 'react';
import { Panel } from '@/components/ui';
import { AnswerView } from '@/features/workbench/answer/AnswerView';
import { AnswerHeaderActions } from '@/features/workbench/answer/AnswerHeaderActions';
import { toAnswerModel, toProcessModel } from '@/lib/answer';
import type { AnswerQuote } from '@/features/workbench/answer/model';
import { ExampleSection } from './ExampleSection';
import answered from '../../../../../contracts/fixtures/page-a-answered-now-b5.json';
import possible from '../../../../../contracts/fixtures/page-a-abstain-possible-explanation.json';
import leads from '../../../../../contracts/fixtures/page-a-abstain-leads-only.json';
import tier from '../../../../../contracts/fixtures/page-a-tier-only-aapl-2026-03-24.json';
import failed from '../../../../../contracts/fixtures/page-a-run-failed.json';
import legacy from '../../../../../contracts/fixtures/page-a-partial-legacy.json';

const fixtures = [
  { name: 'Answered', data: answered.response, tab: 'answer' as const },
  { name: 'Possible explanations', data: possible.response, tab: 'answer' as const },
  { name: 'Leads only', data: leads.response, tab: 'answer' as const },
  { name: 'Tier only', data: tier.response, tab: 'answer' as const },
  { name: 'Failed', data: failed.response, tab: 'answer' as const },
  { name: 'Legacy', data: legacy.response, tab: 'answer' as const },
];
type Fixture = (typeof fixtures)[number];
function AnswerCard({
  fixture,
  initialTab,
}: {
  fixture: Fixture;
  initialTab: 'answer' | 'process';
}): React.JSX.Element {
  const [tab, setTab] = useState<'answer' | 'process'>(initialTab);
  const view = fixture.data as Parameters<typeof toAnswerModel>[0];
  const answer = toAnswerModel(view);
  const process = toProcessModel(view);
  return (
    <Panel
      title={fixture.name}
      actions={[<AnswerHeaderActions key="actions" answer={answer} process={process} tab={tab} />]}
      className="answer-gallery-card shrink-0"
    >
      <AnswerView
        answer={answer}
        process={process}
        tab={tab}
        onTabChange={(value) => setTab(value as 'answer' | 'process')}
        selectedQuoteId={null}
        onSelectQuote={(_quote: AnswerQuote) => undefined}
        onBack={() => undefined}
        onRunAgain={() => undefined}
      />
    </Panel>
  );
}
export function AnswerSection(): React.JSX.Element {
  return (
    <ExampleSection label="Workbench" name="Finished run answers">
      <div className="flex flex-wrap gap-3">
        <AnswerCard fixture={fixtures[0]!} initialTab="answer" />
        <AnswerCard fixture={fixtures[0]!} initialTab="process" />
        {fixtures.slice(1).map((fixture) => (
          <AnswerCard key={fixture.name} fixture={fixture} initialTab="answer" />
        ))}
      </div>
    </ExampleSection>
  );
}
