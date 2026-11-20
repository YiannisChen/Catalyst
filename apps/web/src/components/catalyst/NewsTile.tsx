import { useState } from 'react';
import { ExternalLink } from 'lucide-react';
import { cn } from '@/lib/cn';

export type NewsTileProps = {
  title: string;
  summary?: string;
  publisher: string;
  time: string;
  phase: 'PRE_OPEN' | 'SESSION';
  category?: string;
  url?: string | null;
  imageUrl?: string;
  usedAsEvidence?: boolean;
  aboutSubject?: boolean;
  selected?: boolean;
  onClick?: () => void;
};

export function NewsTile({
  title,
  summary,
  publisher,
  time,
  phase,
  category,
  url,
  imageUrl,
  usedAsEvidence,
  aboutSubject,
  selected,
  onClick,
}: NewsTileProps) {
  const [imageFailed, setImageFailed] = useState(false);
  const content = (
    <span className="news-tile-content">
      <span className="news-tile-copy">
        <span className="news-tile-meta type-caption text-ink-300">
          <span className="font-numeric">{time}</span>
          <span>·</span>
          <span>{publisher}</span>
          {usedAsEvidence && <span className="text-accent">· Used as evidence</span>}
        </span>
        <span className="news-tile-title type-body font-medium text-ink-100">
          {title}
          {url && <ExternalLink size={14} className="ml-1 inline text-ink-300" />}
        </span>
        {summary && <span className="news-tile-summary type-body-sm text-ink-200">{summary}</span>}
      </span>
      {imageUrl && !imageFailed && (
        <span className="news-tile-media">
          <img
            src={imageUrl}
            alt=""
            loading="lazy"
            onError={() => setImageFailed(true)}
            className="size-full object-cover"
          />
        </span>
      )}
      <span className="sr-only">
        {phase === 'PRE_OPEN' ? 'Before the open' : 'During the session'}
        {category ? ` · ${category}` : ''}
        {aboutSubject ? ' · About this ticker' : ''}
      </span>
    </span>
  );
  const className = cn(
    'news-tile flex h-full items-center gap-3 overflow-hidden rounded-md border border-line-100 bg-bg-200 p-3 hover:bg-bg-300',
    usedAsEvidence && 'news-tile-evidence',
    !usedAsEvidence && aboutSubject && 'news-tile-about',
    selected && 'news-tile-selected',
  );
  return url ? (
    <a
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      aria-current={selected || undefined}
      onClick={onClick}
      className={className}
    >
      {content}
    </a>
  ) : (
    <div aria-current={selected || undefined} onClick={onClick} className={className}>
      {content}
    </div>
  );
}
