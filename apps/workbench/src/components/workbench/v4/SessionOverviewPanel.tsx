import type { DemoCase } from '../../../mock/demoCases';
import { getSelectedDayMetrics } from '../workbench-model';

interface Props {
  demoCase: DemoCase;
  selectedDate: string | null;
  canRun: boolean;
}

const compactNumber = new Intl.NumberFormat('en-US', {
  notation: 'compact',
  maximumFractionDigits: 1,
});

function severityLabel(pct: number): string {
  const abs = Math.abs(pct);
  if (abs >= 4) return 'Large move';
  if (abs >= 2) return 'Moderate move';
  if (abs >= 0.8) return 'Small move';
  return 'Flat';
}

export default function SessionOverviewPanel({ demoCase, selectedDate, canRun }: Props) {
  const metrics = getSelectedDayMetrics(demoCase, selectedDate);

  return (
    <div className="v4-session-summary">
      <h2 className="v4-panel-title">
        {metrics ? `${demoCase.ticker} ${selectedDate}` : 'No date selected'}
      </h2>

      {!metrics ? (
        <div className="v4-empty-state">
          <span className="v4-empty-text">Select a candle to inspect session context.</span>
        </div>
      ) : (
        <div className="v4-session-body">
          <div className="v4-price-move">
            <span className="v4-price-label">Close move</span>
            <span className={`v4-price-value ${metrics.closeMove >= 0 ? 'is-up' : 'is-down'}`}>
              {metrics.closeMove >= 0 ? '+' : ''}{metrics.closeMove.toFixed(2)}%
            </span>
            <span className="v4-severity-label">{severityLabel(metrics.closeMove)}</span>
          </div>

          
          <dl className="v4-metrics-grid">
            <div><dt>Open</dt><dd>${metrics.open.toFixed(2)}</dd></div>
            <div><dt>High</dt><dd>${metrics.high.toFixed(2)}</dd></div>
            <div><dt>Low</dt><dd>${metrics.low.toFixed(2)}</dd></div>
            <div><dt>Close</dt><dd>${metrics.close.toFixed(2)}</dd></div>
            <div><dt>Volume</dt><dd>{compactNumber.format(metrics.volume)}</dd></div>
            <div><dt>Range</dt><dd>${metrics.intradayRange.toFixed(2)}</dd></div>
          </dl>

          {/* Compact status rows */}
          <div className="v4-session-status-rows">
            <div className="v4-session-status-row">
              <span className="v4-session-status-label">Attribution readiness</span>
              <span className={`v4-session-status-value ${canRun ? 'is-ready' : 'is-muted'}`}>
                {canRun ? 'Ready' : 'Select date'}
              </span>
            </div>
            <div className="v4-session-status-row">
              <span className="v4-session-status-label">Event window</span>
              <span className="v4-session-status-value">{metrics.eventWindow}</span>
            </div>
            <div className="v4-session-status-row">
              <span className="v4-session-status-label">Move severity</span>
              <span className="v4-session-status-value">{severityLabel(metrics.closeMove)}</span>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
