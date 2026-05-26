import { useMemo, useState } from 'react'

import './styles.css'

function todayIsoDate(): string {
  return new Date().toISOString().slice(0, 10)
}

export default function App() {
  const [ticker, setTicker] = useState('AAPL')
  const [tradeDate, setTradeDate] = useState(todayIsoDate())
  const [query, setQuery] = useState('')

  const shellTitle = useMemo(() => `${ticker} · ${tradeDate}`, [ticker, tradeDate])

  return (
    <div className="app-shell">
      <header className="toolbar">
        <div className="field">
          <label htmlFor="ticker">Ticker</label>
          <select id="ticker" value={ticker} onChange={(event) => setTicker(event.target.value)}>
            <option value="AAPL">AAPL</option>
            <option value="MSFT">MSFT</option>
            <option value="NVDA">NVDA</option>
          </select>
        </div>

        <div className="field">
          <label htmlFor="trade-date">Date</label>
          <input
            id="trade-date"
            type="date"
            value={tradeDate}
            onChange={(event) => setTradeDate(event.target.value)}
          />
        </div>

        <div className="field">
          <label htmlFor="query">Query</label>
          <input
            id="query"
            type="text"
            placeholder="Explain the market move"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </div>
      </header>

      <main className="content-grid">
        <section className="panel" aria-label="chart-panel">
          <h2>Workbench Chart · {shellTitle}</h2>
          <div className="placeholder">K-line panel placeholder</div>
        </section>

        <aside className="panel" aria-label="runtime-console-panel">
          <h2>Runtime Console Placeholder</h2>
          <div className="console-block">
            <h3>Status</h3>
            <ul className="console-list">
              <li>run_id: -</li>
              <li>status: QUEUED / RUNNING / TERMINAL</li>
            </ul>
          </div>
          <div className="console-block">
            <h3>Events</h3>
            <ul className="console-list">
              <li>event_seq, node, status_before, status_after</li>
            </ul>
          </div>
          <div className="console-block">
            <h3>Artifacts</h3>
            <ul className="console-list">
              <li>artifact_type, event_seq, payload</li>
            </ul>
          </div>
        </aside>
      </main>
    </div>
  )
}
