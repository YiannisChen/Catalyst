import type React from 'react';
import { useState } from 'react';
import { Badge } from '@/components/ui';
import { Delta, Stat, NewsCard, StateNotice } from '@/components/catalyst';
import { formatEtTime, formatPct, formatSeconds, formatUsd } from '@/lib/format';
import { ExampleSection } from './ExampleSection';
import { facts, firstNews, coverNews, realNews, tierAnswer, hasPendingImages } from './galleryData';

export function CatalystSection(): React.JSX.Element {
  const [selectedNews, setSelectedNews] = useState(0);
  const relativeRows = facts
    ? [
        { label: 'Target', kind: 'target' as const, value: facts.target_return_pct ?? -7.58 },
        { label: 'Market', kind: 'market' as const, value: facts.market?.return_pct ?? -0.07 },
        { label: 'Sector', kind: 'sector' as const, value: facts.sector?.return_pct ?? 0.39 },
        { label: 'Peers', kind: 'peers' as const, value: facts.peers?.median_return_pct ?? -3.29 },
      ]
    : [
        { label: 'Target', kind: 'target' as const, value: -7.58 },
        { label: 'Market', kind: 'market' as const, value: -0.07 },
        { label: 'Sector', kind: 'sector' as const, value: 0.39 },
        { label: 'Peers', kind: 'peers' as const, value: -3.29 },
      ];
  return (
    <>
      <ExampleSection label="Returns" name="Delta">
        <div className="flex flex-wrap items-center gap-4">
          <Delta value={2.31} />
          <Delta value={-1.27} />
          <Delta value={0} />
          <Delta value={2.31} chip />
          <Delta value={-1.27} arrow={false} />
        </div>
      </ExampleSection>
      <ExampleSection label="States" name="State notice">
        <div className="grid grid-cols-2 gap-3">
          <StateNotice
            title="No headlines in this window"
            body="Nothing was published about NOW between 04-09 16:00 and 04-10 16:00 ET."
          />
          <StateNotice title="Headlines loading" body="Prices are ready; headlines appear as they load." />
          <StateNotice
            title="No step record for this run"
            body="This run was made before step records were kept. The answer is still valid; run it again to see each step."
            action={{ label: 'Run again', onClick: () => undefined }}
          />
          <StateNotice
            tone="warn"
            title="No prices for this ticker"
            body="XYZ is not in the covered tickers, so there is nothing to explain yet."
            action={{ label: 'Browse covered tickers', onClick: () => undefined }}
          />
          <StateNotice
            tone="warn"
            title="Free demo limit reached"
            body="You used today's free runs. Add your own key to keep going, or replay a featured case for free."
            action={{ label: 'Add a key', onClick: () => undefined }}
            secondaryAction={{ label: 'Featured cases', to: '/featured' }}
          />
          <StateNotice
            tone="down"
            title="Admin token needed"
            body="The data center needs the admin token. Viewing and running do not."
            action={{ label: 'Add token', to: '/settings#access' }}
          />
          <StateNotice
            tone="down"
            title="Can't reach the server"
            body="The last request failed. Catalyst retries every 10 s; your inputs are kept."
            action={{ label: 'Retry now', onClick: () => undefined }}
          />
          <StateNotice
            tone="down"
            title="Your key was rejected"
            body="Your key was rejected: Invalid API key. Nothing was charged."
            action={{ label: 'Edit key', onClick: () => undefined }}
          />
          <StateNotice
            tone="warn"
            title="Run stopped at the cost cap"
            body="It spent $0.196 of $0.20. Nothing was published."
            action={{ label: 'Retry with $0.40', onClick: () => undefined }}
          />
        </div>
      </ExampleSection>
      <ExampleSection label="Metrics" name="Stat">
        <div className="grid max-w-3xl grid-cols-2 gap-4 rounded-md border border-line-100 bg-bg-100 p-4">
          <Stat
            label="Session move"
            value={formatPct(facts?.target_return_pct ?? -7.58)}
            delta={facts?.target_return_pct ?? -7.58}
            hint="Answered session return"
          />
          <Stat
            size="sm"
            label="Provider cost"
            value={formatUsd(0.03925388)}
            delta={2.31}
            hint={formatSeconds(41.2)}
          />
        </div>
        <div className="flex items-center gap-2">
          <Badge tone="warn">{tierAnswer?.outcome === 'TIER_ONLY' ? 'Tier only' : 'Unavailable'}</Badge>
          <span className="type-caption text-ink-300">
            Fixture · {tierAnswer?.headline} ·{' '}
            <span className="font-numeric">{tierAnswer?.session_date}</span>
          </span>
        </div>
      </ExampleSection>
      <ExampleSection label="Research" name="News card">
        <div className="grid grid-cols-2 gap-3">
          {firstNews && (
            <>
              <div className="flex flex-col gap-2">
                <NewsCard
                  title={firstNews.title}
                  summary={firstNews.summary ?? undefined}
                  publisher={firstNews.publisher}
                  time={formatEtTime(firstNews.published_at)}
                  phase={firstNews.phase}
                  category={firstNews.category}
                  usedAsEvidence={firstNews.used_as_evidence}
                  imageUrl={firstNews.image_url ?? undefined}
                  selected={selectedNews === 0}
                  onClick={() => setSelectedNews(0)}
                />
                <div className="type-caption text-ink-300">Fixture · page-a-news-images-pending</div>
              </div>
              <div className="flex flex-col gap-2">
                {coverNews && (
                  <NewsCard
                    layout="cover"
                    title={coverNews.title}
                    summary={coverNews.summary ?? undefined}
                    publisher={coverNews.publisher}
                    time={formatEtTime(coverNews.published_at)}
                    phase={coverNews.phase}
                    category={coverNews.category}
                    imageUrl={coverNews.image_url ?? undefined}
                    usedAsEvidence
                  />
                )}
                <NewsCard
                  layout="cover"
                  title="No image available for this source"
                  publisher="Yahoo"
                  time="09:50 ET"
                />
                <NewsCard
                  layout="cover"
                  title="Image backfill pending"
                  publisher="Yahoo"
                  time="09:50 ET"
                  imagePending={hasPendingImages}
                />
              </div>
            </>
          )}
          {realNews.slice(1, 3).map((item, index) => (
            <NewsCard
              key={item.title}
              title={item.title}
              summary={item.summary ?? undefined}
              publisher={item.publisher}
              time={formatEtTime(item.published_at)}
              phase={item.phase}
              imageUrl={item.image_url ?? undefined}
              selected={selectedNews === index + 1}
              usedAsEvidence={item.used_as_evidence}
              onClick={() => setSelectedNews(index + 1)}
            />
          ))}
        </div>
      </ExampleSection>
    </>
  );
}
