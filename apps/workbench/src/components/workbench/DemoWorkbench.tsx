import { useEffect, useMemo, useReducer, useState, useCallback } from 'react';
import {
  DEFAULT_DEMO_CASE_ID,
  DEMO_CASES,
  getDemoCase,
  type DemoCaseId,
} from '../../mock/demoCases';
import {
  collectReferencedEvidenceIds,
  getCandidateEvidence,
  resolveAttributionResult,
} from './workbench-model';
import {
  canRunAttribution,
  createWorkflowState,
  workflowReducer,
} from './workflow-state';
import WorkbenchHeader from './v4/WorkbenchHeader';
import MarketSessionPanel from './v4/MarketSessionPanel';
import SessionOverviewPanel from './v4/SessionOverviewPanel';
import EvidenceIntakePanel from './v4/EvidenceIntakePanel';
import ResultWorkspaceTabs from './v4/ResultWorkspaceTabs';
import { MOCK_ARTIFACTS_BY_CASE, MOCK_RUNTIME_MS_BY_CASE } from '../../dev/mockRunData';
import type { ArtifactResponse } from '../../api/types';
import './v4/workbench-v4.css';

export default function DemoWorkbench() {
  const [activeCaseId, setActiveCaseId] = useState<DemoCaseId>(DEFAULT_DEMO_CASE_ID);
  const demoCase = getDemoCase(activeCaseId);
  const [workflow, dispatch] = useReducer(
    workflowReducer,
    demoCase.defaultSelectedDate,
    createWorkflowState,
  );
  const [focusTab, setFocusTab] = useState<string | null>(null);
  const canRun = canRunAttribution(workflow);

  const candidateEvidence = useMemo(
    () => getCandidateEvidence(demoCase, workflow.selectedDate),
    [demoCase, workflow.selectedDate],
  );

  const result = useMemo(
    () =>
      workflow.phase === 'completed' && workflow.selectedDate
        ? resolveAttributionResult(demoCase, workflow.selectedDate)
        : null,
    [demoCase, workflow.phase, workflow.selectedDate],
  );

  const referencedIds = useMemo(
    () => (result ? collectReferencedEvidenceIds(result) : new Set<string>()),
    [result],
  );

  const visibleSteps = useMemo(() => {
    if (workflow.phase === 'idle') return demoCase.pipelineBeforeRun;
    if (workflow.phase === 'completed') {
      if (result?.status === demoCase.id) return demoCase.pipelineCompleted;
      return demoCase.pipelineCompleted.map((step) => {
        if (step.id === 'critic')
          return { ...step, status: 'warning' as const, note: 'Evidence failed sufficiency checks' };
        if (step.id === 'router')
          return { ...step, status: 'complete' as const, note: 'Refusal route selected' };
        if (step.id === 'judge')
          return { ...step, status: 'skipped' as const, note: 'Skipped after refusal route' };
        if (step.id === 'finalized')
          return { ...step, status: 'complete' as const, note: 'Insufficient evidence response' };
        return step;
      });
    }
    if (workflow.phase === 'error') {
      return demoCase.pipelineBeforeRun.map((step, index) =>
        index === workflow.completedStepCount
          ? { ...step, status: 'error' as const, note: workflow.errorMessage ?? 'System error' }
          : step,
      );
    }
    return demoCase.pipelineCompleted.map((step, index) => {
      if (index < workflow.completedStepCount) return { ...step, status: 'complete' as const };
      if (index === workflow.completedStepCount)
        return { ...step, status: 'active' as const, note: 'Processing' };
      return { ...demoCase.pipelineBeforeRun[index] };
    });
  }, [demoCase, result, workflow]);

  // Scroll to top on mount
  useEffect(() => {
    window.scrollTo(0, 0);
    document.documentElement.scrollTop = 0;
    document.body.scrollTop = 0;
    const mainEl = document.querySelector('.v4-page');
    if (mainEl) mainEl.scrollTop = 0;
  }, []);

  useEffect(() => {
    if (workflow.phase !== 'running') return;
    const timer = window.setTimeout(() => {
      if (workflow.completedStepCount >= demoCase.pipelineCompleted.length) {
        dispatch({ type: 'COMPLETE_RUN' });
      } else {
        dispatch({ type: 'ADVANCE_STEP' });
      }
    }, 180);
    return () => window.clearTimeout(timer);
  }, [demoCase.pipelineCompleted.length, workflow.completedStepCount, workflow.phase]);

  const handleScenarioChange = (id: DemoCaseId) => {
    const nextCase = getDemoCase(id);
    setActiveCaseId(id);
    dispatch({ type: 'RESET_SCENARIO', date: nextCase.defaultSelectedDate });
    setFocusTab(null);
  };

  const handleViewAllEvidence = useCallback(() => {
    setFocusTab('evidence');
  }, []);

  return (
    <main className="v4-page" id="v4-main-content">
      <a href="#v4-main-content" className="v4-skip-link">Skip to content</a>
      <div className="v4-shell">
        <WorkbenchHeader
          demoCase={demoCase}
          cases={DEMO_CASES}
          selectedDate={workflow.selectedDate}
          phase={workflow.phase}
          canRun={canRun}
          onCaseChange={handleScenarioChange}
          onRun={() => dispatch({ type: 'START_RUN' })}
          onModelReady={() => {}}
          onModelClear={() => {}}
        />

        <section className="v4-market-composite">
          <MarketSessionPanel
            demoCase={demoCase}
            selectedDate={workflow.selectedDate}
            onSelectDate={(date) => dispatch({ type: 'SELECT_DATE', date })}
          />
          <div className="v4-market-session-col">
            <SessionOverviewPanel
              demoCase={demoCase}
              selectedDate={workflow.selectedDate}
              canRun={canRun}
            />
          </div>
        </section>

        <EvidenceIntakePanel mode="preview"
          evidence={candidateEvidence}
          referencedIds={referencedIds}
          completed={workflow.phase === 'completed'}
          selectedDate={workflow.selectedDate}
          onViewAll={handleViewAllEvidence}
        />

        <ResultWorkspaceTabs
          result={result}
          evidence={candidateEvidence}
          referencedIds={referencedIds}
          steps={visibleSteps}
          phase={workflow.phase}
          errorMessage={workflow.errorMessage}
          focusTab={focusTab}
          onTabFocused={() => setFocusTab(null)}
          artifacts={workflow.phase === 'completed' ? (MOCK_ARTIFACTS_BY_CASE[activeCaseId] as ArtifactResponse[]) : []}
          runtimeMs={workflow.phase === 'completed' ? MOCK_RUNTIME_MS_BY_CASE[activeCaseId] : undefined}
        />
      </div>
    </main>
  );
}
