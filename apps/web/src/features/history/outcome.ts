import type { components } from '@/api/schema';

type RunHistoryItem = components['schemas']['RunHistoryItem'];

export type HistoryOutcome = {
  label: string;
  tone: 'up' | 'warn' | 'neutral' | 'down' | 'info';
};

export function productTierOutcome(tier: string | null | undefined): HistoryOutcome | null {
  switch (tier) {
    case 'CONFIRMED':
      return { label: 'Confirmed', tone: 'up' };
    case 'REPORTED':
      return { label: 'Reported', tone: 'info' };
    case 'MOVED_WITH_GROUP':
      return { label: 'With its group', tone: 'info' };
    case 'LARGER_THAN_GROUP':
    case 'NOTHING_FOUND':
      return { label: 'No clear cause', tone: 'warn' };
    case 'ORDINARY_MOVE':
      return { label: 'Ordinary move', tone: 'neutral' };
    default:
      return null;
  }
}

export function historyOutcome(item: RunHistoryItem): HistoryOutcome {
  if (item.lifecycle_status === 'FAILED') return { label: 'Failed', tone: 'down' };
  if (item.lifecycle_status === 'CANCELLED') return { label: 'Stopped', tone: 'neutral' };
  if (
    item.lifecycle_status === 'RUNNING' ||
    item.lifecycle_status === 'ACCEPTED' ||
    item.lifecycle_status === 'CANCEL_REQUESTED'
  ) {
    return { label: 'Running', tone: 'info' };
  }
  const tierOutcome = productTierOutcome(item.product_tier);
  if (tierOutcome) return tierOutcome;
  switch (item.outcome) {
    case 'ANSWERED':
      return { label: 'Confirmed', tone: 'up' };
    case 'ABSTAINED':
      return { label: 'No clear cause', tone: 'warn' };
    case 'TIER_ONLY':
      return { label: 'Ordinary move', tone: 'neutral' };
    default:
      return { label: 'Partial answer', tone: 'info' };
  }
}
