import type { JSX } from 'react';
import { Panel } from '@/components/ui';
import { fmtBytes, fmtTime, storeDescription } from '@/features/data/model';
import type { Overview } from '@/features/data/model';
export function Stores({ overview, stale }: { overview: Overview; stale: boolean }): JSX.Element {
  const stores = [
    ...overview.stores,
    {
      name: 'Search index',
      path_kind: 'search_index',
      bytes: overview.search_index.bytes,
      modified_at: null,
    },
  ];
  const order = [
    'market_db',
    'archive_db',
    'bodies_db',
    'news_categories_db',
    'media_db',
    'sec_submissions_dir',
    'search_index',
    'served_db',
    'web_search_db',
    'jobs_db',
    'sec_earnings_cache',
    'corp_actions_db',
  ];
  const list = [...stores].sort((a, b) => {
    const ai = order.indexOf(a.path_kind);
    const bi = order.indexOf(b.path_kind);
    return (ai < 0 ? 99 : ai) - (bi < 0 ? 99 : bi);
  });
  const groups = new Set<string>();
  return (
    <Panel
      className="h-auto shrink-0"
      flush
      title={
        <>
          <h2 className="type-heading m-0">3 · What is stored</h2>
          <span className="type-caption text-ink-300">
            {overview.stores.length} stores ·{' '}
            {fmtBytes(overview.stores.reduce((sum, store) => sum + (store.bytes ?? 0), 0))} plus the{' '}
            {fmtBytes(overview.search_index.bytes)} search index
          </span>
        </>
      }
      bodyClassName="gap-0 p-0"
    >
      <div className="overflow-x-auto p-4">
        <table className="cx-table w-full">
          <thead>
            <tr>
              <th>Store</th>
              <th>Holds</th>
              <th>Used for</th>
              <th className="text-right">Size</th>
              <th className="text-right">Updated</th>
            </tr>
          </thead>
          <tbody>
            {list.map((store) => {
              const desc = storeDescription(
                store.path_kind === 'search_index' ? 'Search index' : store.path_kind,
                overview,
                102,
              );
              const first = !groups.has(desc.group);
              groups.add(desc.group);
              const staleArchive = store.path_kind === 'archive_db' && stale;
              const updated =
                store.path_kind === 'search_index'
                  ? overview.search_index.built
                    ? 'built'
                    : 'not created'
                  : store.modified_at
                    ? fmtTime(store.modified_at)
                    : 'not created';
              return (
                <tr key={store.path_kind}>
                  <td>
                    <span className="type-label block text-ink-300">{first ? desc.group : ''}</span>
                    {desc.label}
                  </td>
                  <td className="text-ink-200">{desc.holds}</td>
                  <td className="text-ink-200">{desc.use}</td>
                  <td className="text-right font-numeric text-code">{fmtBytes(store.bytes)}</td>
                  <td className={`text-right text-code ${staleArchive ? 'text-warn' : 'text-ink-200'}`}>
                    {updated}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}
