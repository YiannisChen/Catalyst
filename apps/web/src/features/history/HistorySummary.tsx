import type { JSX } from 'react';
import type { components } from '@/api/schema';
import { Stat } from '@/components/catalyst/Stat';

type Summary = components['schemas']['RunHistorySummary'];

export function HistorySummary({ summary }: { summary: Summary }): JSX.Element {
  return (
    <div className="grid grid-cols-3 gap-6">
      <Stat label="Runs" value={summary.runs} size="sm" />
      <Stat label="Confirmed" value={summary.answered} size="sm" />
      <Stat label="Spent" value={`$${summary.spent_usd.toFixed(2)}`} size="sm" />
    </div>
  );
}
