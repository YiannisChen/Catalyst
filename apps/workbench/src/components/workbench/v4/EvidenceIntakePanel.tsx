import type { EvidenceItem } from '../../../mock/demoCases';

interface Props {
  evidence: EvidenceItem[];
  referencedIds: Set<string>;
  completed: boolean;
  selectedDate: string | null;
  onViewAll?: () => void;
  /** Preview mode — news shown before attribution run. */
  mode?: 'preview' | 'retrieved';
  /** Whether news is currently loading. */
  loading?: boolean;
  /** Error message from news fetch. */
  error?: string | null;
}

function formatTimestamp(timestamp: string): string {
  return new Intl.DateTimeFormat('en-US', {
    month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZone: 'UTC',
  }).format(new Date(timestamp));
}

const MAX_VISIBLE = 3;
const DECISION_LABELS: Record<string, string> = { accepted: 'Accepted', rejected: 'Rejected', ungraded: 'Ungraded' };

export default function EvidenceIntakePanel({ evidence, referencedIds, completed, selectedDate, onViewAll, mode = 'retrieved', loading = false, error = null }: Props) {
  const hasEvidence = evidence.length > 0;
  const visible = evidence.slice(0, MAX_VISIBLE);
  const overflow = evidence.length - MAX_VISIBLE;
  const citedCount = evidence.filter((e) => referencedIds.has(e.id)).length;

  return (
    <section className="v4-panel v4-evidence-intake">
      <div className="v4-panel-header">
        <div>
          <h2 className="v4-panel-title">
                {loading ? 'Loading news preview…'
                  : hasEvidence
                    ? (mode === 'preview' ? 'Candidate news' : 'Retrieved Evidence')
                    : 'No evidence'}
              </h2>
        </div>
        {hasEvidence && !loading && (
                <span className="v4-count-badge">
                  {evidence.length} {mode === 'preview' ? 'articles' : 'retrieved'}
                </span>
              )}
      </div>

      {loading && !hasEvidence ? (
        <div className="v4-evidence-intake-empty">
          <span>Loading news preview…</span>
        </div>
      ) : error && !hasEvidence ? (
        <div className="v4-evidence-intake-empty v4-state-banner--error" role="alert">
          <span>{error}</span>
        </div>
      ) : !hasEvidence ? (
        <div className="v4-evidence-intake-empty">
          <span>{selectedDate ? 'No news found in the selected event window.' : 'Select a candle to preview its evidence window.'}</span>
        </div>
      ) : (
        <>
          <div className="v4-evidence-strip">
            {visible.map((item) => {
              const cited = referencedIds.has(item.id);
              return (
                <article key={item.id} className={`v4-evidence-card ${cited ? 'is-cited' : ''}`}>
                  <div className="v4-evidence-top">
                    <span className="v4-ev-source">{item.source}</span>
                    <span className={`v4-ev-temporal v4-ev-temporal-${item.temporalStatus}`}>{item.temporalStatus}</span>
                    {completed && (
                      <span className={`v4-ev-decision v4-ev-decision-${item.criticDecision}`}>
                        {DECISION_LABELS[item.criticDecision] ?? item.criticDecision}
                      </span>
                    )}
                    {completed && cited && <span className="v4-cited-badge">Cited</span>}
                  </div>
                  <h3 className="v4-evidence-title">{item.title}</h3>
                  <p className="v4-evidence-snippet">{item.snippet}</p>
                  <div className="v4-evidence-footer">
                    <time dateTime={item.publishedAt}>{formatTimestamp(item.publishedAt)} UTC</time>
                    <code>{item.id}</code>
                  </div>
                </article>
              );
            })}
          </div>

          <div className="v4-evidence-intake-footer">
            <span className="v4-evidence-intake-count">
              {completed && citedCount > 0
                ? <><strong>{citedCount}</strong> of <strong>{evidence.length}</strong> cited</>
                : <><strong>{evidence.length}</strong> retrieved</>
              }
              {overflow > 0 && <> &middot; <strong>{overflow}</strong> more</>}
            </span>
            <button type="button" className="v4-evidence-intake-action" onClick={onViewAll}>View all evidence &rarr;</button>
          </div>
        </>
      )}
    </section>
  );
}
