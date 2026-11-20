import type React from 'react';
import { memo, useEffect, useMemo, useState } from 'react';
import { Button, Chip, Panel as Surface, Skeleton } from '@/components/ui';
import { NewsTile, SessionTimeline, StateNotice } from '@/components/catalyst';
import type { components } from '@/api/schema';
import { decodeEntities, formatEtDateTime } from '@/lib/format';

type News = components['schemas']['SessionNewsResponse'];
type NewsItem = News['items'][number];
const noItems: NewsItem[] = [];
function rankNews(a: NewsItem, b: NewsItem): number {
  return (
    Number(Boolean(b.used_as_evidence)) - Number(Boolean(a.used_as_evidence)) ||
    Number(Boolean(b.mentions_subject)) - Number(Boolean(a.mentions_subject)) ||
    b.published_at.localeCompare(a.published_at)
  );
}
function formatEtNewsTime(value: string): string {
  const date = new Date(value);
  return `${new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    hour12: true,
  }).format(date)} ET`;
}
type NewsQuery = {
  data: News | undefined;
  isLoading: boolean;
  isError: boolean;
  refetch: () => Promise<unknown>;
};
export type NewsPanelProps = {
  date: string;
  ticker: string;
  pricesReady: boolean;
  news: NewsQuery;
  selectedNewsId?: string | null;
  onSelectNews?: (id: string) => void;
};
// Memoized: the workbench re-renders on every chart hover, and hundreds of cards make that slow.
export const NewsPanel = memo(function NewsPanel(props: NewsPanelProps): React.JSX.Element {
  return <NewsPanelContent key={`${props.ticker}:${props.date}`} {...props} />;
});

function NewsPanelContent({
  date,
  ticker,
  pricesReady,
  news,
  selectedNewsId,
  onSelectNews,
}: NewsPanelProps): React.JSX.Element {
  const [filter, setFilter] = useState('All');
  const [mode, setMode] = useState<'Key' | 'All'>('Key');
  const [internalSelectedNews, setInternalSelectedNews] = useState<string>();
  const newsItems = news.data?.items ?? noItems;
  const selectedNews = selectedNewsId ?? internalSelectedNews;
  const rankedNews = useMemo(() => [...newsItems].sort(rankNews), [newsItems]);
  const keyNews = useMemo(() => {
    const keyItems = rankedNews.filter((item) => item.used_as_evidence || item.mentions_subject);
    return keyItems.length < 4
      ? [...keyItems, ...rankedNews.filter((item) => !item.used_as_evidence && !item.mentions_subject)].slice(
          0,
          12,
        )
      : keyItems.slice(0, 12);
  }, [rankedNews]);
  const selectNews = (id: string) => {
    setInternalSelectedNews(id);
    onSelectNews?.(id);
    requestAnimationFrame(() =>
      document.getElementById(`news-${id}`)?.scrollIntoView({ block: 'center', behavior: 'smooth' }),
    );
  };
  const filteredNews = useMemo(
    () => (filter === 'All' ? rankedNews : rankedNews.filter((item) => item.category === filter)),
    [rankedNews, filter],
  );
  // Memoized: the page re-renders on every chart hover, and a new list would re-run the scroll effect.
  const visibleItems = useMemo(() => {
    if (mode !== 'Key') return filteredNews;
    const key =
      filter === 'All'
        ? keyNews
        : filteredNews.filter((item) => item.used_as_evidence || item.mentions_subject).slice(0, 12);
    // Key news with a cover image first (otherwise in the same order), so the list reads tidier.
    return [...key.filter((item) => item.image_url), ...key.filter((item) => !item.image_url)];
  }, [mode, filter, keyNews, filteredNews]);
  useEffect(() => {
    if (!selectedNewsId) return;
    if (!newsItems.some((item) => item.id === selectedNewsId)) return;
    if (!visibleItems.some((item) => item.id === selectedNewsId)) {
      setFilter('All');
      setMode('All');
      return;
    }
    const frame = requestAnimationFrame(() =>
      document
        .getElementById(`news-${selectedNewsId}`)
        ?.scrollIntoView({ block: 'center', behavior: 'smooth' }),
    );
    return () => cancelAnimationFrame(frame);
  }, [selectedNewsId, visibleItems, newsItems]);
  const categories = Object.fromEntries(
    Object.entries(news.data?.counts_by_category ?? {}).filter(
      ([key, count]) => count > 0 && key !== 'UNCATEGORIZED',
    ),
  );
  const startDate = news.data?.window.information_window_start_at;
  const openDate = news.data?.window.session_open_at;
  const cutoffDate = news.data?.window.cutoff_at;
  const openAt =
    startDate && openDate ? Math.round((Date.parse(openDate) - Date.parse(startDate)) / 60000) : 810;
  const cutoffAt =
    startDate && cutoffDate
      ? Math.round((Date.parse(cutoffDate) - Date.parse(startDate)) / 60000)
      : openAt + 390;
  return (
    <Surface
      title={
        <>
          News
          {date && (
            <span className="type-caption text-ink-300">
              {startDate && cutoffDate
                ? `${formatEtDateTime(startDate)} → ${formatEtDateTime(cutoffDate)} · ${newsItems.length} headlines`
                : ''}
            </span>
          )}
        </>
      }
      className="h-auto shrink-0 overflow-visible"
      bodyClassName="flex-none overflow-visible"
    >
      <div className="flex flex-col">
        {!date ? (
          <p className="type-body-sm text-ink-300">Pick a session to see its news</p>
        ) : news.isLoading ? (
          <div className="flex flex-col gap-3">
            {pricesReady && (
              <p className="m-0 type-caption text-ink-300">
                Prices are ready; headlines appear as they load.
              </p>
            )}
            {[1, 2, 3, 4].map((n) => (
              <Skeleton key={n} height={64} />
            ))}
          </div>
        ) : news.isError ? (
          <div className="flex items-center gap-2 type-body-sm text-ink-300">
            Could not load news.
            <Button size="sm" onClick={() => void news.refetch()}>
              Retry
            </Button>
          </div>
        ) : newsItems.length === 0 ? (
          <StateNotice
            title="No headlines in this window"
            body={`Nothing was published about ${ticker} between ${startDate ? formatEtDateTime(startDate) : 'the window start'} and ${cutoffDate ? formatEtDateTime(cutoffDate) : 'the cutoff'}.`}
          />
        ) : (
          <>
            <div className="mb-2">
              <SessionTimeline
                openAt={openAt}
                closeAt={openAt + Math.max(1, cutoffAt - openAt)}
                cutoffAt={cutoffAt}
                labels={[startDate ? formatEtDateTime(startDate) : 'Window start', '09:30 open', 'Cutoff']}
                legend={false}
                title="When the news came out"
                items={newsItems.map((item) => ({
                  at: Math.max(
                    0,
                    Math.round(
                      (Date.parse(item.published_at) - Date.parse(startDate ?? item.published_at)) / 60000,
                    ),
                  ),
                  label: item.title,
                  evidence: item.used_as_evidence,
                  selected: selectedNews === item.id,
                  onClick: () => {
                    selectNews(item.id);
                  },
                }))}
              />
            </div>
            <div className="sticky top-0 z-10 flex flex-col gap-2 border-b border-line-100 bg-bg-100 pb-2 pt-2">
              <div className="flex flex-wrap gap-2">
                <Chip
                  label="Key"
                  count={keyNews.length}
                  selected={mode === 'Key' && filter === 'All'}
                  onClick={() => {
                    setFilter('All');
                    setMode('Key');
                  }}
                />
                <Chip
                  label="All"
                  count={newsItems.length}
                  selected={mode === 'All' && filter === 'All'}
                  onClick={() => {
                    setFilter('All');
                    setMode('All');
                  }}
                />
                {Object.entries(categories).map(([category, count]) => (
                  <Chip
                    key={category}
                    label={category
                      .replaceAll('_', ' ')
                      .toLowerCase()
                      .replace(/\b\w/g, (char) => char.toUpperCase())}
                    count={count}
                    selected={filter === category}
                    onClick={() => {
                      setFilter(category);
                      setMode('All');
                    }}
                  />
                ))}
              </div>
            </div>
            <div className="flex flex-col gap-5 pt-3">
              {(['PRE_OPEN', 'SESSION'] as const).map((phase) => {
                const phaseItems = visibleItems.filter((item) => item.phase === phase);
                return phaseItems.length ? (
                  <section key={phase} className="flex flex-col gap-2">
                    <div className="news-phase-head">
                      <span className="type-heading text-ink-100">
                        {phase === 'PRE_OPEN' ? 'Before the open' : 'During the session'}
                      </span>
                      <span className="type-body-sm text-ink-300">
                        {phaseItems.length} {phaseItems.length === 1 ? 'headline' : 'headlines'} ·{' '}
                        {phase === 'PRE_OPEN' ? 'until 09:30 ET' : 'from 09:30 ET to the cutoff'}
                      </span>
                      <span className="news-phase-rule" />
                    </div>
                    <div className="news-grid">
                      {phaseItems.map((item) => (
                        <div id={`news-${item.id}`} key={item.id} className="min-w-0">
                          <NewsTile
                            title={decodeEntities(item.title)}
                            summary={item.summary ? decodeEntities(item.summary) : undefined}
                            publisher={item.publisher}
                            time={formatEtNewsTime(item.published_at)}
                            phase={item.phase}
                            category={item.category ?? undefined}
                            url={item.url}
                            aboutSubject={item.mentions_subject}
                            usedAsEvidence={item.used_as_evidence}
                            imageUrl={item.image_url ?? undefined}
                            selected={selectedNews === item.id}
                            onClick={() => selectNews(item.id)}
                          />
                        </div>
                      ))}
                    </div>
                  </section>
                ) : null;
              })}
            </div>
            <Button
              variant="ghost"
              size="sm"
              className="w-full"
              onClick={() => setMode(mode === 'Key' ? 'All' : 'Key')}
            >
              {mode === 'Key' ? `Show all ${filteredNews.length} headlines` : 'Show key headlines only'}
            </Button>
          </>
        )}
      </div>
    </Surface>
  );
}
