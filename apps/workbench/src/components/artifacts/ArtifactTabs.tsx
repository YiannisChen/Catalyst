import { useState, useMemo } from 'react';
import type { ArtifactResponse } from '../../api/types';

/* ------------------------------------------------------------------ */
/* Pipeline node tabs — fixed order                                   */
/* ------------------------------------------------------------------ */
type PipelineNode = 'miner' | 'critic' | 'judge' | 'validator';
const PIPELINE_NODES: PipelineNode[] = ['miner', 'critic', 'judge', 'validator'];

const NODE_LABELS: Record<PipelineNode, string> = {
  miner: 'Miner',
  critic: 'Critic',
  judge: 'Judge',
  validator: 'Validator',
};

/* ------------------------------------------------------------------ */
/* Source-type badges                                                  */
/* ------------------------------------------------------------------ */
function sourceLabel(sourceType: string): string {
  const map: Record<string, string> = {
    polygon_news: 'Polygon',
    fmp_news: 'FMP',
    finnhub_company_news: 'Finnhub',
    fmp_fundamentals: 'Fundamentals',
    sec_filing: 'SEC',
    macro_news: 'Macro',
    market_news: 'Market',
    fred_rates: 'FRED',
    gdelt_news: 'GDELT',
  };
  return map[sourceType] || sourceType;
}

function sourceClass(sourceType: string): string {
  if (sourceType.includes('fundamental') || sourceType.includes('sec')) return 'src-fund';
  if (sourceType.includes('macro') || sourceType.includes('fred') || sourceType.includes('gdelt')) return 'src-macro';
  return 'src-news';
}

/* ------------------------------------------------------------------ */
/* Chunk card — used by retrieved_chunks & reranked_chunks             */
/* ------------------------------------------------------------------ */
function ChunkCard({ chunk, showRerank }: { chunk: any; showRerank: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const assetId: string = chunk.asset_id || '';
  const isL2 = assetId.includes('::l2s');

  return (
    <div className="chunk-card">
      <div className="chunk-card-top">
        <span className="chunk-rank">#{chunk.rank ?? '—'}</span>
        <span className={`chunk-src ${sourceClass(chunk.source_type || '')}`}>
          {sourceLabel(chunk.source_type || '')}
        </span>
        {chunk.reference_date && (
          <span className="chunk-date">{chunk.reference_date}</span>
        )}
        <span className="chunk-score" title="RRF score">
          RRF {((chunk.score ?? 0) * 100).toFixed(1)}
        </span>
        {showRerank && chunk.rerank_score != null && (
          <span className="chunk-rerank" title="Cross-encoder rerank score">
            RE {(chunk.rerank_score * 100).toFixed(1)}
          </span>
        )}
      </div>

      {chunk.headline && (
        <div className="chunk-headline">{chunk.headline}</div>
      )}

      {chunk.snippet && (
        <p className="chunk-snippet">{chunk.snippet}</p>
      )}

      <div className="chunk-footer">
        <button
          className="chunk-id-toggle"
          onClick={() => setExpanded(!expanded)}
          title={expanded ? 'Hide asset ID' : 'Show asset ID'}
        >
          {expanded ? 'Hide ID' : 'Asset ID'}
        </button>
        {chunk.ticker && <span className="chunk-ticker">{chunk.ticker}</span>}
        {isL2 && <span className="chunk-l2-badge">L2</span>}
      </div>

      {expanded && (
        <div className="chunk-id-row">
          <code className="chunk-asset-id">{assetId || 'N/A'}</code>
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Graded evidence viewer                                             */
/* ------------------------------------------------------------------ */
function GradedEvidenceViewer({ items }: { items: any[] }) {
  if (!items.length) return <div className="av-empty">No graded evidence</div>;

  const relevanceClass = (r: unknown) => {
    const s = typeof r === 'string' ? r : '';
    const lower = s.toLowerCase();
    if (lower === 'high') return 'rel-high';
    if (lower === 'medium') return 'rel-med';
    return 'rel-low';
  };

  return (
    <div className="graded-list">
      {items.map((item, idx) => (
        <div key={idx} className="graded-card">
          <div className="graded-top">
            <span className={`graded-rel ${relevanceClass(item.relevance)}`}>
              {typeof item.relevance === 'string' ? item.relevance : '—'}
            </span>
            {item.category && <span className="graded-cat">{item.category}</span>}
            {item.temporal_alignment && (
              <span className="graded-temporal">{item.temporal_alignment}</span>
            )}
            {item.temporal_match && <span className="graded-time-ok">temporal</span>}
          </div>
          {item.reasoning && <p className="graded-reasoning">{typeof item.reasoning === 'string' ? item.reasoning : JSON.stringify(item.reasoning)}</p>}
          <div className="graded-meta">
            <span className="graded-id" title="chunk_id">{item.chunk_id || '—'}</span>
            {item.event_specificity && (
              <span className="graded-spec">spec: {item.event_specificity}</span>
            )}
            {item.evidence_granularity && (
              <span className="graded-gran">{item.evidence_granularity}</span>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Critic decision viewer                                             */
/* ------------------------------------------------------------------ */
function CriticDecisionViewer({ decision }: { decision: any }) {
  if (!decision) return <div className="av-empty">No critic decision</div>;
  const d = typeof decision === 'object' ? decision : { verdict: decision };

  /* Handle both formats: {verdict} and {sufficiency, next_action} */
  const rawVerdict = d.verdict || d.sufficiency || d.decision || '—';
  const verdict = typeof rawVerdict === 'string' ? rawVerdict : JSON.stringify(rawVerdict);
  const rawAction = d.next_action;
  const action = typeof rawAction === 'string' ? rawAction : null;

  return (
    <div className="critic-card">
      <div className="critic-verdict-row">
        <span className={`critic-verdict ${verdict.toLowerCase()}`}>
          {verdict}
        </span>
        {action && (
          <span className={`critic-action ${action}`}>→ {action}</span>
        )}
        {d.magnitude_coverage != null && (
          <span className="critic-count">coverage: {(d.magnitude_coverage * 100).toFixed(0)}%</span>
        )}
        {d.high_relevance_count != null && (
          <span className="critic-count high">H:{d.high_relevance_count}</span>
        )}
        {d.medium_relevance_count != null && (
          <span className="critic-count med">M:{d.medium_relevance_count}</span>
        )}
        {d.low_relevance_count != null && (
          <span className="critic-count low">L:{d.low_relevance_count}</span>
        )}
      </div>
      {d.reasoning && <p className="critic-reasoning">{typeof d.reasoning === 'string' ? d.reasoning : JSON.stringify(d.reasoning)}</p>}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Judge causes viewer                                                */
/* ------------------------------------------------------------------ */
function JudgeCausesViewer({ causes }: { causes: any[] }) {
  if (!causes.length) return <div className="av-empty">No causes identified</div>;

  return (
    <div className="causes-list">
      {causes.map((cause, idx) => {
        const weight = cause.weight ?? cause.confidence ?? 0;
        const label = cause.title || cause.text || cause.category || '—';
        return (
        <div key={idx} className="cause-card">
          <div className="cause-top">
            <span className="cause-title">{label}</span>
            <span className={`cause-dir ${cause.direction}`}>{cause.direction}</span>
            <span className="cause-weight">{(weight * 100).toFixed(0)}%</span>
          </div>
          {/* Weight bar */}
          <div className="cause-bar-bg">
            <div
              className={`cause-bar-fill ${cause.direction}`}
              style={{ width: `${Math.min(weight * 100, 100)}%` }}
            />
          </div>
          {(cause.summary || cause.text) && <p className="cause-summary">{cause.summary || cause.text}</p>}
          {cause.evidence_ids?.length > 0 && (
            <div className="cause-evidence-ids">
              {cause.evidence_ids.map((eid: string, i: number) => (
                <code key={i} className="cause-eid">{eid}</code>
              ))}
            </div>
          )}
        </div>
      );
      })}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Judge summary viewer                                               */
/* ------------------------------------------------------------------ */
function JudgeSummaryViewer({ payload }: { payload: any }) {
  const md = payload?.summary_md || '';
  const grounding = payload?.grounding_rate;

  return (
    <div className="judge-summary-card">
      {grounding != null && (
        <div className="judge-grounding">
          Grounding rate: <strong>{(grounding * 100).toFixed(1)}%</strong>
        </div>
      )}
      <div className="judge-md">{md}</div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Validator decision viewer                                          */
/* ------------------------------------------------------------------ */
function ValidatorViewer({ payload }: { payload: any }) {
  const status = payload?.output_status;
  const attempts = payload?.validator_attempts;
  const error = payload?.validation_error;

  return (
    <div className="validator-card">
      <div className="validator-top">
        <span className={`validator-status ${typeof status === 'string' ? status.toLowerCase() : ''}`}>
          {typeof status === 'string' ? status : '—'}
        </span>
        {attempts != null && (
          <span className="validator-attempts">Attempts: {attempts}</span>
        )}
      </div>
      {error && <p className="validator-error">{error}</p>}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Fallback raw JSON viewer                                           */
/* ------------------------------------------------------------------ */
function RawViewer({ payload }: { payload: any }) {
  return (
    <pre className="av-raw-json">
      {JSON.stringify(payload, null, 2)}
    </pre>
  );
}

/* ------------------------------------------------------------------ */
/* Route artifact to the right viewer                                 */
/* ------------------------------------------------------------------ */
function ArtifactContent({ artifacts }: { artifacts: ArtifactResponse[] }) {
  if (artifacts.length === 0) return <div className="av-empty">No data</div>;

  return (
    <div className="av-content-list">
      {artifacts.map((a, idx) => {
        const p = a.payload as any;
        const key = `${a.node}-${a.artifact_type}-${idx}`;

        /* Safety: catch any render-time errors per artifact */
        try { return renderArtifact(a, p, key); } catch (err) {
          return (
            <div key={key} className="av-section">
              <div className="av-section-header">{a.artifact_type} (render error)</div>
              <RawViewer payload={p} />
            </div>
          );
        }
      })}
    </div>
  );
}

function renderArtifact(a: ArtifactResponse, p: any, key: string) {

        switch (a.artifact_type) {
          case 'retrieved_chunks':
            return (
              <div key={key} className="av-section">
                <div className="av-section-header">
                  Hybrid Search Results (BM25 + Vector → RRF merge)
                  <span className="av-count">{(p.chunks || p.items || []).length} chunks</span>
                </div>
                {(p.chunks || p.items || []).map((chunk: any, i: number) => (
                  <ChunkCard key={i} chunk={chunk} showRerank={false} />
                ))}
              </div>
            );

          case 'reranked_chunks':
            return (
              <div key={key} className="av-section">
                <div className="av-section-header">
                  Cross-Encoder Reranked
                  <span className="av-count">{(p.chunks || p.items || []).length} chunks</span>
                </div>
                {(p.chunks || p.items || []).map((chunk: any, i: number) => (
                  <ChunkCard key={i} chunk={chunk} showRerank={true} />
                ))}
              </div>
            );

          case 'graded_evidence':
          case 'all_graded_chunks':
            return (
              <div key={key} className="av-section">
                <div className="av-section-header">
                  {a.artifact_type === 'graded_evidence' ? 'Graded Evidence' : 'All Graded Chunks'}
                </div>
                <GradedEvidenceViewer items={p.items || []} />
              </div>
            );

          case 'critic_decision':
            return (
              <div key={key} className="av-section">
                <CriticDecisionViewer decision={p.decision ?? p} />
              </div>
            );

          case 'judge_causes':
            return (
              <div key={key} className="av-section">
                <JudgeCausesViewer causes={p.causes || []} />
              </div>
            );

          case 'judge_summary':
            return (
              <div key={key} className="av-section">
                <JudgeSummaryViewer payload={p} />
              </div>
            );

          case 'validator_decision':
            return (
              <div key={key} className="av-section">
                <ValidatorViewer payload={p} />
              </div>
            );

          default:
            return (
              <div key={key} className="av-section">
                <div className="av-section-header">{a.artifact_type}</div>
                <RawViewer payload={p} />
              </div>
            );
        }
}

/* ------------------------------------------------------------------ */
/* Main export                                                        */
/* ------------------------------------------------------------------ */
interface Props {
  artifacts: ArtifactResponse[];
}

export default function ArtifactTabs({ artifacts }: Props) {
  /* Determine which pipeline nodes have artifacts */
  const nodesWithArtifacts = useMemo(() => {
    const nodeSet = new Set<string>();
    artifacts.forEach((a) => nodeSet.add(a.node));
    return PIPELINE_NODES.filter((n) => nodeSet.has(n));
  }, [artifacts]);

  const [activeTab, setActiveTab] = useState<PipelineNode | null>(
    nodesWithArtifacts.length > 0 ? nodesWithArtifacts[0] : null
  );

  // Auto-select first tab when available nodes change
  if (activeTab && !nodesWithArtifacts.includes(activeTab) && nodesWithArtifacts.length > 0) {
    setActiveTab(nodesWithArtifacts[0]);
  }

  const getArtifactsForNode = (node: PipelineNode) =>
    artifacts.filter((a) => a.node === node);

  if (nodesWithArtifacts.length === 0) {
    return (
      <div className="artifact-tabs">
        <div className="artifact-tabs-empty">No artifacts available</div>
      </div>
    );
  }

  return (
    <div className="artifact-tabs">
      <div className="artifact-tabs-header">
        {nodesWithArtifacts.map((node) => (
          <button
            key={node}
            className={`artifact-tab-button ${activeTab === node ? 'active' : ''}`}
            onClick={() => setActiveTab(node)}
          >
            {NODE_LABELS[node]}
            <span className="atb-count">{getArtifactsForNode(node).length}</span>
          </button>
        ))}
      </div>

      <div className="artifact-tabs-content">
        {activeTab && <ArtifactContent artifacts={getArtifactsForNode(activeTab)} />}
      </div>
    </div>
  );
}
