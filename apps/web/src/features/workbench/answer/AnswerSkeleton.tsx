import type React from 'react';
import { Skeleton, Tabs } from '@/components/ui';
export function AnswerSkeleton(): React.JSX.Element {
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-4">
      <Tabs
        items={[
          { id: 'answer', label: 'Answer' },
          { id: 'process', label: 'Process' },
          { id: 'run', label: 'Run' },
        ]}
        value="answer"
      />
      <div className="flex items-center gap-3">
        <Skeleton height={28} width="24%" />
        <Skeleton height={16} width="32%" />
      </div>
      <Skeleton height={12} width="46%" />
      <Skeleton height={64} />
      <Skeleton height={12} width="58%" />
      <Skeleton height={96} />
    </div>
  );
}
