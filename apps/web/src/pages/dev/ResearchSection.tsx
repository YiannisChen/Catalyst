import type React from 'react';
import { useCallback, useState } from 'react';
import { QuoteCard, StageStepper, RelativeBars, SessionTimeline, PriceChart } from '@/components/catalyst';
import { formatEtTime } from '@/lib/format';
import { ExampleSection } from './ExampleSection';
import {
  answer,
  facts,
  source,
  headline,
  running,
  timelineStart,
  timelineOpenAt,
  timelineCloseAt,
  timelineCutoffAt,
  chartRows,
  chartMarks,
} from './galleryData';

export function ResearchSection(): React.JSX.Element {
  const [selectedDate, setSelectedDate] = useState<string | undefined>(chartRows[67]?.date);
  const selectDate = useCallback((date: string) => setSelectedDate(date), []);
  const relativeRows = facts
    ? [
        { label: 'Target', kind: 'target' as const, value: facts.target_return_pct ?? -7.58 },
        { label: 'Market', kind: 'market' as const, value: facts.market?.return_pct ?? -0.07 },
        { label: 'Sector', kind: 'sector' as const, value: facts.sector?.return_pct ?? 0.39 },
        { label: 'Peers', kind: 'peers' as const, value: facts.peers?.median_return_pct ?? -3.29 },
      ]
    : [
        { label: 'Target', kind: 'target' as const, value: -7.58 },
        { label: 'Market', kind: 'market' as const, value: -0.07 },
        { label: 'Sector', kind: 'sector' as const, value: 0.39 },
        { label: 'Peers', kind: 'peers' as const, value: -3.29 },
      ];
  const mark = 'downgraded the stock from Buy to Neutral';
  return (
    <>
      <ExampleSection label="Evidence" name="Quote card">
        <div className="grid max-w-4xl gap-3">
          {source && (
            <>
              <QuoteCard
                claim={headline}
                quote={source.text ?? ''}
                highlight={mark}
                role={source.role === 'PRIMARY' ? 'Primary' : 'Secondary'}
                publisher={source.publisher ?? 'Source'}
                time={source.published_at ? formatEtTime(source.published_at) : '09:50 ET'}
                url={source.url}
              />
              <div className="type-caption text-ink-300">Fixture · page-a-answered-now-b5</div>
            </>
          )}
          <QuoteCard
            claim="A longer quotation demonstrates wrapping across several lines while preserving the full source wording and the highlighted match."
            quote="Investors reassessed the outlook after management described demand as broadening across enterprise customers, while also noting that purchase timing remained uneven. The company expects continued investment in infrastructure and product development through the next several quarters."
            highlight="purchase timing remained uneven"
            role="Secondary"
            publisher="Company filing"
            time="09:50 ET"
          />
        </div>
      </ExampleSection>
      <ExampleSection label="Pipeline" name="Stage stepper">
        <div className="grid max-w-3xl grid-cols-2 gap-4">
          <StageStepper
            stages={[
              {
                id: 'a',
                label: 'Retrieval complete',
                detail: '4 sources found',
                status: 'done',
                time: '1.1 s',
              },
              { id: 'b', label: 'Assessing evidence', detail: 'Reviewing source quality', status: 'active' },
              { id: 'c', label: 'Prepare answer', status: 'pending' },
            ]}
          />
          <div className="rounded-md border border-line-100 bg-bg-100 p-3">
            <StageStepper
              stages={[
                { id: 'a', label: 'Started', status: 'done' },
                { id: 'b', label: 'Retrieval failed', detail: 'Provider request failed', status: 'failed' },
                { id: 'c', label: 'Answer', status: 'pending' },
              ]}
            />
            <div className="type-caption text-ink-300">
              Fixture lifecycle · {running?.lifecycle ?? 'RUNNING'}
            </div>
          </div>
        </div>
      </ExampleSection>
      <ExampleSection label="Relative move" name="Relative bars">
        <div className="max-w-3xl rounded-md border border-line-100 bg-bg-100 p-4">
          <RelativeBars
            rows={relativeRows}
            excess={(facts?.target_return_pct ?? -7.58) - (facts?.sector?.return_pct ?? 0.39)}
          />
          {facts && <div className="type-caption mt-2 text-ink-300">Fixture · page-a-answered-now-b5</div>}
        </div>
      </ExampleSection>
      <ExampleSection label="Session" name="Timeline">
        <div className="max-w-4xl rounded-md border border-line-100 bg-bg-100 p-4">
          <SessionTimeline
            openAt={timelineOpenAt}
            closeAt={timelineCloseAt}
            cutoffAt={timelineCutoffAt}
            items={(answer?.sources ?? []).slice(0, 3).map((item) => ({
              at:
                timelineStart && item.published_at
                  ? Math.round(
                      (new Date(item.published_at).getTime() - new Date(timelineStart).getTime()) / 60000,
                    )
                  : 785,
              label: `${item.publisher ?? 'News'} evidence`,
              evidence: true,
            }))}
          />
        </div>
        <div className="type-caption text-ink-300">Fixture · page-a-answered-now-b5</div>
      </ExampleSection>
      <ExampleSection label="Market data" name="Price chart">
        <div className="gallery-chart rounded-md border border-line-100 bg-bg-100 p-3">
          <PriceChart
            rows={chartRows}
            selectableDates={chartMarks}
            selectedDate={selectedDate}
            onSelectDate={selectDate}
          />
        </div>
        <div className="type-caption text-ink-300">
          Selected date · <span className="font-numeric">{selectedDate}</span>
        </div>
      </ExampleSection>
    </>
  );
}
