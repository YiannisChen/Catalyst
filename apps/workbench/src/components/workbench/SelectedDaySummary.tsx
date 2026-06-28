import type { DemoCase } from '../../mock/demoCases';
import { getSelectedDayMetrics } from './workbench-model';

interface Props {
  demoCase: DemoCase;
  selectedDate: string | null;
}

const compactNumber = new Intl.NumberFormat('en-US', {
  notation: 'compact',
  maximumFractionDigits: 1,
});

export default function SelectedDaySummary({ demoCase, selectedDate }: Props) {
  const metrics = getSelectedDayMetrics(demoCase, selectedDate);

  return (
    <aside className="wb-card wb-day-summary" aria-labelledby="selected-day-title">
      <div className="wb-card-header">
        <div>
          <span className="wb-card-kicker">Trade Day Snapshot</span>
          <h2 id="selected-day-title">{metrics ? `${demoCase.ticker} ${metrics.date}` : 'No date selected'}</h2>
          {metrics && <p className="wb-day-context">{demoCase.companyName} · Selected market session</p>}
        </div>
      </div>

      {!metrics ? (
        <div className="wb-day-empty">Select a candle to inspect its event window.</div>
      ) : (
        <div className="wb-day-body">
          <dl className="wb-day-primary">
            <div>
              <dt>Close move</dt>
              <dd className={metrics.closeMove >= 0 ? 'is-positive' : 'is-negative'}>
                {metrics.closeMove >= 0 ? '+' : ''}{metrics.closeMove.toFixed(2)}%
              </dd>
            </div>
            <div className="wb-close-context">
              <dt>Close</dt>
              <dd>${metrics.close.toFixed(2)}</dd>
            </div>
          </dl>
          <dl className="wb-day-metrics">
            <div><dt>Open</dt><dd>${metrics.open.toFixed(2)}</dd></div>
            <div><dt>High</dt><dd>${metrics.high.toFixed(2)}</dd></div>
            <div><dt>Low</dt><dd>${metrics.low.toFixed(2)}</dd></div>
            <div><dt>Volume</dt><dd>{compactNumber.format(metrics.volume)}</dd></div>
            <div><dt>Intraday range</dt><dd>${metrics.intradayRange.toFixed(2)}</dd></div>
          </dl>
          <dl className="wb-day-footer">
            <div className="wb-day-window"><dt>Event window</dt><dd>{metrics.eventWindow}</dd></div>
          </dl>
        </div>
      )}
    </aside>
  );
}
