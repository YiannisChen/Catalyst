/**
 * ABSTAIN UX helpers (M6-11, AGENT-01).
 *
 * For an ABSTAIN result the Workbench renders "no accepted cause" plus the
 * supplied gaps/limitations and never displays attribution_type as a causal or
 * no-material badge. The type badge is user-visible only for PARTIAL or
 * SUFFICIENT results.
 */
export const NO_ACCEPTED_CAUSE_COPY = 'No accepted cause identified.';

export function shouldShowAttributionBadge(attributionStatus: string | null | undefined): boolean {
  return attributionStatus === 'PARTIAL' || attributionStatus === 'SUFFICIENT';
}

export function isAbstain(attributionStatus: string | null | undefined): boolean {
  return attributionStatus === 'ABSTAIN';
}

export function abstainCopy(
  attributionStatus: string | null | undefined,
  limitations: string[],
): string | null {
  if (!isAbstain(attributionStatus)) return null;
  const parts = [NO_ACCEPTED_CAUSE_COPY, ...limitations.map((l) => `• ${l}`)]
  return parts.join('\n')
}
