import type React from 'react';
import { Link } from 'react-router';
import { Button } from '@/components/ui';
import { StateNotice } from '@/components/catalyst';
import { formatDay, formatEtDateTime, formatPct, formatUsd } from '@/lib/format';

export type ReadyToRunProps = {
  ticker: string;
  date: string;
  session: { volume?: number | null } | undefined;
  candles: { date: string; volume: number }[];
  move: number;
  gap: number | null;
  intraday: number | null;
  newsCount: number | null;
  overnightCount: number;
  inSessionCount: number;
  cutoffDate?: string;
  modelLine: string;
  analysisCredential: 'browser_key' | 'server_env' | 'none';
  anonymousVisitor: boolean;
  freeRunsRemaining?: number;
  quotaLimit?: number;
  avgCost: number | null;
  avgSeconds: number | null;
  onExplain: () => void;
  onChangeModel: () => void;
  onAddKey?: () => void;
  starting: boolean;
  error: string | null;
  quotaReached?: boolean;
  retryable: boolean;
};

const compact = new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 });

export function ReadyToRun(props: ReadyToRunProps): React.JSX.Element {
  const earlier = props.candles.filter((bar) => bar.date < props.date).slice(-20);
  const averageVolume =
    earlier.length >= 10 ? earlier.reduce((sum, bar) => sum + bar.volume, 0) / earlier.length : null;
  const ratio = props.session?.volume != null && averageVolume ? props.session.volume / averageVolume : null;
  const gapMagnitude = Math.abs(props.gap ?? 0);
  const intradayMagnitude = Math.abs(props.intraday ?? 0);
  const total = gapMagnitude + intradayMagnitude;
  const gapWidth = total ? `${(gapMagnitude / total) * 100}%` : '0%';
  const moveTone = props.move > 0 ? 'text-up' : props.move < 0 ? 'text-down' : 'text-ink-200';
  const ownLabel =
    props.analysisCredential === 'browser_key'
      ? 'your key'
      : props.analysisCredential === 'server_env'
        ? 'server key'
        : 'no key yet';
  const estimate = [
    props.avgCost == null || ownLabel === 'your key' ? null : `~${formatUsd(props.avgCost)}`,
    props.avgSeconds == null ? null : `~${Math.round(props.avgSeconds)} s`,
  ]
    .filter(Boolean)
    .join(' · ');
  const failed = props.error && props.quotaReached;
  const ownKeyLimitReached = props.error === "You reached today's limit of runs with your own key.";
  return (
    <div className="ready-panel">
      <section className="ready-move">
        <span className="type-body-sm text-ink-200">
          {props.ticker} · {formatDay(props.date)}
        </span>
        <strong className={`ready-move-value font-numeric ${moveTone}`}>{formatPct(props.move)}</strong>
        <div className="ready-move-bar" aria-label="Opening gap and open to close portions of the move">
          <span
            className={`ready-bar-gap ${props.gap != null && props.gap < 0 ? 'ready-bar-down' : 'ready-bar-up'}`}
            style={{ width: gapWidth }}
          />
          <span
            className={props.intraday != null && props.intraday < 0 ? 'ready-bar-down' : 'ready-bar-up'}
            style={{ width: `${total ? (intradayMagnitude / total) * 100 : 0}%` }}
          />
        </div>
        <div className="ready-split type-caption text-ink-200">
          <span>
            Opening gap <span className="font-numeric">{props.gap == null ? '—' : formatPct(props.gap)}</span>
          </span>
          <span>
            Open to close{' '}
            <span className="font-numeric">{props.intraday == null ? '—' : formatPct(props.intraday)}</span>
          </span>
        </div>
        <span className="type-caption text-ink-200">
          Volume{' '}
          <span className="font-numeric">
            {props.session?.volume == null ? '—' : compact.format(props.session.volume)}
          </span>
          {ratio != null && (
            <>
              {' '}
              · <span className="font-numeric text-ink-100">{ratio.toFixed(1)}×</span> its 20-day average
            </>
          )}
        </span>
      </section>
      {props.anonymousVisitor ? (
        <section className="ready-visitor-box">
          <strong>Explaining a new day needs your own key</strong>
          <span>One run costs about $0.01 with your provider. The key stays in this browser.</span>
          <Button variant="primary" className="w-full" onClick={props.onAddKey}>
            Add a key and explain
          </Button>
          <Link className="ready-feature-link" to="/featured">
            See featured cases · free
          </Link>
        </section>
      ) : (
        <section className="ready-action">
          <Button variant="primary" className="w-full" disabled={props.starting} onClick={props.onExplain}>
            {props.starting ? 'Starting…' : 'Explain this move'}
          </Button>
          <div className="ready-meta type-caption text-ink-300">
            <span>
              {props.modelLine.split(' · ')[0]} · {ownLabel}
              {estimate ? ` · ${estimate}` : ''}
            </span>
            <button type="button" className="text-info" onClick={props.onChangeModel}>
              Change
            </button>
          </div>
          {props.error && !failed && (
            <div role="status" className="flex items-center justify-between gap-2 type-caption text-down">
              <span>{props.error}</span>
              {props.retryable && (
                <Button size="sm" variant="ghost" onClick={props.onExplain}>
                  Retry
                </Button>
              )}
            </div>
          )}
          {failed && (
            <StateNotice
              tone="warn"
              title={ownKeyLimitReached ? 'Daily limit reached' : 'Free demo limit reached'}
              body={
                ownKeyLimitReached
                  ? props.error!
                  : props.quotaLimit == null
                    ? 'You reached the limit of free runs for now.'
                    : `You used today’s ${props.quotaLimit} free runs. Add your own key to keep going, or replay a featured case for free.`
              }
              action={
                ownKeyLimitReached
                  ? undefined
                  : props.onAddKey
                    ? { label: 'Add a key', onClick: props.onAddKey }
                    : undefined
              }
              secondaryAction={ownKeyLimitReached ? undefined : { label: 'Featured cases', to: '/featured' }}
            />
          )}
        </section>
      )}
      <section className="ready-section">
        <span className="type-label text-ink-300">What Catalyst will read</span>
        <div className="ready-row">
          <span>Headlines</span>
          <span>
            <span className="font-numeric">{props.newsCount ?? '—'}</span>
            <span className="text-ink-300">
              {' '}
              · {props.overnightCount} overnight · {props.inSessionCount} in session
            </span>
          </span>
        </div>
        <div className="ready-row">
          <span>Filings and releases</span>
          <span className="text-ink-200">up to the close</span>
        </div>
        <div className="ready-row">
          <span>Cutoff</span>
          <span className="text-ink-200">
            {props.cutoffDate ? formatEtDateTime(props.cutoffDate) : `${formatDay(props.date)}, 4:00 PM ET`}
          </span>
        </div>
        {!props.anonymousVisitor && (
          <p className="m-0 type-caption text-ink-300">Nothing published after the close is used.</p>
        )}
      </section>
      {!props.anonymousVisitor && (
        <section className="ready-section ready-steps">
          <span className="type-label text-ink-300">How it works</span>
          <div>
            <b>1</b>
            <span>Picks the headlines that could explain the move</span>
          </div>
          <div>
            <b>2</b>
            <span>Checks every quote against the full article</span>
          </div>
          <div>
            <b>3</b>
            <span>Compares with the sector and peers; a second model checks the answer</span>
          </div>
        </section>
      )}
    </div>
  );
}
