/**
 * Attribution Result — aggregates judge summary, causes with referenced
 * evidence, and validator verdict into a single attribution output view.
 */
import { useMemo } from 'react';
import type { ArtifactResponse } from '../../api/types';

/* ------------------------------------------------------------------ */
/* Helpers                                                            */
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
/* Component                                                          */
/* ------------------------------------------------------------------ */
interface Props {
  artifacts: ArtifactResponse[];
}

export default function AttributionResult({ artifacts }: Props) {
  const data = useMemo(() => {
    const judgeSummaryArt = artifacts.find((a) => a.artifact_type === 'judge_summary');
    const judgeCausesArt = artifacts.find((a) => a.artifact_type === 'judge_causes');
    const validatorArt = artifacts.find((a) => a.artifact_type === 'validator_decision');

    /* Build chunk lookup from retrieved + reranked */
    const chunkMap = new Map<string, any>();
    for (const a of artifacts) {
      if (a.artifact_type === 'retrieved_chunks' || a.artifact_type === 'reranked_chunks') {
        const chunks: any[] = (a.payload as any)?.chunks || (a.payload as any)?.items || [];
        for (const c of chunks) {
          if (c.asset_id) chunkMap.set(c.asset_id, c);
        }
      }
    }

    const summaryPayload = judgeSummaryArt ? (judgeSummaryArt.payload as any) : null;
    const causes: any[] = judgeCausesArt ? ((judgeCausesArt.payload as any)?.causes || []) : [];
    const validatorPayload = validatorArt ? (validatorArt.payload as any) : null;

    return { summaryPayload, causes, chunkMap, validatorPayload };
  }, [artifacts]);

  const { summaryPayload, causes, chunkMap, validatorPayload } = data;

  /* No judge artifacts yet */
  if (!summaryPayload && causes.length === 0) {
    return (
      <div className="attr-result">
        <div className="attr-empty">Attribution analysis not yet complete</div>
      </div>
    );
  }

  return (
    <div className="attr-result">
      {/* Judge Summary */}
      {summaryPayload && (
        <div className="attr-summary">
          <div className="attr-section-title">Judge Summary</div>
          {summaryPayload.grounding_rate != null && (
            <div className="attr-grounding">
              Grounding rate: <strong>{(summaryPayload.grounding_rate * 100).toFixed(1)}%</strong>
            </div>
          )}
          <div className="attr-summary-md">{summaryPayload.summary_md}</div>
        </div>
      )}

      {/* Causes */}
      {causes.length > 0 && (
        <div className="attr-causes">
          <div className="attr-section-title">Causes Breakdown</div>
          {causes.map((cause: any, idx: number) => (
            <div key={idx} className="attr-cause-card">
              <div className="cause-top">
                <span className="cause-title">{cause.title}</span>
                <span className={`cause-dir ${cause.direction}`}>{cause.direction}</span>
                <span className="cause-weight">{(cause.weight * 100).toFixed(0)}%</span>
              </div>
              <div className="cause-bar-bg">
                <div
                  className={`cause-bar-fill ${cause.direction}`}
                  style={{ width: `${Math.min(cause.weight * 100, 100)}%` }}
                />
              </div>
              {cause.summary && <p className="cause-summary">{cause.summary}</p>}

              {/* Referenced Evidence */}
              {cause.evidence_ids?.length > 0 && (
                <div className="attr-evidence-ref">
                  <div className="attr-ref-label">Referenced Evidence</div>
                  {cause.evidence_ids.map((eid: string, i: number) => {
                    const chunk = chunkMap.get(eid);
                    if (!chunk) {
                      return (
                        <div key={i} className="attr-ref-row">
                          <code className="attr-ref-id">{eid}</code>
                          <span className="attr-ref-missing">not found in evidence pool</span>
                        </div>
                      );
                    }
                    return (
                      <div key={i} className="attr-ref-chunk">
                        <div className="attr-ref-chunk-top">
                          <span className="chunk-rank">#{chunk.rank ?? '—'}</span>
                          <code className="attr-ref-id">{chunk.asset_id}</code>
                          <span className={`chunk-src ${sourceClass(chunk.source_type || '')}`}>
                            {sourceLabel(chunk.source_type || '')}
                          </span>
                          {chunk.reference_date && (
                            <span className="chunk-date">{chunk.reference_date}</span>
                          )}
                        </div>
                        {chunk.headline && (
                          <div className="attr-ref-headline">{chunk.headline}</div>
                        )}
                        {chunk.snippet && (
                          <p className="attr-ref-snippet">{chunk.snippet}</p>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Validator Verdict */}
      {validatorPayload && (
        <div className="attr-validator">
          <div className="attr-section-title">Validator Verdict</div>
          <div className="validator-card">
            <div className="validator-top">
              <span className={`validator-status ${(validatorPayload.output_status || '').toLowerCase()}`}>
                {validatorPayload.output_status || '—'}
              </span>
              {validatorPayload.validator_attempts != null && (
                <span className="validator-attempts">Attempts: {validatorPayload.validator_attempts}</span>
              )}
            </div>
            {validatorPayload.validation_error && (
              <p className="validator-error">{validatorPayload.validation_error}</p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
