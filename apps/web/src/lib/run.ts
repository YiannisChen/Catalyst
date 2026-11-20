import type { components } from '@/api/schema';
import type { ProviderKeys, RunDefaults } from './storage';

export const DEFAULT_COST_CAP_USD = 0.2;
/** Own-key runs are bounded by tokens: any model works, priced or not. */
export const DEFAULT_MAX_TOKENS = 300_000;
export const MIN_MAX_TOKENS = 150_000; // below this the largest single step (about 136k) cannot run
export const MAX_MAX_TOKENS = 2_000_000;
export function formatTokens(value: number): string {
  return value >= 1_000_000 ? `${(value / 1_000_000).toFixed(1)}M` : `${Math.round(value / 1000)}k`;
}

// The cost title explains when the reserved upper bound was booked.
export function costLabel(
  cost: number | null | undefined,
  method: string | null | undefined,
  digits = 3,
): string {
  if (cost == null) return '—';
  return `$${cost.toFixed(digits)}`;
}

export function costTitle(method: string | null | undefined): string | undefined {
  return method === 'upper_bound_charged'
    ? 'No usage came back from the provider, so the reserved upper bound was booked'
    : undefined;
}

// Short reason for a failed or stopped run, from the API's failure_code.
const FAILURE_LABELS: Record<string, string> = {
  TIMEOUT: 'Timed out at the run time limit',
  BUDGET_EXCEEDED: 'Hit the cost cap',
  MODEL_PRICE_UNAVAILABLE: 'No price listed for this model',
  PROVIDER_UNAVAILABLE: 'Model provider unavailable',
  DATA_UNAVAILABLE: 'Data for this day unavailable',
  CAPACITY_EXCEEDED: 'Server busy; try again',
  EXECUTOR_SHUTDOWN: 'Server restarted during the run',
  SYSTEM_ERROR: 'Internal error',
};
export function failureLabel(status: string, code: string | null | undefined): string {
  if (status === 'CANCELLED') return 'Stopped before it finished';
  if (!code) return 'Stopped with an error';
  return (
    FAILURE_LABELS[code] ??
    code
      .toLowerCase()
      .replace(/_/g, ' ')
      .replace(/^\w/, (c) => c.toUpperCase())
  );
}

export type RunModelChoice = {
  provider: string;
  providerLabel: string;
  modelId: string;
  credential: 'browser_key' | 'server_env' | 'none';
  keyHint: string | null;
  key?: string;
};

export function resolveAnalysisModel(
  catalog: components['schemas']['ModelCatalogResponse'] | undefined,
  providerKeys: ProviderKeys,
  defaults: RunDefaults,
  serverUsable = true, // false for a visitor who may not use the server key
): RunModelChoice | null {
  const providers = catalog?.providers?.filter((provider) => provider.executable) ?? [];
  const preferred = defaults.preferred;
  const own =
    preferred &&
    providers.find((provider) => provider.id === preferred.provider && providerKeys[provider.id]?.key);
  const server = serverUsable ? providers.find((provider) => provider.env_key_configured) : undefined;
  const fallbackOwn = providers.find((provider) => providerKeys[provider.id]?.key);
  const provider = defaults.payer === 'own' && own ? own : (server ?? fallbackOwn);
  if (!provider) return null;
  const key = providerKeys[provider.id]?.key;
  const modelId =
    provider.id === preferred?.provider &&
    provider.models?.some((model) => model.model_id === preferred.modelId)
      ? preferred.modelId
      : (provider.default_model_id ?? provider.models?.[0]?.model_id ?? '');
  const useServer = provider === server && !(defaults.payer === 'own' && own);
  return {
    provider: provider.id,
    providerLabel: provider.label,
    modelId,
    credential: useServer ? 'server_env' : 'browser_key',
    keyHint: useServer ? null : (key?.slice(-4) ?? null),
    ...(!useServer && key ? { key } : {}),
  };
}

export function buildCreateRunRequest(
  ticker: string,
  date: string,
  choice: RunModelChoice,
  capUsd = DEFAULT_COST_CAP_USD,
  maxTokens = DEFAULT_MAX_TOKENS,
): components['schemas']['CreateRunRequest'] {
  const model: components['schemas']['ModelConfig'] | undefined =
    choice.credential === 'browser_key'
      ? {
          provider: choice.provider,
          model_id: choice.modelId,
          api_key: choice.key ?? '',
          credential_source: 'browser_key',
        }
      : choice.credential === 'server_env'
        ? {
            provider: choice.provider,
            model_id: choice.modelId,
            api_key: '',
            credential_source: 'server_env',
          }
        : undefined;
  return {
    ticker,
    trade_date: date,
    config: 'v1.1',
    force_full: false,
    ...(model ? { model } : {}),
    ...(choice.credential === 'browser_key'
      ? { max_tokens: Math.round(Math.max(MIN_MAX_TOKENS, Math.min(MAX_MAX_TOKENS, maxTokens))) }
      : { max_cost_usd: Math.max(0.05, Math.min(2, capUsd)) }),
  };
}

/** Display text for the model block: "provider · model" and where the key comes from. */
export function describeRunModel(choice: RunModelChoice | null): { line: string; source: string } {
  if (!choice) return { line: '—', source: 'no model available' };
  const source =
    choice.credential === 'browser_key'
      ? 'your key, saved in this browser'
      : choice.credential === 'server_env'
        ? 'server key'
        : 'no key yet';
  return { line: `${choice.provider} · ${choice.modelId}`, source };
}
