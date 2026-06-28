import type { DemoCase, DemoCaseId } from '../../mock/demoCases';
import DemoCaseSwitcher from './DemoCaseSwitcher';
import type { WorkflowPhase } from './workflow-state';

interface Props {
  demoCase: DemoCase;
  cases: DemoCase[];
  selectedDate: string | null;
  phase: WorkflowPhase;
  canRun: boolean;
  onCaseChange: (id: DemoCaseId) => void;
  onRun: () => void;
}

export default function HeroSummary({ demoCase, cases, selectedDate, phase, canRun, onCaseChange, onRun }: Props) {
  return (
    <header className="wb-hero">
      <div className="wb-brand">
        <h1>Catalyst</h1>
        <p>Evidence-Bounded Stock Move Attribution</p>
      </div>

      <div className="wb-hero-controls">
        <label className="wb-control-field wb-ticker-control">
          <span>Ticker</span>
          <select value={demoCase.id} onChange={(event) => onCaseChange(event.target.value as DemoCaseId)}>
            {cases.map((item) => <option key={item.id} value={item.id}>{item.ticker}</option>)}
          </select>
        </label>
        {selectedDate && <span className="wb-selected-date-chip">Selected {selectedDate}</span>}
        <button
          type="button"
          className="wb-run-button"
          disabled={!canRun}
          onClick={onRun}
        >
          {phase === 'running' ? 'Running attribution...' : 'Run Attribution'}
        </button>
        <details className="wb-demo-data">
          <summary>Demo data</summary>
          <DemoCaseSwitcher cases={cases} activeId={demoCase.id} onChange={onCaseChange} />
        </details>
      </div>
    </header>
  );
}
