import type React from 'react';
import { useEffect, useState } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { Button, Select } from '@/components/ui';
import { useValidateKey } from '@/api/queries';
import type { components } from '@/api/schema';
import { useAnalysisModel } from '@/lib/useAnalysisModel';
import { getProviderKeys, getRunDefaults, setProviderKey } from '@/lib/storage';
import { formatDay } from '@/lib/format';
import { DEFAULT_MAX_TOKENS, formatTokens } from '@/lib/run';
import { checkMessages, keyLinks } from '@/features/settings/keyHelp';

type CatalogProvider = components['schemas']['CatalogProvider'];
type Mode = 'browser_key' | 'server_env';
export type RunDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  ticker: string;
  date: string;
  estimate: number | null;
  averageSeconds?: number | null;
  providers: CatalogProvider[];
  quota?: components['schemas']['QuotaResponse'];
  defaultProvider?: string;
  payer?: 'server' | 'own';
  openOnOwnKey?: boolean;
  browserKeys?: Record<string, { key: string; remembered: boolean }>;
  costCapUsd?: number;
  onSubmit: (choice: {
    mode: Mode;
    provider: CatalogProvider;
    modelId: string;
    key?: string;
    remember?: boolean;
  }) => void;
};

export function RunDialog({
  open,
  onOpenChange,
  ticker,
  date,
  estimate,
  averageSeconds,
  providers,
  defaultProvider = '',
  openOnOwnKey = false,
  costCapUsd = 0.2,
  onSubmit,
}: RunDialogProps): React.JSX.Element {
  const { usableModels, choice, setAnalysisModel } = useAnalysisModel();
  const validate = useValidateKey();
  const [step, setStep] = useState<'confirm' | 'add'>('confirm');
  const [selected, setSelected] = useState('');
  const [providerId, setProviderId] = useState(defaultProvider);
  const [modelId, setModelId] = useState('');
  const [key, setKey] = useState('');
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);
  const [remember, setRemember] = useState(true);
  useEffect(() => {
    if (!open) {
      setStep('confirm');
      setSaved(false);
      setKey('');
      setError('');
      return;
    }
    setSelected(
      choice
        ? `${choice.provider}:${choice.modelId}:${choice.credential === 'browser_key' ? 'own' : 'server'}`
        : '',
    );
    if (openOnOwnKey || usableModels.length === 0) setStep('add');
  }, [open, openOnOwnKey, usableModels.length, choice?.provider, choice?.modelId]);
  // A provider with no listed models (the custom endpoint) needs a base URL this dialog does not ask for.
  const executable = providers.filter((item) => item.executable && item.models?.length);
  const provider = executable.find((item) => item.id === providerId) ?? executable[0];
  const model =
    provider?.models?.find((item) => item.model_id === modelId) ??
    (provider ? provider.models?.find((item) => item.model_id === provider.default_model_id) : undefined) ??
    provider?.models?.[0];
  const selectedModel = usableModels.find(
    (item) => `${item.provider.id}:${item.model.model_id}:${item.source}` === selected,
  );
  const saveKey = async () => {
    if (!provider || !model || !key.trim()) return;
    setError('');
    try {
      const result = await validate.mutateAsync({
        provider: provider.id,
        model_id: model.model_id,
        key: key.trim(),
      });
      if (!result.ok) {
        setError(checkMessages[result.error_code ?? ''] ?? 'The provider rejected the key');
        return;
      }
      const firstOwnKey = !Object.values(getProviderKeys()).some((value) => Boolean(value?.key));
      setProviderKey(provider.id, {
        key: key.trim(),
        remember,
        check: { ok: true, at: new Date().toISOString() },
      });
      if (firstOwnKey) setAnalysisModel(provider.id, model.model_id, 'own');
      setSaved(true);
      setSelected(`${provider.id}:${model.model_id}:own`);
      setError(`✓ Works · saved in this browser`);
    } catch {
      setError('The provider rejected the key');
    }
  };
  const explain = () => {
    if (step === 'add') {
      if (!saved || !provider || !model) return;
      onSubmit({
        mode: 'browser_key',
        provider,
        modelId: model.model_id,
        key: getProviderKeys()[provider.id]?.key,
        remember,
      });
      return;
    }
    if (!selectedModel) return;
    onSubmit({
      mode: selectedModel.source === 'server' ? 'server_env' : 'browser_key',
      provider: selectedModel.provider,
      modelId: selectedModel.model.model_id,
      key: selectedModel.source === 'own' ? getProviderKeys()[selectedModel.provider.id]?.key : undefined,
      remember: getProviderKeys()[selectedModel.provider.id]?.remember,
    });
  };
  return (
    <Dialog.Root open={open && Boolean(date)} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-bg-000/70" />
        <Dialog.Content className="run-dialog z-50 flex flex-col gap-4 overflow-auto rounded-lg border border-line-200 bg-bg-200 p-6 shadow-pop">
          {step === 'confirm' ? (
            <>
              <Dialog.Title className="type-title m-0">
                Explain {ticker} on {formatDay(date)}
              </Dialog.Title>
              <Dialog.Description className="type-body-sm text-ink-200">
                Reads that day&apos;s news and filings up to the close.
              </Dialog.Description>
              <div className="flex flex-col border-t border-line-100">
                <div className="settings-dialog-row">
                  <span className="type-caption text-ink-300">Model</span>
                  <Select value={selected} onChange={(event) => setSelected(event.target.value)}>
                    {usableModels.map(({ provider: item, model: itemModel, source }) => (
                      <option
                        key={`${item.id}:${itemModel.model_id}:${source}`}
                        value={`${item.id}:${itemModel.model_id}:${source}`}
                      >
                        {item.label} · {itemModel.model_id} ·{' '}
                        {source === 'server' ? 'server key' : 'your key'}
                      </option>
                    ))}
                  </Select>
                  <span />
                </div>
                <div className="settings-dialog-row">
                  <span className="type-caption text-ink-300">Cost</span>
                  <span>
                    {selectedModel?.source === 'own'
                      ? `billed by your provider · stops before ${formatTokens(getRunDefaults().maxTokens ?? DEFAULT_MAX_TOKENS)} tokens`
                      : `${estimate == null ? 'Estimate unavailable' : `about $${estimate.toFixed(2)}`} · stops before $${costCapUsd.toFixed(2)}`}
                  </span>
                  <span />
                </div>
                {averageSeconds != null && (
                  <div className="settings-dialog-row">
                    <span className="type-caption text-ink-300">Takes</span>
                    <span>about {Math.round(averageSeconds)} s</span>
                    <span />
                  </div>
                )}
              </div>
              <div className="flex items-center gap-2">
                <button className="settings-link" type="button" onClick={() => setStep('add')}>
                  Add or manage keys
                </button>
                <span className="flex-1" />
                <Button variant="ghost" onClick={() => onOpenChange(false)}>
                  Cancel
                </Button>
                <Button variant="primary" disabled={!selectedModel} onClick={explain}>
                  Explain
                </Button>
              </div>
            </>
          ) : (
            <>
              <Dialog.Title className="type-title m-0">Add a key</Dialog.Title>
              <div className="grid grid-cols-2 gap-3">
                <label className="flex flex-col gap-1 type-caption text-ink-200">
                  Provider
                  <Select
                    value={provider?.id ?? ''}
                    onChange={(event) => {
                      setProviderId(event.target.value);
                      setModelId('');
                      setSaved(false);
                    }}
                  >
                    {executable.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.label}
                      </option>
                    ))}
                  </Select>
                </label>
                <label className="flex flex-col gap-1 type-caption text-ink-200">
                  Model
                  <Select
                    value={model?.model_id ?? ''}
                    onChange={(event) => {
                      setModelId(event.target.value);
                      setSaved(false);
                    }}
                  >
                    {provider?.models?.map((item) => (
                      <option key={item.model_id} value={item.model_id}>
                        {item.label}
                      </option>
                    ))}
                  </Select>
                </label>
              </div>
              <label className="flex flex-col gap-1 type-caption text-ink-200">
                <span>
                  API key
                  {provider && keyLinks[provider.id] && (
                    <>
                      {' · '}
                      <a
                        className="text-info"
                        href={keyLinks[provider.id]}
                        target="_blank"
                        rel="noopener noreferrer"
                      >
                        get one ↗
                      </a>
                    </>
                  )}
                </span>
                <input
                  className="settings-input"
                  type="password"
                  value={key}
                  onChange={(event) => {
                    setKey(event.target.value);
                    setSaved(false);
                  }}
                  autoComplete="new-password"
                />
              </label>
              <label className="type-caption text-ink-200">
                <input
                  type="checkbox"
                  checked={remember}
                  onChange={(event) => setRemember(event.target.checked)}
                />{' '}
                Keep it in this browser
              </label>
              {error && (
                <p className={`m-0 type-caption ${error.startsWith('✓') ? 'text-up' : 'text-down'}`}>
                  {error}
                </p>
              )}
              <div className="flex justify-end gap-2">
                <Button
                  variant="secondary"
                  disabled={!key.trim() || validate.isPending}
                  onClick={() => void saveKey()}
                >
                  {validate.isPending ? 'Checking…' : 'Check and save'}
                </Button>
                <Button variant="ghost" onClick={() => setStep('confirm')}>
                  Back
                </Button>
                <Button variant="primary" disabled={!saved} onClick={explain}>
                  Use it and explain
                </Button>
              </div>
            </>
          )}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
