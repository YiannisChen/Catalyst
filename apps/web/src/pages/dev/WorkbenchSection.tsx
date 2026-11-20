import type React from 'react';
import { useState } from 'react';
import { Badge, Button, Panel } from '@/components/ui';
import { TickerPalette } from '@/features/workbench/TickerPalette';
import { StartState } from '@/features/workbench/attribution/StartState';
import { ReadyToRun } from '@/features/workbench/attribution/ReadyToRun';
import { ExistingRun } from '@/features/workbench/attribution/ExistingRun';
import { AttributionSkeleton } from '@/features/workbench/attribution/AttributionSkeleton';
import { RunDialog } from '@/features/workbench/run/RunDialog';
import { RunningPanelExample } from '@/features/workbench/run/RunningPanelExample';
import type { components } from '@/api/schema';
import { ExampleSection } from './ExampleSection';
import modelCatalogFixture from '../../../../../contracts/fixtures/page-models-catalog.json';
import { runExamples } from './runExamples';
import quotaFixture from '../../../../../contracts/fixtures/page-quota-demo.json';

const ticker: components['schemas']['SelectableTicker'] = {
  ticker: 'NOW',
  name: 'ServiceNow',
  sector_etf: 'XLK',
};
const recentRun: components['schemas']['RunHistoryItem'] = {
  run_id: 'sample-now-2026-04-10',
  ticker: 'NOW',
  session_date: '2026-04-10',
  lifecycle_status: 'COMPLETED',
  outcome: 'ANSWERED',
  headline: 'Why ServiceNow fell',
  model: 'Sample model',
  cost_usd: 0.04,
  duration_ms: 41200,
  created_at: '2026-04-10T20:00:00Z',
};
const session: components['schemas']['SessionResponse'] = {
  ticker: 'NOW',
  trade_date: '2026-04-10',
  is_trading_day: true,
  open: 680,
  close: 628.5,
  previous_close: 680,
  high: 684,
  low: 624,
  volume: 1800000,
};

export function WorkbenchSection(): React.JSX.Element {
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [runDialogOpen, setRunDialogOpen] = useState(false);
  return (
    <>
      <ExampleSection label="Workbench" name="Start palette">
        <Button onClick={() => setPaletteOpen(true)}>Open ticker palette</Button>
        <span className="type-caption text-ink-300">
          Sample · NOW fixture is used for the board content where available.
        </span>
        <TickerPalette
          open={paletteOpen}
          onOpenChange={setPaletteOpen}
          tickers={[ticker]}
          recentRuns={[recentRun]}
          onSelectTicker={() => setPaletteOpen(false)}
          onSelectRun={() => setPaletteOpen(false)}
        />
      </ExampleSection>
      <ExampleSection label="Workbench" name="Attribution states">
        <div className="grid grid-cols-2 gap-3">
          <Panel title="Start">
            <Badge>No session</Badge>
            <StartState
              ticker="AAPL"
              suggestions={[
                { date: '2026-07-31', movePct: -7.35 },
                { date: '2026-11-04', movePct: 5.31 },
                { date: '2026-06-25', movePct: -6.12 },
              ]}
            />
            <p className="type-caption text-ink-300">
              Sample · click a candle or a large move to get started.
            </p>
          </Panel>
          <Panel title="Selected · not run">
            <Badge>Not explained yet</Badge>
            <ReadyToRun
              ticker="NOW"
              date="2026-04-10"
              session={session}
              move={-7.58}
              gap={0}
              intraday={-7.58}
              newsCount={12}
              overnightCount={5}
              inSessionCount={7}
              candles={[]}
              analysisCredential="server_env"
              anonymousVisitor={false}
              cutoffDate="2026-04-10T20:00:00Z"
              modelLine="Sample model"
              avgCost={0.04}
              avgSeconds={41.2}
              onExplain={() => undefined}
              onChangeModel={() => undefined}
              error={null}
              starting={false}
              retryable={false}
            />
            <p className="type-caption text-ink-300">Sample · NOW fell 7.58% on 2026-04-10.</p>
          </Panel>
          <Panel title="Selected · with run">
            <Badge>Answered</Badge>
            <ExistingRun
              run={recentRun}
              onOpen={() => undefined}
              onRunAgain={() => undefined}
              starting={false}
              runError={null}
            />
            <p className="type-caption text-ink-300">Sample · NOW fell 7.58% on 2026-04-10.</p>
          </Panel>
          <Panel title="Loading">
            <Badge>Loading</Badge>
            <h3 className="type-title m-0">Loading session</h3>
            <AttributionSkeleton />
          </Panel>
        </div>
      </ExampleSection>
      <ExampleSection label="Workbench" name="Choose how to run">
        <Button onClick={() => setRunDialogOpen(true)}>Open run options</Button>
        <RunDialog
          open={runDialogOpen}
          onOpenChange={setRunDialogOpen}
          ticker="NOW"
          date="2026-04-10"
          estimate={0.04}
          providers={
            modelCatalogFixture.response.providers as unknown as components['schemas']['CatalogProvider'][]
          }
          quota={quotaFixture.response as unknown as components['schemas']['QuotaResponse']}
          defaultProvider="deepseek"
          onSubmit={() => setRunDialogOpen(false)}
        />
      </ExampleSection>
      <ExampleSection label="Workbench" name="Running states">
        <div className="grid grid-cols-2 gap-3">
          <RunningPanelExample title="After facts are ready" events={runExamples.afterFacts} />
          <RunningPanelExample title="Assessing evidence" events={runExamples.midAssessment} />
          <RunningPanelExample title="Completed" events={runExamples.done} />
          <RunningPanelExample title="Could not complete" events={runExamples.failed} />
          <RunningPanelExample title="Stopped" events={runExamples.cancelled} />
        </div>
      </ExampleSection>
    </>
  );
}
