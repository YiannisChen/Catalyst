import type { EvidenceItem } from '../../mock/demoCases';
import EvidenceBadge from './EvidenceBadge';

interface Props {
  evidence: EvidenceItem[];
  referencedIds: Set<string>;
  completed: boolean;
  selectedDate: string | null;
}

function formatTimestamp(timestamp: string): string {
  return new Intl.DateTimeFormat('en-US', {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    timeZone: 'UTC',
  }).format(new Date(timestamp));
}

export default function EvidenceTimeline({ evidence, referencedIds, completed, selectedDate }: Props) {
  return (
    <section className="wb-card wb-evidence-card" aria-labelledby="evidence-title">
      <div className="wb-card-header">
        <div>
          <span className="wb-card-kicker">Event-window evidence</span>
          <h2 id="evidence-title">{completed ? 'Referenced evidence' : 'Candidate evidence'}</h2>
        </div>
        <span className="wb-card-count">{evidence.length} items</span>
      </div>

      {evidence.length === 0 ? (
        <div className="wb-evidence-empty">{selectedDate ? 'No candidate evidence is available for this demo date.' : 'Select a candle to preview its evidence window.'}</div>
      ) : <div className="wb-evidence-list">
        {evidence.map((item) => {
          const referenced = referencedIds.has(item.id);
          return (
            <article key={item.id} className={`wb-evidence-item ${referenced ? 'is-referenced' : ''}`}>
              <div className="wb-evidence-rail" aria-hidden="true">
                <span />
              </div>
              <div className="wb-evidence-content">
                <div className="wb-evidence-badges">
                  <EvidenceBadge kind="source" value={item.source} />
                  <EvidenceBadge kind="quality" value={item.quality} />
                  <EvidenceBadge kind="temporal" value={item.temporalStatus} />
                  {completed && referenced && <span className="wb-reference-label">Cited</span>}
                </div>
                <h3>{item.title}</h3>
                <p>{item.snippet}</p>
                <div className="wb-evidence-meta">
                  <span>{item.source}</span>
                  <time dateTime={item.publishedAt}>{formatTimestamp(item.publishedAt)} UTC</time>
                  <code>{item.id}</code>
                </div>
              </div>
            </article>
          );
        })}
      </div>}
    </section>
  );
}
