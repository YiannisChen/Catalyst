import { useMemo } from 'react';
import type { RunSummaryResponse, RunEventResponse } from '../../api/types';

/* ------------------------------------------------------------------ */
/* Pipeline stages                                                    */
/* ------------------------------------------------------------------ */
const PIPELINE_STAGES = ['miner', 'critic', 'judge', 'validator'] as const;
type StageStatus = 'completed' | 'active' | 'pending' | 'error';

function titleCase(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function formatMs(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

interface Props {
  events: RunEventResponse[];
  summary: RunSummaryResponse | null;
}

export default function NodeTimeline({ events, summary }: Props) {
  const stageData = useMemo(() => {
    /* Group events by node */
    const eventsByNode: Record<string, RunEventResponse[]> = {};
    for (const e of events) {
      if (!eventsByNode[e.node]) eventsByNode[e.node] = [];
      eventsByNode[e.node].push(e);
    }

    const isRunning = summary?.status === 'RUNNING';

    return PIPELINE_STAGES.map((stage) => {
      const nodeEvents = eventsByNode[stage] || [];
      const hasEvents = nodeEvents.length > 0;

      /* Determine status */
      let status: StageStatus = 'pending';
      if (hasEvents) {
        const lastEvent = nodeEvents[nodeEvents.length - 1];
        const statusAfter = (lastEvent.status_after || '').toUpperCase();
        if (statusAfter.includes('FAILED') || statusAfter.includes('ERROR')) {
          status = 'error';
        } else if (
          isRunning &&
          (stage === summary?.predicted_next_node || stage === summary?.last_completed_node)
        ) {
          /* If this is the latest stage with events during a running pipeline */
          status = 'active';
        } else {
          status = 'completed';
        }
      } else if (isRunning && stage === summary?.predicted_next_node) {
        status = 'active';
      }

      /* Aggregate metrics */
      let totalLatency = 0;
      let totalInputTokens = 0;
      let totalOutputTokens = 0;
      let totalCost = 0;
      let modelId: string | null = null;
      const transitions: string[] = [];

      for (const e of nodeEvents) {
        totalLatency += e.latency_ms || 0;
        totalInputTokens += e.input_tokens || 0;
        totalOutputTokens += e.output_tokens || 0;
        totalCost += e.cost_usd || 0;
        if (e.model_id) modelId = e.model_id;
        const before = e.status_before || '?';
        const after = e.status_after || '?';
        transitions.push(`${before} -> ${after}`);
      }

      return {
        stage,
        status,
        nodeEvents,
        modelId,
        totalLatency,
        totalInputTokens,
        totalOutputTokens,
        totalCost,
        transitions,
      };
    });
  }, [events, summary]);

  return (
    <div className="tl-pipeline">
      {stageData.map((s, idx) => (
        <div key={s.stage} className={`tl-stage tl-stage-${s.status}`}>
          {/* Left: connector + circle */}
          <div className="tl-connector-col">
            {idx > 0 && <div className={`tl-connector tl-connector-${s.status}`} />}
            <div className={`tl-circle tl-circle-${s.status}`} />
            {idx < stageData.length - 1 && <div className={`tl-connector-tail tl-connector-${stageData[idx + 1].status}`} />}
          </div>

          {/* Right: stage card */}
          <div className={`tl-stage-card tl-card-${s.status}`}>
            <div className="tl-stage-header">
              <span className="tl-stage-name">{titleCase(s.stage)}</span>
              {s.modelId && <span className="tl-stage-model">{s.modelId}</span>}
            </div>

            {s.nodeEvents.length > 0 && (
              <div className="tl-stage-metrics">
                <span className="tl-metric">
                  Latency <strong>{formatMs(s.totalLatency)}</strong>
                </span>
                {(s.totalInputTokens > 0 || s.totalOutputTokens > 0) && (
                  <span className="tl-metric">
                    Tokens <strong>{s.totalInputTokens + s.totalOutputTokens}</strong>
                    <span className="tl-metric-detail"> ({s.totalInputTokens}in / {s.totalOutputTokens}out)</span>
                  </span>
                )}
                {s.totalCost > 0 && (
                  <span className="tl-metric">
                    Cost <strong>${s.totalCost.toFixed(4)}</strong>
                  </span>
                )}
              </div>
            )}

            {s.transitions.length > 0 && (
              <div className="tl-stage-transitions">
                {s.transitions.map((t, i) => (
                  <span key={i} className="tl-transition-badge">{t}</span>
                ))}
              </div>
            )}

            {s.status === 'pending' && (
              <div className="tl-stage-pending-label">Waiting</div>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}
