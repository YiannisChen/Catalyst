// The Catalyst mark: a quiet line, the event (dot), then the repricing. The event comes before the move.
const PATH = 'M4.5 16.5H9.5L14.5 7.5H19.5';

export function LogoMark({ size = 22, className }: { size?: number; className?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden="true" className={className}>
      <rect width="24" height="24" rx="6" fill="var(--accent)" />
      <path
        d={PATH}
        fill="none"
        stroke="var(--accent-ink)"
        strokeWidth="2.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <circle cx="9.5" cy="16.5" r="2.4" fill="var(--accent-ink)" />
    </svg>
  );
}
