/**
 * Sensitive-field redaction for Catalyst workbench.
 *
 * Does not mutate input. Exports pure utility functions usable both
 * in production TraceTab and in unit tests without code duplication.
 */

/* ── Sensitive key detection ── */

export function normalizeSensitiveKey(key: string): string {
  return key.toLowerCase().replace(/[_\-\s]/g, '');
}

const SENSITIVE_NORMALIZED = new Set([
  'apikey',
  'authorization',
  'cookie',
  'password',
  'secret',
  'systemprompt',
  'token',
  'accesstoken',
  'authtoken',
  'bearer',
  'refreshtoken',
  'clientsecret',
  'privatekey',
]);

/* ── Bearer token patterns in plain text ── */

const BEARER_TOKEN_RE = /\bbearer\s+\S+/gi;

function redactBearerTokens(text: string): string {
  return text.replace(BEARER_TOKEN_RE, '[REDACTED]');
}

/* ── Public API ── */

/**
 * Recursively redact sensitive fields from an arbitrary value.
 *
 * - Object keys whose normalised form matches SENSITIVE_NORMALIZED
 *   are replaced with the string "[REDACTED]".
 * - String values that are valid JSON are parsed, recursively
 *   redacted, and returned as the parsed (non-string) form so that
 *   callers such as JSON.stringify() serialise the redacted structure.
 * - Plain-text strings are scanned for Bearer-token patterns
 *   (case-insensitive "bearer <token>") and the token portion is
 *   replaced while preserving surrounding text.
 * - Arrays are recursed element-wise.
 * - The original object graph is never mutated.
 *
 * @returns A structurally-equivalent deep copy with sensitive data removed.
 */
export function redactPayload(obj: unknown): unknown {
  if (obj === null || obj === undefined) {
    return obj;
  }

  if (typeof obj === 'string') {
    return redactString(obj);
  }

  if (Array.isArray(obj)) {
    return obj.map(redactPayload);
  }

  if (typeof obj === 'object') {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(obj as Record<string, unknown>)) {
      if (SENSITIVE_NORMALIZED.has(normalizeSensitiveKey(k))) {
        out[k] = '[REDACTED]';
      } else {
        out[k] = redactPayload(v);
      }
    }
    return out;
  }

  return obj;
}

/* ── String handler ── */

function redactString(value: string): unknown {
  const trimmed = value.trim();

  // 1. Try JSON parse → recursive redaction → return structured form
  if (
    (trimmed.startsWith('{') && trimmed.endsWith('}')) ||
    (trimmed.startsWith('[') && trimmed.endsWith(']'))
  ) {
    try {
      const parsed = JSON.parse(trimmed);
      return redactPayload(parsed);
    } catch {
      // Not valid JSON — fall through to plain-text handling
    }
  }

  // 2. Plain text: scan for Bearer tokens inline
  return redactBearerTokens(value);
}
