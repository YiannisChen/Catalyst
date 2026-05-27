import type { RuntimeStatus } from '../../api/types';

interface SelectedDayContextProps {
  ticker: string;
  tradeDate: string;
  query: string;
  runStatus: RuntimeStatus | null;
  isCreatingRun: boolean;
  createRunError: string | null;
  onRunAttribution: () => void;
}

const ACTIVE_RUN_STATUSES: RuntimeStatus[] = ['QUEUED', 'RUNNING'];

export function SelectedDayContext({
  ticker,
  tradeDate,
  query,
  runStatus,
  isCreatingRun,
  createRunError,
  onRunAttribution,
}: SelectedDayContextProps) {
  const hasRequiredContext = ticker.length > 0 && tradeDate.length > 0;
  const isSameRunActive = runStatus !== null && ACTIVE_RUN_STATUSES.includes(runStatus);
  const isRunDisabled = !hasRequiredContext || isCreatingRun || isSameRunActive;

  return (
    <section className="panel selected-day-context" aria-label="selected day context">
      <div className="selected-day-header">
        <div>
          <h2>Selected Day Context</h2>
          <p>
            {ticker || 'No ticker'} · {tradeDate || 'No trade date'}
          </p>
        </div>
        <button className="primary-action" type="button" disabled={isRunDisabled} onClick={onRunAttribution}>
          Run Attribution
        </button>
      </div>

      <dl className="selected-day-grid">
        <div>
          <dt>Ticker</dt>
          <dd>{ticker || '-'}</dd>
        </div>
        <div>
          <dt>Trade date</dt>
          <dd>{tradeDate || '-'}</dd>
        </div>
        <div>
          <dt>Status</dt>
          <dd>
            <span className="status-chip">{runStatus ?? 'No run'}</span>
          </dd>
        </div>
      </dl>

      {query ? <p className="selected-day-query">Query: {query}</p> : null}
      {!hasRequiredContext ? <p className="inline-feedback">Select a ticker and trade date first.</p> : null}
      {createRunError ? <p className="inline-feedback inline-feedback-error">{createRunError}</p> : null}
    </section>
  );
}
