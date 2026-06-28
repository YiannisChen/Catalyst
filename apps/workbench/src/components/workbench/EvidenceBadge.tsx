import type { EvidenceQuality, TemporalStatus } from '../../mock/demoCases';

type Props =
  | { kind: 'quality'; value: EvidenceQuality }
  | { kind: 'temporal'; value: TemporalStatus }
  | { kind: 'source'; value: string };

const LABELS: Record<EvidenceQuality | TemporalStatus, string> = {
  high: 'High quality',
  medium: 'Medium quality',
  low: 'Low quality',
  'same-day': 'Same day',
  'prior-window': 'Prior window',
  'outside-window': 'Outside window',
};

export default function EvidenceBadge({ kind, value }: Props) {
  const label = kind === 'source' ? value : LABELS[value];
  return <span className={`wb-evidence-badge wb-evidence-${kind} wb-evidence-${value}`}>{label}</span>;
}
