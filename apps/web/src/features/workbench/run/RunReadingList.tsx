import type React from 'react';
import type { RunReadingItem, RunReadState } from './progress';

export function formatReadingTime(value: string): string {
  const normalized = value.includes('T') ? value : value.replace(' ', 'T').replace(/ UTC$/, 'Z');
  const date = new Date(normalized);
  if (!Number.isFinite(date.getTime())) return '';
  return `${new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', hour12: true }).format(date).replace(',', '')}`;
}

const readLabels: Record<RunReadState, { label: string; tone: string }> = {
  states_cause: { label: 'States a cause', tone: 'text-up' },
  reports_event: { label: 'Reports an event', tone: 'text-info' },
  no_cause: { label: 'No cause', tone: 'text-ink-300' },
  not_about_company: { label: 'Not about this company', tone: 'text-ink-300' },
  failed: { label: 'Could not read', tone: 'text-ink-300' },
};
const readOrder: Record<RunReadState, number> = {
  states_cause: 0,
  reports_event: 1,
  no_cause: 2,
  not_about_company: 3,
  failed: 4,
};

/** The articles the run is reading; each one updates as soon as it has been read. */
export function ReadingList({
  items,
  readCount,
}: {
  items: RunReadingItem[];
  readCount: number;
}): React.JSX.Element {
  const causes = items.filter((item) => item.read === 'states_cause').length;
  // Articles that state a cause or report an event come first, then the rest in arrival order.
  const ordered = [...items].sort(
    (a, b) => (a.read === null ? 9 : readOrder[a.read]) - (b.read === null ? 9 : readOrder[b.read]),
  );
  const shown = ordered.slice(0, 6);
  return (
    <div className="run-reading">
      <div className="run-reading-heading">
        <span className="type-label text-ink-300">
          Reading · {Math.min(readCount, items.length)} of {items.length} done · ET
        </span>
        {causes > 0 && <span className="type-caption text-ink-300">{causes} states a cause</span>}
      </div>
      <ul className="m-0 list-none p-0 type-caption">
        {shown.map((item, index) => (
          <li key={`${index}-${item.evidenceId ?? item.url ?? item.publisher}`} className="run-reading-row">
            {item.url ? (
              <a
                className="run-reading-publisher text-info hover:underline"
                href={item.url}
                target="_blank"
                rel="noopener noreferrer"
              >
                {item.publisher}
              </a>
            ) : (
              <span className="run-reading-publisher text-ink-200">{item.publisher}</span>
            )}
            <span className="run-reading-time text-ink-300">
              {item.publishedAt ? formatReadingTime(item.publishedAt) : ''}
            </span>
            <span
              className={`run-reading-state ${item.read === 'states_cause' ? 'text-accent' : item.read ? readLabels[item.read].tone : 'text-ink-300'}`}
            >
              {item.read ? readLabels[item.read].label : 'Reading…'}
            </span>
          </li>
        ))}
        {items.length > shown.length && (
          <li className="type-caption text-ink-300">and {items.length - shown.length} more</li>
        )}
      </ul>
    </div>
  );
}
