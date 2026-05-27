import { useEffect, useMemo, useState } from 'react'

import { createLiveRun } from './api/client'
import type { RuntimeStatus } from './api/types'
import { SelectedDayContext } from './components/attribution/SelectedDayContext'
import { KLinePanel } from './components/chart/KLinePanel'
import { ContextBar } from './components/context/ContextBar'
import { useBootstrapData } from './hooks/useBootstrapData'
import './styles.css'

export default function App() {
  const bootstrap = useBootstrapData()
  const [ticker, setTicker] = useState('')
  const [tradeDate, setTradeDate] = useState('')
  const [query, setQuery] = useState('')
  const [runStatus, setRunStatus] = useState<RuntimeStatus | null>(null)
  const [activeRunId, setActiveRunId] = useState<string | null>(null)
  const [isCreatingRun, setIsCreatingRun] = useState(false)
  const [createRunError, setCreateRunError] = useState<string | null>(null)

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

  const chartRange =
    bootstrap.status === 'ready' || bootstrap.status === 'empty'
      ? {
          startDate: bootstrap.data.range.min_date,
          endDate: bootstrap.data.range.max_date,
        }
      : { startDate: null, endDate: null }

  async function handleRunAttribution() {
    if (!ticker || !tradeDate || isCreatingRun) return

    setIsCreatingRun(true)
    setCreateRunError(null)

    try {
      const response = await createLiveRun({
        ticker,
        trade_date: tradeDate,
        query,
      })
      setActiveRunId(response.run_id)
      setRunStatus(response.status)
    } catch (error) {
      setCreateRunError(error instanceof Error ? error.message : 'Unable to create attribution run.')
    } finally {
      setIsCreatingRun(false)
    }
  }

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
        <div>
          <KLinePanel ticker={ticker} startDate={chartRange.startDate} endDate={chartRange.endDate} />
          <p className="chart-context-title">Workbench Chart · {shellTitle}</p>
          <SelectedDayContext
            ticker={ticker}
            tradeDate={tradeDate}
            query={query}
            runStatus={runStatus}
            isCreatingRun={isCreatingRun}
            createRunError={createRunError}
            onRunAttribution={handleRunAttribution}
          />
        </div>

        <aside className="panel" aria-label="runtime-console-panel">
          <h2>Runtime Console Placeholder</h2>
          <div className="console-block">
            <h3>Status</h3>
            <ul className="console-list">
              <li>run_id: {activeRunId ?? '-'}</li>
              <li>status: {runStatus ?? 'No run'}</li>
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
