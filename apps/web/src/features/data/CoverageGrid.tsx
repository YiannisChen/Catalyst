import type React from 'react';
import { useState, type JSX } from 'react';
import type { CoverageRow } from './coverage';

export function CoverageGrid({ rows }: { rows: CoverageRow[] }): JSX.Element {
  const [showAll, setShowAll] = useState(false);
  const visible = showAll ? rows : rows.slice(0, 12);
  return (
    <section className="flex min-w-0 shrink-0 flex-col overflow-hidden rounded-lg border border-line-100 bg-bg-100">
      <header className="panel-header flex flex-wrap items-center justify-between gap-3 border-b border-line-100 px-4">
        <div className="flex items-center gap-2">
          <h2 className="type-heading m-0">News coverage</h2>
          <span className="text-caption text-ink-300">
            last <span className="font-numeric">{rows[0]?.cells.length ?? 0}</span> sessions ·{' '}
            <span className="font-numeric">{rows.length}</span> tickers
          </span>
        </div>
        <div className="flex flex-wrap items-center gap-3 text-caption text-ink-300">
          <span className="inline-flex items-center gap-1">
            <i className="coverage-legend-full" />
            Full
          </span>
          <span className="inline-flex items-center gap-1">
            <i className="coverage-legend-thin" />
            Thin
          </span>
          <span className="inline-flex items-center gap-1">
            <i className="coverage-legend-gap" />
            Gap
          </span>
          {rows.length > 12 && (
            <button
              type="button"
              className="text-accent hover:text-ink-100"
              onClick={() => setShowAll((value) => !value)}
            >
              {showAll ? 'Show fewer' : `Show all ${rows.length}`}
            </button>
          )}
        </div>
      </header>
      <div className="flex flex-col gap-1 overflow-x-auto p-4">
        {visible.map((row) => (
          <div
            key={row.ticker}
            className="coverage-row items-center gap-1"
            style={{ '--coverage-columns': row.cells.length } as React.CSSProperties}
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
    </section>
  );
}
