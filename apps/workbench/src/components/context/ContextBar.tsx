import type { BootstrapState } from '../../hooks/useBootstrapData';

interface ContextBarProps {
  bootstrap: BootstrapState;
  ticker: string;
  tradeDate: string;
  query: string;
  onTickerChange: (ticker: string) => void;
  onTradeDateChange: (tradeDate: string) => void;
  onQueryChange: (query: string) => void;
}

export function ContextBar({
  bootstrap,
  ticker,
  tradeDate,
  query,
  onTickerChange,
  onTradeDateChange,
  onQueryChange,
}: ContextBarProps) {
  if (bootstrap.status === 'loading') {
    return (
      <header className="toolbar" aria-busy="true">
        <span className="status-chip">Loading market context</span>
      </header>
    );
  }

  if (bootstrap.status === 'error') {
    return (
      <header className="toolbar" role="status">
        <span className="status-chip status-chip-error">Context unavailable</span>
        <span>{bootstrap.message}</span>
      </header>
    );
  }

  const { health, range, tickers } = bootstrap.data;
  const hasTickers = bootstrap.status === 'ready';

  return (
    <header className="toolbar">
      {bootstrap.status === 'empty' ? (
        <div className="field context-message">
          <span className="status-chip status-chip-muted">No tickers available</span>
          <span>Local OHLCV range is empty.</span>
        </div>
      ) : null}

      <div className="field">
        <label htmlFor="ticker">Ticker</label>
        <select
          id="ticker"
          value={ticker}
          disabled={!hasTickers}
          onChange={(event) => onTickerChange(event.target.value)}
        >
          {tickers.symbols.map((symbol) => (
            <option key={symbol} value={symbol}>
              {symbol}
            </option>
          ))}
        </select>
      </div>

      <div className="field">
        <label htmlFor="trade-date">Date</label>
        <input
          id="trade-date"
          type="date"
          value={tradeDate}
          min={range.min_date ?? undefined}
          max={range.max_date ?? undefined}
          onChange={(event) => onTradeDateChange(event.target.value)}
        />
      </div>

      <div className="field">
        <label htmlFor="query">Query</label>
        <input
          id="query"
          type="text"
          placeholder="Explain the market move"
          value={query}
          onChange={(event) => onQueryChange(event.target.value)}
        />
      </div>

      <div className="field runtime-health" role="status" aria-label="runtime health">
        <span>Runtime health</span>
        <strong className={`status-chip status-chip-${health.status}`}>{health.status}</strong>
      </div>
    </header>
  );
}
