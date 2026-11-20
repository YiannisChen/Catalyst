import type { JSX } from 'react';
import { Chip, Input } from '@/components/ui';

export type HistoryFilter = '' | 'ANSWERED' | 'REPORTED' | 'NO_CAUSE' | 'ABSTAINED' | 'TIER_ONLY' | 'FAILED';

const filters: Array<{ label: string; value: HistoryFilter }> = [
  { label: 'All', value: '' },
  { label: 'Confirmed', value: 'ANSWERED' },
  { label: 'Reported', value: 'REPORTED' },
  { label: 'No clear cause', value: 'NO_CAUSE' },
  { label: 'Ordinary move', value: 'TIER_ONLY' },
  { label: 'Failed', value: 'FAILED' },
];

export function HistoryFilters({
  ticker,
  outcome,
  onTickerChange,
  onTickerSubmit,
  onOutcomeChange,
}: {
  ticker: string;
  outcome: HistoryFilter;
  onTickerChange: (value: string) => void;
  onTickerSubmit: () => void;
  onOutcomeChange: (value: HistoryFilter) => void;
}): JSX.Element {
  return (
    <div className="flex flex-wrap items-center gap-2 border-b border-line-100 px-4 py-3">
      <Input
        aria-label="Filter by ticker"
        className="history-ticker-filter"
        mono
        placeholder="Filter by ticker"
        value={ticker}
        onChange={(event) => onTickerChange(event.currentTarget.value.toUpperCase())}
        onKeyDown={(event) => {
          if (event.key === 'Enter') onTickerSubmit();
        }}
      />
      {filters.map((filter) => (
        <Chip
          key={filter.value || 'all'}
          label={filter.label}
          selected={outcome === filter.value}
          onClick={() => onOutcomeChange(filter.value)}
        />
      ))}
    </div>
  );
}
