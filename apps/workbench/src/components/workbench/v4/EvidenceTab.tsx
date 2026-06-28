import { useState, useMemo } from 'react';
import type { EvidenceDecision, EvidenceItem } from '../../../mock/demoCases';

interface Props {
  evidence: EvidenceItem[];
  referencedIds: Set<string>;
  completed: boolean;
}

type FilterKey = 'all' | 'cited' | 'accepted' | 'rejected' | 'ungraded';

const FILTERS: { key: FilterKey; label: string }[] = [
  { key: 'all', label: 'All' },
  { key: 'cited', label: 'Cited' },
  { key: 'accepted', label: 'Accepted' },
  { key: 'rejected', label: 'Rejected' },
  { key: 'ungraded', label: 'Ungraded' },
];

const DECISION_LABELS: Record<EvidenceDecision, string> = {
  accepted: 'Accepted',
  rejected: 'Rejected',
  ungraded: 'Ungraded',
};

function formatTimestamp(timestamp: string): string {
  return new Intl.DateTimeFormat('en-US', {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    timeZone: 'UTC',
  }).format(new Date(timestamp));
}

export default function EvidenceTab({ evidence, referencedIds, completed }: Props) {
  const [activeFilter, setActiveFilter] = useState<FilterKey>('all');

  const { referenced, filtered } = useMemo(() => {
    const ref = evidence.filter((e) => referencedIds.has(e.id));
    let filt = evidence;
    if (activeFilter === 'cited') filt = ref;
    else if (activeFilter === 'accepted') filt = evidence.filter((e) => e.criticDecision === 'accepted');
    else if (activeFilter === 'rejected') filt = evidence.filter((e) => e.criticDecision === 'rejected');
    else if (activeFilter === 'ungraded') filt = evidence.filter((e) => e.criticDecision === 'ungraded');
    return { referenced: ref, filtered: filt };
  }, [evidence, referencedIds, activeFilter]);

  if (evidence.length === 0) {
    return (
      <div className="v4-empty-state">
        <span className="v4-empty-text">No evidence items available.</span>
      </div>
    );
  }

  return (
    <div className="v4-evidence-tab">
      <div className="v4-evidence-filters" role="group" aria-label="Evidence filters">
        {FILTERS.map((f) => (
          <button
            key={f.key}
            className={`v4-filter-chip ${activeFilter === f.key ? 'is-active' : ''}`}
            onClick={() => setActiveFilter(f.key)}
            aria-pressed={activeFilter === f.key}
          >
            {f.label}
          </button>
        ))}
        <span className="v4-filter-count">{filtered.length} of {evidence.length}</span>
      </div>

      {filtered.length === 0 ? (
        <div className="v4-empty-state">
          <span className="v4-empty-text">No evidence matches the selected filter.</span>
        </div>
      ) : (
        <>
          {completed && referenced.length > 0 && activeFilter === 'all' && (
            <div className="v4-evidence-section">
              <span className="v4-evidence-section-label">Cited Evidence</span>
              <div className="v4-evidence-grid">
                {referenced.map((item) => (
                  <EvidenceGridCard key={item.id} item={item} cited completed={completed} />
                ))}
              </div>
            </div>
          )}

          <div className="v4-evidence-section">
            {activeFilter !== 'all' || (completed && referenced.length > 0) ? (
              <span className="v4-evidence-section-label">
                {activeFilter === 'cited'
                  ? 'Cited Evidence'
                  : activeFilter === 'all'
                  ? 'Retrieved Evidence'
                  : 'Evidence'}
              </span>
            ) : (
              <span className="v4-evidence-section-label">All Evidence</span>
            )}
            <div className="v4-evidence-grid">
              {filtered.map((item) => (
                <EvidenceGridCard key={item.id} item={item} cited={referencedIds.has(item.id)} completed={completed} />
              ))}
            </div>
          </div>
        </>
      )}
    </div>
  );
}

function EvidenceGridCard({ item, cited, completed }: { item: EvidenceItem; cited: boolean; completed: boolean }) {
  return (
    <article className={`v4-evidence-grid-card ${cited ? 'is-cited' : ''}`}>
      <div className="v4-evidence-top">
        <span className="v4-ev-source">{item.source}</span>
        <span className={`v4-ev-decision v4-ev-decision-${item.criticDecision}`}>
          {DECISION_LABELS[item.criticDecision]}
        </span>
        <span className={`v4-ev-temporal v4-ev-temporal-${item.temporalStatus}`}>
          {item.temporalStatus}
        </span>
        {completed && cited && <span className="v4-cited-badge">Cited</span>}
      </div>
      <h4 className="v4-evidence-title">{item.title}</h4>
      <p className="v4-evidence-snippet">{item.snippet}</p>
      <div className="v4-evidence-meta-row">
        <span>Relevance {item.relevanceScore.toFixed(2)}</span>
      </div>
      <div className="v4-evidence-footer">
        <time dateTime={item.publishedAt}>{formatTimestamp(item.publishedAt)} UTC</time>
        <code>{item.id}</code>
      </div>
    </article>
  );
}
