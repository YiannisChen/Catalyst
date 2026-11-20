import type { JSX } from 'react';
import type { components } from '@/api/schema';
import { formatBytes } from './format';
import { formatEtDateTime } from '@/lib/format';

type Store = components['schemas']['StoreInfo'];
const names: Record<string, string> = {
  market_db: 'Market prices',
  bodies_db: 'News bodies',
  jobs_db: 'Update jobs',
  archive_db: 'News archive',
  web_search_db: 'Web search cache',
  corp_actions_db: 'Corporate actions',
  news_categories_db: 'News categories',
  media_db: 'Images',
  sec_submissions_dir: 'SEC filings',
  sec_earnings_cache: 'Earnings cache',
  served_db: 'Search index',
};
function updated(value: string | null): string {
  if (!value) return '—';
  return formatEtDateTime(value);
}
export function StoresTable({
  stores,
  searchIndex,
}: {
  stores: Store[];
  searchIndex: components['schemas']['SearchIndexInfo'];
}): JSX.Element {
  const allStores = [
    ...stores,
    { name: 'Search index', path_kind: searchIndex.path_kind, bytes: searchIndex.bytes, modified_at: null },
  ];
  return (
    <section className="flex min-w-0 shrink-0 flex-col overflow-hidden rounded-lg border border-line-100 bg-bg-100">
      <header className="panel-header flex items-center border-b border-line-100 px-4">
        <h2 className="type-heading m-0">Stores</h2>
      </header>
      <div className="overflow-x-auto">
        <table className="cx-table w-full">
          <thead>
            <tr>
              <th>Name</th>
              <th>Size</th>
              <th>Updated (ET)</th>
            </tr>
          </thead>
          <tbody>
            {allStores.map((store) => (
              <tr key={store.path_kind}>
                <td>{names[store.path_kind] ?? 'Data store'}</td>
                <td className="font-numeric">{formatBytes(store.bytes)}</td>
                <td className="text-ink-200">{updated(store.modified_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
