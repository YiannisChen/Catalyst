import { useState } from 'react';
import { cn } from '@/lib/cn';
import { Badge } from '@/components/ui';
export interface NewsCardProps {
  title: string;
  summary?: string;
  publisher: string;
  time: string;
  phase?: 'PRE_OPEN' | 'SESSION';
  category?:
    | 'EARNINGS'
    | 'ANALYST'
    | 'DEALS'
    | 'PRODUCT'
    | 'REGULATORY_LEGAL'
    | 'MANAGEMENT'
    | 'CAPITAL'
    | 'MARKET'
    | 'OPINION'
    | 'OTHER';
  usedAsEvidence?: boolean;
  imageUrl?: string;
  imagePending?: boolean;
  layout?: 'row' | 'cover';
  selected?: boolean;
  onClick?: () => void;
  className?: string;
}
const categories: Record<string, string> = { REGULATORY_LEGAL: 'Regulatory' };
function Cover({
  imageUrl,
  publisher,
  pending,
}: {
  imageUrl?: string;
  publisher: string;
  pending?: boolean;
}) {
  const [failed, setFailed] = useState(false);
  return (
    <div
      className={cn(
        'news-cover relative overflow-hidden rounded-sm bg-bg-300',
        pending && 'news-cover-pending animate-shimmer skeleton-shimmer',
      )}
    >
      <span className="news-cover-fallback absolute inset-0 flex items-end bg-gradient-to-br from-bg-300 to-bg-200 p-3 text-body-sm font-semibold text-ink-300">
        {publisher}
      </span>
      {imageUrl && !failed && !pending && (
        <img
          src={imageUrl}
          alt=""
          onError={() => setFailed(true)}
          className="absolute inset-0 size-full object-cover"
        />
      )}
    </div>
  );
}
function Thumbnail({ imageUrl, publisher }: { imageUrl: string; publisher: string }) {
  const [failed, setFailed] = useState(false);
  return failed ? (
    <span className="news-thumb flex items-end rounded-sm bg-bg-300 p-2 text-caption text-ink-300">
      {publisher}
    </span>
  ) : (
    <img
      src={imageUrl}
      alt=""
      onError={() => setFailed(true)}
      className="news-thumb rounded-sm bg-bg-300 object-cover"
    />
  );
}
export function NewsCard({
  title,
  summary,
  publisher,
  time,
  phase,
  category,
  usedAsEvidence,
  imageUrl,
  imagePending,
  layout = 'row',
  selected,
  onClick,
  className,
}: NewsCardProps) {
  const cover = layout === 'cover';
  const meta = (
    <div className="flex flex-wrap items-center gap-2 text-caption text-ink-300">
      <span className="font-medium text-ink-200">{publisher}</span>
      <span className="font-numeric">{time}</span>
      {phase && <Badge>{phase === 'PRE_OPEN' ? 'Pre-open' : 'Session'}</Badge>}
      {category && <Badge>{categories[category] ?? category[0] + category.slice(1).toLowerCase()}</Badge>}
      {usedAsEvidence && (
        <Badge tone="accent" dot>
          Evidence
        </Badge>
      )}
    </div>
  );
  return (
    <article
      role="button"
      tabIndex={0}
      aria-current={selected ? 'true' : 'false'}
      onClick={onClick}
      onKeyDown={(event) => {
        if ((event.key === 'Enter' || event.key === ' ') && onClick) {
          event.preventDefault();
          onClick();
        }
      }}
      className={cn(
        'news-row cursor-pointer gap-3 rounded-md border border-line-100 bg-bg-200 p-3 hover:border-line-200 hover:bg-bg-300 aria-[current=true]:border-accent',
        cover && 'flex-col gap-2 p-2',
        className,
      )}
    >
      {cover && <Cover imageUrl={imageUrl} publisher={publisher} pending={imagePending} />}
      <div className={cn('news-row-main flex min-w-0 flex-col gap-2', cover && 'px-1 pb-1')}>
        <div className="line-clamp-2 text-body-sm font-medium text-ink-100">{title}</div>
        {summary && (
          <div className={cn('text-caption text-ink-200', !cover && 'line-clamp-2')}>{summary}</div>
        )}
        {meta}
      </div>
      {!cover && imageUrl && <Thumbnail imageUrl={imageUrl} publisher={publisher} />}
    </article>
  );
}
