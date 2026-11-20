import type React from 'react';
import { Link } from 'react-router';
import { Badge } from '@/components/ui';
import { useFeaturedList } from '@/api/queries';
import { productTierOutcome, type HistoryOutcome } from '@/features/history/outcome';
import { formatDay, formatPct } from '@/lib/format';

export type DaySuggestion = { date: string; movePct: number };

export type FirstVisitProps = {
  ticker: string;
  /** The ticker's biggest explainable moves, largest first. */
  suggestions: DaySuggestion[];
  onPickDay: (date: string) => void;
};

/** What a visitor sees before any answer exists: the question, three steps, and two ways to start. */
export function FirstVisit({ ticker, suggestions, onPickDay }: FirstVisitProps): React.JSX.Element {
  const featured = useFeaturedList();
  const groups = new Map<string, { label: string; tone: HistoryOutcome['tone']; count: number }>();
  for (const item of featured.data?.items ?? []) {
    const outcome = productTierOutcome(item.product_tier);
    if (!outcome) continue;
    const group = groups.get(outcome.label) ?? { label: outcome.label, tone: outcome.tone, count: 0 };
    group.count += 1;
    groups.set(outcome.label, group);
  }
  return (
    <div className="first-visit">
      <div className="flex flex-col gap-2">
        <h2 className="type-title m-0">Why did {ticker} move?</h2>
        <p className="m-0 type-body-sm text-ink-200">
          Pick a day on the chart. Catalyst reads that day&apos;s news and filings up to the close, quotes the
          cause, and checks it against the sector and peers.
        </p>
      </div>
      <ol className="first-visit-steps">
        <li>
          <b>1</b>
          <span>
            <span className="text-ink-100">Pick a marked day</span> · the dots under the chart moved 2% or
            more
          </span>
        </li>
        <li>
          <b>2</b>
          <span>
            <span className="text-ink-100">Read its news</span> · any day, marked or not
          </span>
        </li>
        <li>
          <b>3</b>
          <span>
            <span className="text-ink-100">Explain it</span> · about 20 s, the answer quotes its source
          </span>
        </li>
      </ol>
      {suggestions.length > 0 && (
        <section className="flex flex-col gap-2">
          <h3 className="type-label m-0 text-ink-300">Try one of its biggest moves</h3>
          {suggestions.map((day) => (
            <button
              key={day.date}
              type="button"
              className="first-visit-day"
              onClick={() => onPickDay(day.date)}
            >
              <span className={`font-numeric ${day.movePct < 0 ? 'text-down' : 'text-up'}`}>
                {formatPct(day.movePct)}
              </span>
              <span>{formatDay(day.date)}</span>
              <span aria-hidden="true" className="text-ink-300">
                →
              </span>
            </button>
          ))}
        </section>
      )}
      {groups.size > 0 && (
        <section className="first-visit-featured">
          <h3 className="type-label m-0 text-ink-300">Or see finished examples</h3>
          {[...groups.values()].map((group) => (
            <div key={group.label} className="first-visit-result">
              <span>{group.label}</span>
              <Badge tone={group.tone}>
                {group.count} {group.count === 1 ? 'case' : 'cases'}
              </Badge>
            </div>
          ))}
          <Link className="first-visit-link" to="/featured">
            Open featured cases · free
          </Link>
        </section>
      )}
    </div>
  );
}
