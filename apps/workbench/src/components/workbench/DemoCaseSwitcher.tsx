import type { DemoCase, DemoCaseId } from '../../mock/demoCases';

interface Props {
  cases: DemoCase[];
  activeId: DemoCaseId;
  onChange: (id: DemoCaseId) => void;
}

export default function DemoCaseSwitcher({ cases, activeId, onChange }: Props) {
  return (
    <label className="wb-control-field">
      <span>Demo scenario</span>
      <select value={activeId} onChange={(event) => onChange(event.target.value as DemoCaseId)}>
        {cases.map((demoCase) => (
          <option key={demoCase.id} value={demoCase.id}>{demoCase.scenarioName}</option>
        ))}
      </select>
    </label>
  );
}
