import type { AttributionStatus } from '../../mock/demoCases';

interface Props {
  status: AttributionStatus;
  compact?: boolean;
}

const LABELS: Record<AttributionStatus, string> = {
  SUFFICIENT: 'Sufficient',
  PARTIAL: 'Partial',
  INSUFFICIENT: 'Insufficient',
  SYSTEM_ERROR: 'System error',
};

export default function StatusBadge({ status, compact = false }: Props) {
  return (
    <span className={`wb-status-badge wb-status-${status.toLowerCase()} ${compact ? 'wb-badge-compact' : ''}`}>
      <span className="wb-status-dot" aria-hidden="true" />
      {LABELS[status]}
    </span>
  );
}
