import { useCallback, useEffect, useState } from 'react';
import { useModelsCatalog, useQuota } from '@/api/queries';
import { resolveAnalysisModel } from '@/lib/run';
import {
  getProviderKeys,
  getRunDefaults,
  setProviderKey,
  setRunDefaults,
  type ProviderKeys,
  type RunDefaults,
} from '@/lib/storage';

export function useAnalysisModel() {
  const catalog = useModelsCatalog();
  const quota = useQuota();
  const [keys, setKeys] = useState<ProviderKeys>(() => getProviderKeys());
  const [defaults, setDefaults] = useState<RunDefaults>(() => getRunDefaults());
  const refresh = useCallback(() => {
    setKeys(getProviderKeys());
    setDefaults(getRunDefaults());
  }, []);
  useEffect(() => {
    window.addEventListener('storage', refresh);
    window.addEventListener('catalyst:keys-changed', refresh);
    return () => {
      window.removeEventListener('storage', refresh);
      window.removeEventListener('catalyst:keys-changed', refresh);
    };
  }, [refresh]);
  const setAnalysisModel = useCallback((provider: string, modelId: string, source: 'server' | 'own') => {
    const next = { ...getRunDefaults(), payer: source, preferred: { provider, modelId } };
    setRunDefaults(next);
    setDefaults(next);
  }, []);
  const providers = catalog.data?.providers?.filter((provider) => provider.executable) ?? [];
  const usableModels = providers.flatMap((provider) => {
    const isServer = Boolean(provider.env_key_configured) && quota.data?.per_caller?.limit !== 0;
    const hasKey = Boolean(keys[provider.id]?.key);
    return (provider.models ?? []).flatMap((model) => [
      ...(isServer ? [{ provider, model, source: 'server' as const, failed: false }] : []),
      ...(hasKey
        ? [
            {
              provider,
              model,
              source: 'own' as const,
              failed: Boolean(keys[provider.id]?.check && !keys[provider.id]?.check?.ok),
            },
          ]
        : []),
    ]);
  });
  const choice = resolveAnalysisModel(catalog.data, keys, defaults, quota.data?.per_caller?.limit !== 0);
  const updateDefaults = useCallback((next: RunDefaults) => {
    setRunDefaults(next);
    setDefaults(next);
  }, []);
  return { choice, keys, defaults, usableModels, setAnalysisModel, updateDefaults, catalog, quota };
}
