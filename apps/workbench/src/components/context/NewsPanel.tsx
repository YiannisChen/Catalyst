import { useState, useEffect } from 'react';
import { getNews } from '../../api/client';
import type { NewsItem } from '../../api/client';

interface Props {
  ticker: string;
  tradeDate: string;
}

/** Derive a sentiment hint from snippet keywords (lightweight heuristic). */
function guessSentiment(snippet: string, title: string): 'positive' | 'negative' | 'neutral' {
  const text = (title + ' ' + snippet).toLowerCase();
  const up = ['surge', 'beat', 'record', 'growth', 'rally', 'gain', 'upgrade', 'strong', 'bullish', 'outperform', 'positive', 'profit'];
  const down = ['drop', 'fall', 'miss', 'decline', 'loss', 'cut', 'downgrade', 'weak', 'bearish', 'slump', 'warning', 'negative', 'crash'];
  let score = 0;
  up.forEach((w) => { if (text.includes(w)) score++; });
  down.forEach((w) => { if (text.includes(w)) score--; });
  if (score > 0) return 'positive';
  if (score < 0) return 'negative';
  return 'neutral';
}

export default function NewsPanel({ ticker, tradeDate }: Props) {
  const [items, setItems] = useState<NewsItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  useEffect(() => {
    if (!ticker || !tradeDate) return;
    setLoading(true);
    setError(null);

    getNews(ticker, tradeDate, 5)
      .then((res) => {
        setItems(res.items);
        setExpandedId(null);
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, [ticker, tradeDate]);

  if (loading) {
    return (
      <div className="np">
        <div className="np-state">
          <span className="loading-pulse" />
          Loading news...
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="np">
        <div className="np-state np-error">Failed to load news: {error}</div>
      </div>
    );
  }

  if (items.length === 0) {
    return (
      <div className="np">
        <div className="np-state">No news found near {tradeDate}</div>
      </div>
    );
  }

  return (
    <div className="np">
      <div className="np-header">
        <span className="np-title">Market News</span>
        <span className="np-count">{items.length} articles</span>
      </div>
      <div className="np-grid">
        {items.map((item) => {
          const isExpanded = expandedId === item.asset_id;
          const sentiment = guessSentiment(item.snippet, item.title);

          // Source name extraction
          const sourceName = item.source_line
            .replace(/^Source:\s*/i, '')
            .split('|')[0]
            .trim();
          const pubTime = item.published_utc
            ? new Date(item.published_utc).toLocaleString('en-US', {
                month: 'short',
                day: 'numeric',
                hour: '2-digit',
                minute: '2-digit',
              })
            : item.reference_date;

          // Clean "AAPL: " prefix
          const cleanTitle = item.title.replace(new RegExp(`^${ticker}:\\s*`, 'i'), '');

          // First 120 chars of snippet as summary
          const summary = item.snippet
            .split('\n')
            .filter((l) => !l.startsWith('##') && !l.startsWith('*Source:'))
            .join(' ')
            .trim();
          const shortSummary = summary.length > 120 ? summary.slice(0, 120) + '...' : summary;

          return (
            <div
              key={item.asset_id}
              className={`np-card ${isExpanded ? 'np-card-expanded' : ''} np-card-${sentiment}`}
              onClick={() => setExpandedId(isExpanded ? null : item.asset_id)}
            >
              <div className="np-card-top">
                <span className={`np-dot np-dot-${sentiment}`} />
                <span className="np-card-title">{cleanTitle}</span>
              </div>
              {!isExpanded && shortSummary && (
                <div className="np-card-summary">{shortSummary}</div>
              )}
              {isExpanded && (
                <div className="np-card-detail">
                  {summary.slice(0, 500)}
                  {summary.length > 500 ? '...' : ''}
                </div>
              )}
              <div className="np-card-footer">
                <span className="np-card-pub">{sourceName}</span>
                <span className="np-card-time">{pubTime}</span>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
