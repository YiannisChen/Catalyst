import type React from 'react';
import { useMemo, useState } from 'react';
import { useNavigate } from 'react-router';
import { useFeaturedList, useQuota } from '@/api/queries';
import type { components } from '@/api/schema';
import { Badge, Button, Chip } from '@/components/ui';
import { StateNotice } from '@/components/catalyst';
import { productTierOutcome } from '@/features/history/outcome';
import { getProviderKeys } from '@/lib/storage';
import { cleanListingTags, formatDay } from '@/lib/format';

type FeaturedItem = components['schemas']['FeaturedItemDTO'];
const categoryNames: Record<string, string> = {
  ANSWERED_REPORTED_ATTRIBUTION: 'Confirmed',
  ANSWERED_GROUP_DRIVER: 'Confirmed',
  ANSWERED_UNIQUE_COMPANY_EVENT: 'Confirmed',
  ANSWERED_EARNINGS_FALLBACK: 'Confirmed',
  ABSTAINED_NO_CAUSE: 'No clear cause',
  ABSTAINED_VERIFIER_REJECTED: 'No clear cause',
  REPORTED_REASON: 'Reported',
  MOVED_WITH_GROUP: 'With its group',
  PLACEBO_DAY: 'Ordinary move',
};
function categoryLabel(value: string): string {
  return (
    categoryNames[value] ??
    value
      .toLowerCase()
      .replaceAll('_', ' ')
      .replace(/\b\w/g, (letter) => letter.toUpperCase())
  );
}
function outcome(value: string | null | undefined): {
  label: string;
  tone: 'up' | 'warn' | 'down' | 'neutral' | 'info';
} {
  switch (value) {
    case 'ANSWERED':
      return { label: 'Confirmed', tone: 'up' };
    case 'ABSTAINED':
      return { label: 'No clear cause', tone: 'warn' };
    case 'TIER_ONLY':
      return { label: 'Ordinary move', tone: 'neutral' };
    case 'PARTIAL':
      return { label: 'Reported', tone: 'info' };
    case 'FAILED':
      return { label: 'Failed', tone: 'down' };
    default:
      return {
        label:
          value
            ?.toLowerCase()
            .replaceAll('_', ' ')
            .replace(/\b\w/g, (letter) => letter.toUpperCase()) ?? 'Completed',
        tone: 'neutral',
      };
  }
}
function CaseCard({ item, onOpen }: { item: FeaturedItem; onOpen: () => void }): React.JSX.Element {
  const result = productTierOutcome(item.product_tier) ?? outcome(item.outcome);
  return (
    <article
      role="link"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          onOpen();
        }
      }}
      className="flex min-w-0 cursor-pointer flex-col gap-3 rounded-md border border-line-100 bg-bg-100 p-4 hover:border-line-200"
    >
      <div className="flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-baseline gap-2">
          <span className="text-body font-medium text-ink-100">{item.ticker ?? '—'}</span>
          <span className="type-caption text-ink-300">
            {item.session_date ? formatDay(item.session_date, true) : '—'}
          </span>
        </div>
        <Badge tone={result.tone}>{result.label}</Badge>
      </div>
      <div className="flex flex-col gap-1">
        <h2 className="type-heading m-0">{item.title}</h2>
        <p className="type-body-sm m-0 text-ink-200">{cleanListingTags(item.summary)}</p>
      </div>
      <div className="mt-auto flex flex-wrap items-center gap-2 border-t border-line-100 pt-3">
        <Button
          className="ml-auto"
          size="sm"
          onClick={(event) => {
            event.stopPropagation();
            onOpen();
          }}
        >
          Replay
        </Button>
      </div>
    </article>
  );
}
export default function Featured(): React.JSX.Element {
  const list = useFeaturedList();
  const quota = useQuota();
  const navigate = useNavigate();
  const [category, setCategory] = useState('');
  const showDemo = quota.data?.tier === 'anonymous' && Object.keys(getProviderKeys()).length === 0;
  const freeLimit = quota.data?.per_caller?.limit ?? 5;
  const items = useMemo(
    () => (list.data?.items ?? []).filter((item) => !category || item.category === category),
    [list.data, category],
  );
  const open = (item: FeaturedItem) => {
    if (item.ticker && item.session_date)
      navigate(
        `/?ticker=${encodeURIComponent(item.ticker)}&date=${encodeURIComponent(item.session_date)}&case=${encodeURIComponent(item.case_id)}`,
      );
  };
  return (
    <main className="mx-auto flex h-full w-full max-w-7xl flex-col gap-6 overflow-auto px-5 py-6">
      {showDemo && (
        <section className="flex flex-wrap items-center gap-4 rounded-md border border-accent bg-accent-soft p-4">
          <div className="flex min-w-0 flex-1 flex-col gap-1">
            <strong className="type-body">You are viewing the demo</strong>
            <p className="type-body-sm m-0 text-ink-200">
              {freeLimit === 0
                ? 'Catalyst explains why a stock moved on a given day, quoting only news published before the close. Replay any case below for free. To explain other days, add your own model API key.'
                : `Catalyst explains why a stock moved on a given day, quoting only news published before the close. Replays below are free; ${freeLimit} live runs a day are on us.`}
            </p>
          </div>
          <Button variant="primary" onClick={() => navigate(freeLimit === 0 ? '/settings' : '/')}>
            {freeLimit === 0 ? 'Add an API key' : 'Try a live run'}
          </Button>
        </section>
      )}
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div className="flex max-w-4xl flex-col gap-2">
          <h1 className="type-title m-0">Featured cases</h1>
          <p className="type-body m-0 text-ink-200">
            Saved runs that show each kind of result. Open one to replay its steps in the workbench; replays
            cost nothing.
          </p>
        </div>
        {!list.isLoading && !list.isError && list.data && list.data.categories.length > 0 && (
          <div className="flex flex-wrap gap-2">
            <Chip label="All" selected={!category} onClick={() => setCategory('')} />
            {list.data.categories.map((value) => (
              <Chip
                key={value}
                label={categoryLabel(value)}
                selected={category === value}
                onClick={() => setCategory(category === value ? '' : value)}
              />
            ))}
          </div>
        )}
      </header>
      {list.isLoading ? (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
          {Array.from({ length: 6 }, (_, index) => (
            <div key={index} className="featured-card-skeleton skeleton-shimmer animate-shimmer rounded-md" />
          ))}
        </div>
      ) : list.isError ? (
        <div className="flex flex-col gap-2">
          <p className="type-body-sm m-0 text-warn">Could not load featured cases.</p>
          <Button className="w-fit" onClick={() => void list.refetch()}>
            Retry
          </Button>
        </div>
      ) : items.length === 0 ? (
        <div className="flex justify-center py-6">
          <div className="w-full max-w-xl">
            <StateNotice
              title="No featured cases yet"
              body="Featured cases are hand-picked saved runs that show each kind of result. Until some are published, replay your own runs from History."
              action={{ label: 'Open History', onClick: () => navigate('/history') }}
            />
          </div>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
          {items.map((item) => (
            <CaseCard key={item.case_id} item={item} onOpen={() => open(item)} />
          ))}
        </div>
      )}
    </main>
  );
}
