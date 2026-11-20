import { cn } from '@/lib/cn';
export interface SkeletonProps {
  width?: string | number;
  height?: number;
  className?: string;
}
export function Skeleton({ width = '100%', height = 12, className }: SkeletonProps) {
  return (
    <div
      aria-hidden="true"
      className={cn('animate-shimmer skeleton-shimmer rounded-sm', className)}
      style={{ width, height }}
    />
  );
}
