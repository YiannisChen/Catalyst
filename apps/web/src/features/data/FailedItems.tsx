import { plainError } from './format';
import type { JSX } from 'react';
import type { components } from '@/api/schema';
import { Badge } from '@/components/ui';

type Failure = components['schemas']['FailedJobItem'];
const stageLabels: Record<string, string> = {
  prices: 'Prices',
  news: 'News',
  sec: 'Filings',
  movers: 'Movers',
  verify: 'Checks',
};
function stageLabel(stage: string): string {
  return (
    stageLabels[stage] ?? stage.replace(/[_-]+/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase())
  );
}
export function FailedItems({ items }: { items: Failure[] }): JSX.Element | null {
  if (!items.length) return null;
  return (
    <section className="flex min-w-0 shrink-0 flex-col overflow-hidden rounded-lg border border-line-100 bg-bg-100">
      <header className="panel-header flex items-center justify-between border-b border-line-100 px-4">
        <h2 className="type-heading m-0">Failed items</h2>
        <Badge tone="warn">{items.length}</Badge>
      </header>
      <div className="overflow-x-auto">
        <table className="cx-table w-full">
          <thead>
            <tr>
              <th>Stage</th>
              <th>Item</th>
              <th>Session</th>
              <th>Attempts</th>
              <th>Error</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item, index) => (
              <tr key={`${item.stage}-${item.item}-${index}`}>
                <td>
                  <span className="text-body-sm">{stageLabel(item.stage)}</span>
                </td>
                <td>{item.item}</td>
                <td className="font-numeric">{item.session ?? '—'}</td>
                <td className="font-numeric">{item.attempts}</td>
                <td className="data-failure-cell">
                  <span className="block whitespace-normal break-words text-ink-200">
                    {plainError(item.error)}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
