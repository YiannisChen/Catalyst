import type { CSSProperties, JSX } from 'react';
import { useState } from 'react';
import { Button, Panel } from '@/components/ui';
import { fmtDate, stageCounts, stageDone } from '@/features/data/model';
import type { LatestJob, Overview } from '@/features/data/model';
import type { buildCoverage } from '@/features/data/coverage';
import { pct } from './dataPage';
export function CoverageSection({
  overview,
  rows,
  tickers,
  job,
}: {
  overview: Overview;
  rows: ReturnType<typeof buildCoverage>;
  tickers: string[];
  job: LatestJob | undefined;
}): JSX.Element {
  const [showGrid, setShowGrid] = useState(false);
  const sessions = [...overview.coverage].sort((a, b) => a.session_date.localeCompare(b.session_date));
  const maxPrice =
    overview.freshness.price_symbols ||
    Math.max(1, ...sessions.map((session) => session.tickers_with_prices.length));
  const maxNews = Math.max(1, ...sessions.map((session) => session.tickers_with_news.length));
  const lastNewsIndex = sessions.reduce(
    (last, session, index) => (session.tickers_with_news.length ? index : last),
    -1,
  );
  const priceCount = sessions.reduce((sum, session) => sum + session.tickers_with_prices.length, 0);
  const pricesPct = pct(priceCount, maxPrice * sessions.length);
  const newsSessions = sessions.filter((session) => session.tickers_with_news.length);
  const weakest = [...rows]
    .sort((a, b) => a.newsDays - b.newsDays || a.ticker.localeCompare(b.ticker))
    .slice(0, 8);
  const secCount = job?.plan_summary?.universe_size ?? tickers.length;
  const lastHeadlineDate = overview.freshness.headlines_through;
  const avgNews = newsSessions.length
    ? Math.round(
        newsSessions.reduce((n, session) => n + session.tickers_with_news.length, 0) / newsSessions.length,
      )
    : 0;
  return (
    <Panel
      className="h-auto shrink-0"
      flush
      title={
        <>
          <h2 className="type-heading m-0">2 · Coverage</h2>
          <span className="type-caption text-ink-300">
            Last {sessions.length} sessions · {tickers.length} tickers in the universe
          </span>
        </>
      }
      bodyClassName="gap-4 p-4"
    >
      <div className="grid grid-cols-1 gap-2 md:grid-cols-3">
        <div className="data-coverage-stat">
          <span className="type-label text-ink-300">Prices</span>
          <span className="font-numeric text-num-lg">{pricesPct.toFixed(0)}%</span>
          <span className="type-caption text-ink-200">
            {maxPrice} symbols on all {sessions.length} sessions (tickers, peers, sector ETFs)
          </span>
        </div>
        <div className="data-coverage-stat">
          <span className="type-label text-ink-300">Company headlines</span>
          <span
            className={`font-numeric text-num-lg ${newsSessions.length < sessions.length ? 'text-warn' : ''}`}
          >
            {newsSessions.length} / {sessions.length} sessions
          </span>
          <span className="type-caption text-ink-200">
            {sessions.length - newsSessions.length
              ? `About ${avgNews} symbols a day until ${fmtDate(lastHeadlineDate ?? newsSessions.at(-1)?.session_date)}, then none`
              : 'Headlines found across all covered sessions'}
          </span>
        </div>
        <div className="data-coverage-stat">
          <span className="type-label text-ink-300">Filings (8-K)</span>
          <span className="font-numeric text-num-lg">{secCount} tickers</span>
          <span className="type-caption text-ink-200">
            Checked through{' '}
            {fmtDate(job?.recent_items.find((item) => item.stage === 'sec' && item.finished_at)?.finished_at)}
            ; this round reached {stageDone(stageCounts(job, 'sec'))} of {secCount}
          </span>
        </div>
      </div>
      <div className="flex flex-col gap-2">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <span className="text-body-sm font-semibold">Symbols with company headlines, per session</span>
          <span className="type-caption text-ink-300">
            Bar = symbols that had at least one headline before the close · dot = prices complete
          </span>
        </div>
        <div className="data-coverage-chart">
          {sessions.map((session, index) => (
            <div
              key={session.session_date}
              className="data-coverage-bar"
              title={`${session.session_date}: ${session.tickers_with_news.length} symbols`}
              style={{
                height: `${session.tickers_with_news.length ? Math.max(7, (session.tickers_with_news.length / maxNews) * 112) : 4}px`,
                background: session.tickers_with_news.length ? 'var(--data-peers)' : 'var(--down-soft)',
              }}
            />
          ))}
          {lastNewsIndex >= 0 && lastNewsIndex < sessions.length - 1 && (
            <div
              className="data-coverage-marker"
              style={{ left: `${((lastNewsIndex + 0.5) / sessions.length) * 100}%` }}
            >
              <span>
                Headline archive ends {fmtDate(sessions[lastNewsIndex]?.session_date)} ·{' '}
                {sessions.length - lastNewsIndex - 1} sessions without headlines
              </span>
            </div>
          )}
        </div>
        <div className="data-coverage-dots">
          {sessions.map((session) => (
            <span
              key={session.session_date}
              title={`${session.session_date}: ${session.tickers_with_prices.length}/${maxPrice} symbols`}
              className={session.tickers_with_prices.length >= maxPrice ? 'bg-up' : 'bg-bg-300'}
            />
          ))}
        </div>
        <div className="flex justify-between text-code text-ink-300">
          {[0, 0.25, 0.5, 0.75, 1].map((position) => (
            <span key={position}>
              {fmtDate(
                sessions[Math.min(sessions.length - 1, Math.round((sessions.length - 1) * position))]
                  ?.session_date,
              )}
            </span>
          ))}
        </div>
      </div>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div className="flex flex-col gap-2">
          <span className="text-body-sm font-semibold">Thinnest headline coverage</span>
          <span className="type-caption text-ink-300">
            Sessions with headlines, of the {newsSessions.length} sessions the archive covers
          </span>
          {weakest.map((row) => (
            <div key={row.ticker} className="data-thin-row">
              <span className="text-body-sm">{row.ticker}</span>
              <div className="data-thin-track">
                <span style={{ width: `${pct(row.newsDays, newsSessions.length)}%` }} />
              </div>
              <span className="text-caption text-ink-200">
                {row.newsDays}/{newsSessions.length}
              </span>
            </div>
          ))}
        </div>
        <div className="flex flex-col gap-2">
          <span className="text-body-sm font-semibold">How to read coverage</span>
          <p className="type-body-sm m-0 text-ink-200">
            An analysis can only use what was published before the close. A session with no headlines for a
            ticker usually ends in “No clear cause”, so gaps here explain weak answers.
          </p>
          <p className="type-body-sm m-0 text-ink-200">
            Prices are always fetched for every symbol; headlines and article text are fetched for movers
            (moves of {job?.plan_summary?.mover_threshold_pct ?? 2}% or more) and their peers.
          </p>
          <Button className="w-fit" onClick={() => setShowGrid((value) => !value)}>
            {showGrid ? 'Hide the full ticker × session grid' : 'Show the full ticker × session grid'}
          </Button>
        </div>
      </div>
      {showGrid && (
        <div className="flex flex-col gap-1 overflow-x-auto border-t border-line-100 pt-3">
          {rows.map((row) => (
            <div
              key={row.ticker}
              className="coverage-row items-center gap-1"
              style={{ '--coverage-columns': row.cells.length } as CSSProperties}
            >
              <span className="text-caption text-ink-200">{row.ticker}</span>
              {row.cells.map((cell) => (
                <span
                  key={`${row.ticker}-${cell.date}`}
                  title={`${row.ticker} ${cell.date}: ${cell.state}`}
                  className={`coverage-cell coverage-${cell.state.toLowerCase()}`}
                />
              ))}
            </div>
          ))}
        </div>
      )}
    </Panel>
  );
}
