import { useRef, type KeyboardEvent } from 'react';
import { cn } from '@/lib/cn';
export interface TabsProps {
  items: { id: string; label: string; count?: number }[];
  value: string;
  onChange?: (id: string) => void;
  className?: string;
}
export function Tabs({ items, value, onChange, className }: TabsProps) {
  const refs = useRef<Array<HTMLButtonElement | null>>([]);
  function handleKey(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    const direction = event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0;
    if (!direction) return;
    event.preventDefault();
    const next = (index + direction + items.length) % items.length;
    refs.current[next]?.focus();
    const target = items[next];
    if (target) onChange?.(target.id);
  }
  return (
    <div role="tablist" className={cn('flex gap-1 rounded-md bg-bg-200 p-1', className)}>
      {items.map((item, i) => (
        <button
          key={item.id}
          id={`tab-${item.id}`}
          ref={(node) => {
            refs.current[i] = node;
          }}
          type="button"
          role="tab"
          aria-controls={value === item.id ? `tabpanel-${item.id}` : undefined}
          aria-selected={value === item.id}
          tabIndex={value === item.id ? 0 : -1}
          className={cn(
            'tabs-control inline-flex flex-1 items-center justify-center gap-1 whitespace-nowrap rounded-sm px-2 text-caption font-medium text-ink-200 hover:text-ink-100',
            value === item.id && 'bg-bg-300 text-ink-100',
          )}
          onClick={() => onChange?.(item.id)}
          onKeyDown={(event) => handleKey(event, i)}
        >
          {item.label}
          {item.count != null && <span className="font-numeric text-ink-300">{item.count}</span>}
        </button>
      ))}
    </div>
  );
}
