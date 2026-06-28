import { useState, useEffect, useMemo, useCallback, useRef } from 'react';
import type { ArtifactResponse } from '../../../api/types';
import type { AttributionResult, EvidenceItem, PipelineStep } from '../../../mock/demoCases';
import type { WorkflowPhase } from '../workflow-state';
import SummaryTab from './SummaryTab';
import TraceTab from './TraceTab';
import EvidenceTab from './EvidenceTab';
import DiagnosticsTab from './DiagnosticsTab';

type TabId = 'summary' | 'trace' | 'evidence' | 'diagnostics';

interface Props {
  result: AttributionResult | null;
  evidence: EvidenceItem[];
  referencedIds: Set<string>;
  steps: PipelineStep[];
  phase: WorkflowPhase;
  errorMessage: string | null;
  focusTab?: string | null;
  onTabFocused?: () => void;
  artifacts?: ArtifactResponse[];
  runtimeMs?: number;
}

const TAB_DEFS: { id: TabId; label: string }[] = [
  { id: 'summary', label: 'Summary' },
  { id: 'trace', label: 'Trace' },
  { id: 'evidence', label: 'Evidence' },
  { id: 'diagnostics', label: 'Diagnostics' },
];

export default function ResultWorkspaceTabs({
  result, evidence, referencedIds, steps, phase, errorMessage,
  focusTab, onTabFocused,
  artifacts = [],
  runtimeMs,
}: Props) {
  const [activeTab, setActiveTab] = useState<TabId>('summary');
  const tabRefs = useRef<Map<string, HTMLButtonElement>>(new Map());

  useEffect(() => {
    if (focusTab && TAB_DEFS.some((t) => t.id === focusTab)) {
      setActiveTab(focusTab as TabId);
      onTabFocused?.();
    }
  }, [focusTab, onTabFocused]);

  /* Focus the active tab after selection changes */
  useEffect(() => {
    tabRefs.current.get(activeTab)?.focus();
  }, [activeTab]);

  const acceptedCount = useMemo(
    () => evidence.filter((e) => e.criticDecision === 'accepted').length,
    [evidence],
  );

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      const idx = TAB_DEFS.findIndex((t) => t.id === activeTab);
      let nextIdx = idx;
      if (e.key === 'ArrowRight') nextIdx = Math.min(idx + 1, TAB_DEFS.length - 1);
      else if (e.key === 'ArrowLeft') nextIdx = Math.max(idx - 1, 0);
      else if (e.key === 'Home') nextIdx = 0;
      else if (e.key === 'End') nextIdx = TAB_DEFS.length - 1;
      else return;
      e.preventDefault();
      const nextTab = TAB_DEFS[nextIdx];
      setActiveTab(nextTab.id);
      tabRefs.current.get(nextTab.id)?.focus();
    },
    [activeTab],
  );

  const panelId = 'v4-tab-panel';

  return (
    <section className="v4-panel v4-result-workspace">
      <div className="v4-panel-header">
        <div>
          <h2 className="v4-panel-title">Attribution Workspace</h2>
        </div>
      </div>

      <div className="v4-tabs">
        <div className="v4-tab-bar" role="tablist" aria-label="Result workspace tabs" onKeyDown={handleKeyDown}>
          {TAB_DEFS.map((tab) => (
            <button
              key={tab.id}
              id={`v4-tab-${tab.id}`}
              ref={(el) => { if (el) tabRefs.current.set(tab.id, el); }}
              role="tab"
              aria-selected={activeTab === tab.id}
              aria-controls={panelId}
              tabIndex={activeTab === tab.id ? 0 : -1}
              className={`v4-tab ${activeTab === tab.id ? 'is-active' : ''}`}
              onClick={() => setActiveTab(tab.id)}
            >
              {tab.label}
            </button>
          ))}
        </div>

        <div id={panelId} className="v4-tab-content" role="tabpanel" aria-labelledby={`v4-tab-${activeTab}`}>
          {activeTab === 'summary' && (
            <SummaryTab result={result} phase={phase} errorMessage={errorMessage} acceptedCount={acceptedCount} citedCount={referencedIds.size} />
          )}
          {activeTab === 'trace' && (
            <TraceTab steps={steps} phase={phase} result={result} evidence={evidence} artifacts={artifacts} />
          )}
          {activeTab === 'evidence' && (
            <EvidenceTab evidence={evidence} referencedIds={referencedIds} completed={phase === 'completed'} />
          )}
          {activeTab === 'diagnostics' && (
            <DiagnosticsTab steps={steps} evidence={evidence} referencedIds={referencedIds} runtimeMs={runtimeMs} />
          )}
        </div>
      </div>
    </section>
  );
}
