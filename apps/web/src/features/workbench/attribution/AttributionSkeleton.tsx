import type React from 'react';
import { Skeleton } from '@/components/ui';
export function AttributionSkeleton(): React.JSX.Element {
  return (
    <div className="flex flex-col gap-3">
      <Skeleton height={24} />
      <Skeleton height={80} />
      <Skeleton height={40} />
      <Skeleton height={64} />
    </div>
  );
}
