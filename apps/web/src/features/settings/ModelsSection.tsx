import type React from 'react';
import { useState } from 'react';
import { Eye, EyeOff } from 'lucide-react';
import { Button } from '@/components/ui';
import { useValidateKey } from '@/api/queries';
import { checkMessages, keyLinks } from './keyHelp';
import { useAnalysisModel } from '@/lib/useAnalysisModel';
import { getProviderKeys, getRunDefaults, setProviderKey, removeProviderKey } from '@/lib/storage';

export function ModelsSection(): React.JSX.Element {
  const { choice, keys, defaults, usableModels, setAnalysisModel, catalog, quota } = useAnalysisModel();
  // A provider with no listed models (the custom endpoint) needs a base URL this page does not ask for.
  const providers = catalog.data?.providers?.filter((item) => item.executable && item.models?.length) ?? [];
  const server = providers.find((item) => item.env_key_configured);
  const visitorBlocked = quota.data?.tier === 'anonymous' && quota.data.per_caller?.limit === 0;
  const [selected, setSelected] = useState('');
  const [key, setKey] = useState('');
  const [visible, setVisible] = useState(false);
  const [remember, setRemember] = useState(true);
  const [message, setMessage] = useState('');
  const validate = useValidateKey();
  // Until you pick one, show the provider the analyses use.
  const provider =
    providers.find((item) => item.id === (selected || choice?.provider)) ?? server ?? providers[0];
  const chosenModel =
    provider?.models?.find((item) => item.model_id === provider.default_model_id) ?? provider?.models?.[0];
  const own = Boolean(keys[provider?.id ?? '']?.key);
  const check = async () => {
    if (!provider || !chosenModel || !key.trim()) return;
    setMessage('');
    try {
      const result = await validate.mutateAsync({
        provider: provider.id,
        model_id: chosenModel.model_id,
        key: key.trim(),
      });
      const at = new Date().toISOString();
      if (result.ok) {
        // The first key you add becomes the one used for analyses; later keys do not override your choice.
        const firstOwnKey = !Object.values(getProviderKeys()).some((value) => Boolean(value?.key));
        setProviderKey(provider.id, { key: key.trim(), remember, check: { ok: true, at } });
        if (firstOwnKey) setAnalysisModel(provider.id, chosenModel.model_id, 'own');
        setMessage(`✓ Works · checked just now with ${chosenModel.model_id}`);
      } else {
        const failure = checkMessages[result.error_code ?? ''] ?? 'The provider rejected the key';
        setMessage(failure);
        const savedKey = keys[provider.id];
        if (savedKey?.key === key.trim())
          setProviderKey(provider.id, { ...savedKey, check: { ok: false, at, message: failure } });
      }
    } catch (error) {
      setMessage((error as { message?: string }).message ?? 'The provider rejected the key');
    }
  };
  const groups = [
    { label: 'SERVER', items: server ? [server] : [] },
    {
      label: 'YOUR KEYS',
      items: providers.filter((item) => Boolean(keys[item.id]?.key) && item.id !== server?.id),
    },
    { label: 'ADD A KEY', items: providers.filter((item) => !keys[item.id]?.key && item.id !== server?.id) },
  ];
  return (
    <section id="models" className="settings-card flex flex-col gap-0 overflow-hidden">
      <div className="border-b border-line-100 px-5 py-4">
        <h2 className="type-heading m-0">Models and keys</h2>
        <p className="m-0 type-body-sm text-ink-200">
          Analyses use ·{' '}
          {choice
            ? `${choice.providerLabel} · ${choice.modelId} · ${choice.credential === 'browser_key' ? 'your key' : 'server key'}`
            : 'No model available'}
        </p>
      </div>
      <div className="models-layout">
        <nav className="models-provider-list" aria-label="Providers">
          {groups.map(
            (group) =>
              group.items.length > 0 && (
                <div key={group.label}>
                  <p className="type-label px-3 text-ink-300">{group.label}</p>
                  {group.items.map((item) => (
                    <button
                      key={item.id}
                      type="button"
                      disabled={item.id === server?.id && visitorBlocked}
                      className={`models-provider ${provider?.id === item.id ? 'is-selected' : ''}`}
                      onClick={() => {
                        setSelected(item.id);
                        setMessage('');
                      }}
                    >
                      <span
                        className={`models-dot ${item.id === server?.id ? 'is-good' : keys[item.id]?.check?.ok ? 'is-good' : keys[item.id]?.check?.ok === false ? 'is-bad' : ''}`}
                      />
                      <span>
                        {item.label}
                        {item.id === server?.id && visitorBlocked && (
                          <small className="block">
                            Not for visitors. Featured cases open without a key; to explain another day, use
                            your own key.
                          </small>
                        )}
                      </span>
                      <small>
                        {item.id === server?.id
                          ? 'server key'
                          : keys[item.id]?.check?.ok
                            ? 'works'
                            : keys[item.id]?.check?.ok === false
                              ? 'rejected'
                              : keys[item.id]?.key
                                ? 'not checked'
                                : ''}
                      </small>
                    </button>
                  ))}
                </div>
              ),
          )}
        </nav>
        <div className="models-detail">
          <div className="flex items-center justify-between">
            <h3 className="type-heading m-0">{provider?.label}</h3>
            {provider && keyLinks[provider.id] && (
              <a className="text-info" href={keyLinks[provider.id]} target="_blank" rel="noopener noreferrer">
                Get a {provider.label} key ↗
              </a>
            )}
          </div>
          {provider?.id === server?.id ? (
            <p className="type-body-sm text-ink-200">
              Uses the key configured on the server
              {quota.data?.tier === 'admin'
                ? ' · Your key from the server settings · admin runs have no daily limit'
                : ''}
            </p>
          ) : (
            <>
              <label className="flex flex-col gap-1 type-caption text-ink-200">
                API key
                <span className="flex gap-2">
                  <input
                    className="settings-input flex-1"
                    type={visible ? 'text' : 'password'}
                    value={key}
                    onChange={(event) => setKey(event.target.value)}
                    autoComplete="new-password"
                  />
                  <button
                    type="button"
                    className="settings-link"
                    aria-label={visible ? 'Hide key' : 'Show key'}
                    onClick={() => setVisible(!visible)}
                  >
                    {visible ? <EyeOff size={16} /> : <Eye size={16} />}
                  </button>
                  <Button
                    size="sm"
                    variant="secondary"
                    disabled={!key.trim() || validate.isPending}
                    onClick={() => void check()}
                  >
                    {validate.isPending ? 'Checking…' : 'Check key'}
                  </Button>
                </span>
              </label>
              <div className="flex items-center gap-3 type-caption text-ink-300">
                <label>
                  <input type="radio" checked={remember} onChange={() => setRemember(true)} /> in this browser
                </label>
                <label>
                  <input type="radio" checked={!remember} onChange={() => setRemember(false)} /> only this tab
                </label>
                {own && (
                  <button
                    className="settings-link ml-auto text-down"
                    type="button"
                    onClick={() => removeProviderKey(provider!.id)}
                  >
                    Remove key
                  </button>
                )}
              </div>
              {message && (
                <p className={`m-0 type-caption ${message.startsWith('✓') ? 'text-up' : 'text-down'}`}>
                  {message}
                </p>
              )}
            </>
          )}
          <p className="type-label text-ink-300">Models</p>
          <div className="rounded-md border border-line-100">
            {provider?.models?.map((item) => {
              const active = choice?.provider === provider.id && choice.modelId === item.model_id;
              const enabled = provider.id === server?.id || own;
              return (
                <div key={item.model_id} className="models-model-row">
                  <span className="font-numeric">{item.model_id}</span>
                  {active ? (
                    <span className="type-caption text-accent">Used for analyses</span>
                  ) : (
                    <button
                      type="button"
                      className="settings-link"
                      disabled={!enabled}
                      onClick={() =>
                        setAnalysisModel(
                          provider.id,
                          item.model_id,
                          provider.id === server?.id ? 'server' : 'own',
                        )
                      }
                    >
                      {enabled ? 'Use for analyses' : 'Add a key first'}
                    </button>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </section>
  );
}
