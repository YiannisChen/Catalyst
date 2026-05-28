import { useState } from 'react';
import RuntimeConsole from './RuntimeConsole';
import ArtifactTabs from '../artifacts/ArtifactTabs';
import AttributionResult from '../artifacts/EvidencePreview';
import type { RunSummaryResponse, RunEventResponse, ArtifactResponse } from '../../api/types';

interface Props {
  summary: RunSummaryResponse | null;
  events: RunEventResponse[];
  runId: string | null;
  artifacts: ArtifactResponse[];
  ticker: string | null;
  tradeDate: string | null;
}

type RuntimeTab = 'console' | 'artifacts' | 'evidence';

export default function PipelinePanel({
  summary,
  events,
  runId,
  artifacts,
  ticker,
  tradeDate,
}: Props) {
  const [runtimeTab, setRuntimeTab] = useState<RuntimeTab>('console');

  const hasRun = !!(summary && runId);
  const hasContext = !!(ticker && tradeDate);

  if (!hasContext) {
    return (
      <div className="pipeline-panel">
        <div className="pipeline-empty">
          <span className="empty-icon">&#x1F4CA;</span>
          <span>Select a ticker and click a date on the chart to view market context</span>
        </div>
      </div>
    );
  }

  /* Idle state: show pipeline flow guide instead of tabs */
  if (!hasRun) {
    return (
      <div className="pipeline-panel">
        <div className="attr-idle">
          <div className="attr-idle-header">
            <span className="attr-idle-icon">⚡</span>
            <span className="attr-idle-label">Attribution Pipeline</span>
          </div>
          <div className="attr-idle-hint">
            Click a date, then <strong>Run Attribution</strong> to analyze price movement drivers.
          </div>
          <div className="attr-idle-flow">
            <span className="attr-flow-node">Miner</span>
            <span className="attr-flow-sep">›</span>
            <span className="attr-flow-node">Critic</span>
            <span className="attr-flow-sep">›</span>
            <span className="attr-flow-node">Judge</span>
            <span className="attr-flow-sep">›</span>
            <span className="attr-flow-node">Validator</span>
          </div>
        </div>
      </div>
    );
  }

  const runtimeTabs: { key: RuntimeTab; label: string }[] = [
    { key: 'console', label: 'Timeline' },
    { key: 'artifacts', label: 'Artifacts' },
    { key: 'evidence', label: 'Attribution' },
  ];

  return (
    <div className="pipeline-panel">
      <div className="runtime-col-tabs">
        {runtimeTabs.map((tab) => (
          <button
            key={tab.key}
            className={`runtime-col-tab ${runtimeTab === tab.key ? 'active' : ''}`}
            onClick={() => setRuntimeTab(tab.key)}
          >
            {tab.label}
          </button>
        ))}
        {summary?.status && (
          <span className={`runtime-col-badge ${summary.status.toLowerCase()}`}>
            {summary.status.replace('_', ' ')}
          </span>
        )}
      </div>
      <div className="runtime-col-content">
        {runtimeTab === 'console' && <RuntimeConsole summary={summary} events={events} />}
        {runtimeTab === 'artifacts' && <ArtifactTabs artifacts={artifacts} />}
        {runtimeTab === 'evidence' && <AttributionResult artifacts={artifacts} />}
      </div>
    </div>
  );
}
