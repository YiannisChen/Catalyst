import type { JSX, KeyboardEvent } from 'react';
import type { components } from '@/api/schema';
import { Badge, Button, Skeleton } from '@/components/ui';
import { Delta } from '@/components/catalyst/Delta';
import { cleanListingTags, formatDay, formatEtDateTime, formatSeconds } from '@/lib/format';
import { costLabel, costTitle, failureLabel } from '@/lib/run';
import { historyOutcome } from './outcome';

type RunHistoryItem = components['schemas']['RunHistoryItem'];

function openRow(event: KeyboardEvent<HTMLTableRowElement>, onOpen: () => void) {
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault();
    onOpen();
  }
}

export function HistoryTable({
  items,
  loading = false,
  onOpen,
}: {
  items: RunHistoryItem[];
  loading?: boolean;
  onOpen: (item: RunHistoryItem) => void;
}): JSX.Element {
  return (
    <div className="overflow-x-auto">
      <table className="history-table">
        <thead>
          <tr>
            <th>Ticker</th>
            <th>Session</th>
            <th>Result</th>
            <th className="text-right">Move</th>
            <th>Cause</th>
            <th>Model</th>
            <th className="text-right">Time</th>
            <th className="text-right">Cost</th>
            <th>Started (ET)</th>
          </tr>
        </thead>
        <tbody>
          {loading
            ? Array.from({ length: 10 }, (_, index) => (
                <tr key={index} aria-hidden="true">
                  {Array.from({ length: 9 }, (_, cell) => (
                    <td key={cell}>
                      <Skeleton height={12} />
                    </td>
                  ))}
                </tr>
              ))
            : items.map((item) => {
                const result = historyOutcome(item);
                const stopped = item.lifecycle_status === 'FAILED' || item.lifecycle_status === 'CANCELLED';
                const rawCauseText = stopped
                  ? failureLabel(item.lifecycle_status, item.failure_code)
                  : (item.cause ?? item.headline);
                const repeatsMove = Boolean(
                  rawCauseText &&
                  item.ticker &&
                  new RegExp(
                    `^${item.ticker}\\s+(?:rose|fell|gained|dropped)\\s+\\d+(?:\\.\\d+)?%\\.?$`,
                    'i',
                  ).test(rawCauseText),
                );
                const causeText = repeatsMove || !rawCauseText ? null : cleanListingTags(rawCauseText);
                return (
                  <tr
                    key={`${item.run_id}-${item.lifecycle_status}`}
                    tabIndex={0}
                    onClick={() => onOpen(item)}
                    onKeyDown={(event) => openRow(event, () => onOpen(item))}
                    aria-label={`Open ${item.ticker ?? 'run'} from ${item.session_date ?? 'unknown date'}`}
                  >
                    <td className="font-medium">{item.ticker ?? '—'}</td>
                    <td className="text-ink-200">
                      {item.session_date ? formatDay(item.session_date, true) : '—'}
                    </td>
                    <td>
                      <Badge tone={result.tone}>{result.label}</Badge>
                    </td>
                    <td className="text-right">
                      {item.move_pct == null ? '—' : <Delta value={item.move_pct} />}
                    </td>
                    <td
                      className={`history-headline ${stopped || repeatsMove ? 'text-ink-300' : 'text-ink-200'}`}
                      title={causeText ?? undefined}
                    >
                      {causeText ?? (repeatsMove ? 'No cause quoted' : '—')}
                    </td>
                    <td className="text-caption text-ink-200" title={item.model ?? undefined}>
                      {/* "provider/model": the model name alone leaves room for the cause */}
                      {item.model?.split('/').at(-1) ?? '—'}
                    </td>
                    <td className="font-numeric text-right text-ink-200">
                      {formatSeconds(item.duration_ms / 1000)}
                    </td>
                    <td className="font-numeric text-right text-ink-200" title={costTitle(item.cost_method)}>
                      {costLabel(item.cost_usd, item.cost_method, 3)}
                    </td>
                    <td className="text-caption text-ink-300">
                      {formatEtDateTime(item.created_at).replace(/ ET$/, '')}
                    </td>
                  </tr>
                );
              })}
        </tbody>
      </table>
    </div>
  );
}

export function HistoryPaging({
  count,
  offset,
  total,
  canPrevious,
  canNext,
  onPrevious,
  onNext,
}: {
  count: number;
  offset: number; // rows on earlier pages
  total: number | null; // runs matching every filter, when the API reports it
  canPrevious: boolean;
  canNext: boolean;
  onPrevious: () => void;
  onNext: () => void;
}): JSX.Element {
  return (
    <div className="flex items-center justify-between gap-2 border-t border-line-100 px-4 py-3 text-caption text-ink-300">
      <span className="font-numeric">
        {total != null && count > 0
          ? `Showing ${offset + 1}–${offset + count} of ${total}`
          : `Showing ${count} runs`}
      </span>
      <div className="flex gap-2">
        <Button variant="ghost" size="sm" disabled={!canPrevious} onClick={onPrevious}>
          Previous
        </Button>
        <Button variant="ghost" size="sm" disabled={!canNext} onClick={onNext}>
          Next
        </Button>
      </div>
    </div>
  );
}
