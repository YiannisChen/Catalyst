import type { ReactNode } from 'react';
import { Badge } from '@/components/ui';
export interface QuoteCardProps {
  claim?: string;
  quote: string;
  highlight?: string;
  role?: string;
  publisher: string;
  time: string;
  url?: string;
  className?: string;
}
export function QuoteCard({
  claim,
  quote,
  highlight,
  role,
  publisher,
  time,
  url,
  className,
}: QuoteCardProps) {
  let content: ReactNode = quote;
  const start = highlight ? quote.indexOf(highlight) : -1;
  if (start >= 0 && highlight)
    content = (
      <>
        {quote.slice(0, start)}
        <mark className="quote-highlight-padding rounded-sm bg-accent-soft text-accent">{highlight}</mark>
        {quote.slice(start + highlight.length)}
      </>
    );
  return (
    <figure
      className={`flex flex-col gap-2 rounded-md border border-line-100 bg-bg-200 p-3 ${className ?? ''}`}
    >
      {claim && <div className="type-body">{claim}</div>}
      <blockquote className="m-0 rounded-sm bg-bg-100 px-3 py-2 text-body-sm text-ink-200">
        {content}
      </blockquote>
      <figcaption className="flex flex-wrap items-center gap-2 text-caption text-ink-300">
        {role && <Badge tone={role === 'Primary' ? 'accent' : 'neutral'}>{role}</Badge>}
        <span>{publisher}</span>
        <span className="font-numeric">{time}</span>
        {url && (
          <a
            className="text-info no-underline hover:underline"
            href={url}
            target="_blank"
            rel="noopener noreferrer"
          >
            Open source
          </a>
        )}
      </figcaption>
    </figure>
  );
}
