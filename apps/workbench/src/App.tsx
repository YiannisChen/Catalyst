import { useEffect, useMemo, useState } from 'react'

import { ContextBar } from './components/context/ContextBar'
import { useBootstrapData } from './hooks/useBootstrapData'
import './styles.css'

export default function App() {
  const bootstrap = useBootstrapData()
  const [ticker, setTicker] = useState('')
  const [tradeDate, setTradeDate] = useState('')
  const [query, setQuery] = useState('')

  useEffect(() => {
    if (bootstrap.status !== 'ready' && bootstrap.status !== 'empty') return

    const { range, tickers } = bootstrap.data
    const nextTicker = tickers.symbols.includes(ticker) ? ticker : (tickers.symbols[0] ?? '')
    const nextTradeDate =
      tradeDate &&
      (!range.min_date || tradeDate >= range.min_date) &&
      (!range.max_date || tradeDate <= range.max_date)
        ? tradeDate
        : (range.max_date ?? range.min_date ?? '')

    if (nextTicker !== ticker) setTicker(nextTicker)
    if (nextTradeDate !== tradeDate) setTradeDate(nextTradeDate)
  }, [bootstrap, ticker, tradeDate])

  const shellTitle = useMemo(() => {
    const selectedTicker = ticker || 'No ticker'
    const selectedDate = tradeDate || 'No date'

    return `${selectedTicker} · ${selectedDate}`
  }, [ticker, tradeDate])

  return (
    <div className="app-shell">
      <ContextBar
        bootstrap={bootstrap}
        ticker={ticker}
        tradeDate={tradeDate}
        query={query}
        onTickerChange={setTicker}
        onTradeDateChange={setTradeDate}
        onQueryChange={setQuery}
      />

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
