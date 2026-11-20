import { cn } from '@/lib/cn';
export interface StageStepperProps {
  stages: {
    id: string;
    label: string;
    detail?: string;
    status?: 'done' | 'active' | 'pending' | 'failed';
    time?: string;
  }[];
  className?: string;
}
export function StageStepper({ stages, className }: StageStepperProps) {
  return (
    <ol className={cn('flex list-none flex-col p-0', className)}>
      {stages.map((stage, i) => {
        const status = stage.status ?? 'pending';
        return (
          <li key={stage.id} className="stage-row gap-3">
            <div className="flex flex-col items-center">
              <span
                className={cn(
                  'mt-1 size-3 shrink-0 rounded-full border-2 border-line-control bg-bg-100',
                  status === 'done' && 'border-up bg-up',
                  status === 'active' && 'animate-pulse-soft border-info',
                  status === 'failed' && 'border-down bg-down',
                )}
              />
              <span
                className={cn(
                  'stage-line min-h-3 flex-1 bg-line-200',
                  status === 'done' && 'bg-up-soft',
                  i === stages.length - 1 && 'hidden',
                )}
              />
            </div>
            <div className="stage-detail-gap flex min-w-0 flex-col pb-3">
              <span
                className={cn(
                  'text-body-sm font-medium',
                  status === 'pending' ? 'text-ink-300' : 'text-ink-100',
                )}
              >
                {stage.label}
              </span>
              {stage.detail && <span className="text-caption text-ink-200">{stage.detail}</span>}
            </div>
            <span className="font-numeric text-caption text-ink-300">{stage.time}</span>
          </li>
        );
      })}
    </ol>
  );
}
