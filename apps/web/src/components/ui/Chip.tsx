import { cva } from 'class-variance-authority';
import { cn } from '@/lib/cn';
import { formatCount } from '@/lib/format';
export interface ChipProps {
  label: string;
  count?: number;
  selected?: boolean;
  onClick?: () => void;
  className?: string;
}
const chipVariants = cva(
  'control-sm inline-flex flex-none items-center gap-2 whitespace-nowrap rounded-sm border px-2 text-caption font-medium hover:bg-bg-300 hover:text-ink-100',
  {
    variants: {
      selected: {
        true: 'chip-selected border-accent bg-accent-soft text-accent',
        false: 'border-line-100 bg-bg-200 text-ink-200',
      },
    },
    defaultVariants: { selected: false },
  },
);
export function Chip({ label, count, selected, onClick, className }: ChipProps) {
  return (
    <button
      type="button"
      className={cn(chipVariants({ selected: Boolean(selected) }), className)}
      aria-pressed={selected ? 'true' : 'false'}
      onClick={onClick}
    >
      <span>{label}</span>
      {count != null && <span className="chip-count font-numeric text-ink-300">{formatCount(count)}</span>}
    </button>
  );
}
