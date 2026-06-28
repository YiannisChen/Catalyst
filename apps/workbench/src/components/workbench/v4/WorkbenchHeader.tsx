import type { DemoCase, DemoCaseId } from '../../../mock/demoCases';
import ModelSettings from './ModelSettings';
import type { ModelConfig } from '../../../api/types';
import type { WorkflowPhase } from '../workflow-state';

interface Props {
  demoCase: DemoCase;
  cases: DemoCase[];
  selectedDate: string | null;
  phase: WorkflowPhase;
  canRun: boolean;
  onCaseChange: (id: DemoCaseId) => void;
  onRun: () => void;
  /** LiveWorkbench BYOK: called when model is ready */
  onModelReady?: (config: ModelConfig) => void;
  /** LiveWorkbench BYOK: called when model is cleared */
  onModelClear?: () => void;
}

export default function WorkbenchHeader({
  demoCase,
  cases,
  selectedDate,
  phase,
  canRun,
  onCaseChange,
  onRun,
  onModelReady,
  onModelClear,
}: Props) {
  return (
    <header className="v4-header" role="banner">
      <div className="v4-header-brand">
        <h1 className="v4-logo">Catalyst</h1>
      </div>

      <div className="v4-header-controls">
        <select
          value={demoCase.id}
          onChange={(e) => onCaseChange(e.target.value as DemoCaseId)}
          className="v4-select"
          aria-label="Select ticker"
        >
          {cases.map((c) => (
            <option key={c.id} value={c.id}>
              {c.ticker}
            </option>
          ))}
        </select>

        <span className="v4-date-chip" role="button" aria-label="Session date">
          {selectedDate || 'Select date'}
        </span>

        <button
          type="button"
          className="v4-run-btn"
          disabled={!canRun}
          onClick={onRun}
        >
          {phase === 'running' ? 'Running…' : 'Run Attribution'}
        </button>
        {onModelReady && onModelClear && (
          <ModelSettings onModelReady={onModelReady} onModelClear={onModelClear} />
        )}
      </div>
    </header>
  );
}
