export function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null || !Number.isFinite(bytes)) return '—';
  if (bytes >= 1_000_000_000) return `${(bytes / 1_000_000_000).toFixed(1)} GB`;
  if (bytes >= 1_000_000) return `${Math.round(bytes / 1_000_000)} MB`;
  return `${Math.max(1, Math.round(bytes / 1_000))} KB`;
}

// Job failures carry raw exception text; drop the exception class name for display.
export function plainError(message: string | null | undefined): string {
  if (!message) return '—';
  return message.replace(/^[A-Z][A-Za-z]*(?:Error|Exception):\s*/, '');
}
