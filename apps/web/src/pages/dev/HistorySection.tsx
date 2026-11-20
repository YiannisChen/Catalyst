import type { JSX } from 'react';
import type { components } from '@/api/schema';
import { Button, Panel } from '@/components/ui';
import { HistoryTable } from '@/features/history/HistoryTable';
import { ExampleSection } from './ExampleSection';
import historyFixture from '../../../../../contracts/fixtures/page-runs-history.json';

type RunHistoryItem = components['schemas']['RunHistoryItem'];
const fixtureItems = (historyFixture.response as { items: RunHistoryItem[] }).items;

export function HistorySection(): JSX.Element {
  return (
    <ExampleSection label="History" name="Run history states">
      <Panel title="Runs">
        <HistoryTable items={fixtureItems} onOpen={() => undefined} />
        <div className="flex items-center justify-between border-t border-line-100 px-4 py-3 text-caption text-ink-300">
          <span>Showing {fixtureItems.length} runs</span>
          <div className="flex gap-2">
            <Button variant="ghost" size="sm" disabled>
              Previous
            </Button>
            <Button variant="ghost" size="sm" disabled>
              Next
            </Button>
          </div>
        </div>
        <p className="type-caption m-0 text-ink-300">Synthetic · page-runs-history</p>
      </Panel>
      <div className="grid grid-cols-2 gap-3">
        <Panel title="Empty">
          <p className="type-body-sm m-0 text-ink-200">
            No runs yet. Pick a session in the workbench and press Explain.
          </p>
          <Button variant="ghost" size="sm">
            Open workbench
          </Button>
        </Panel>
        <Panel title="Loading">
          <HistoryTable items={[]} loading onOpen={() => undefined} />
        </Panel>
      </div>
    </ExampleSection>
  );
}
