import { useState, useCallback, useEffect, useMemo, useRef, type FormEvent } from 'react';
import { createPortal } from 'react-dom';
import { getCatalog, validateModel } from '../../../api/client';
import type {
  ModelConfig,
  ModelValidateResponse,
  CatalogProvider,
} from '../../../api/types';

export type ConnectionStatus = 'not_configured' | 'testing' | 'connected' | 'invalid';
export type ModalVariant = 'a' | 'b' | 'c';

export interface ModelSettingsProps {
  onModelReady: (config: ModelConfig) => void;
  onModelClear: () => void;
  /** Glance-test variant for the modal panel. Defaults to 'b' (light solid). */
  variant?: ModalVariant;
}

function loadSavedConfig(): Partial<ModelConfig> | null {
  try {
    const raw = localStorage.getItem('catalyst_model_config');
    if (raw) return JSON.parse(raw) as Partial<ModelConfig>;
  } catch { /* ignore */ }
  return null;
}

function saveConfig(config: ModelConfig) {
  try {
    localStorage.setItem('catalyst_model_config', JSON.stringify(config));
  } catch { /* ignore */ }
}

function clearSavedConfig() {
  try {
    localStorage.removeItem('catalyst_model_config');
  } catch { /* ignore */ }
}

function resolveVariant(): ModalVariant {
  if (typeof window !== 'undefined') {
    const v = new URLSearchParams(window.location.search).get('modal-variant');
    if (v === 'a' || v === 'b' || v === 'c') return v;
  }
  return 'b';
}

export default function ModelSettings({ onModelReady, onModelClear, variant }: ModelSettingsProps) {
  const effectiveVariant = variant ?? resolveVariant();
  const [catalog, setCatalog] = useState<CatalogProvider[] | null>(null);
  const [catalogError, setCatalogError] = useState<string | null>(null);

  const [selectedProviderId, setSelectedProviderId] = useState('aihubmix');
  const [selectedModelId, setSelectedModelId] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [customModelId, setCustomModelId] = useState('');
  const [remember, setRemember] = useState(false);
  const [showKey, setShowKey] = useState(false);

  const [connectionStatus, setConnectionStatus] = useState<ConnectionStatus>('not_configured');
  const [modelReady, setModelReady] = useState(false);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [latencyMs, setLatencyMs] = useState<number | null>(null);

  const [modalOpen, setModalOpen] = useState(false);

  const triggerRef = useRef<HTMLButtonElement>(null);
  const firstInputRef = useRef<HTMLSelectElement>(null);

  // ── Fetch catalog on mount ──
  useEffect(() => {
    getCatalog()
      .then((res) => setCatalog(res.providers))
      .catch((err) =>
        setCatalogError(err instanceof Error ? err.message : 'Failed to load model catalog'),
      );
  }, []);

  // ── Restore saved config ──
  useEffect(() => {
    const saved = loadSavedConfig();
    if (saved) {
      if (saved.provider) setSelectedProviderId(saved.provider);
      if (saved.model_id) setSelectedModelId(saved.model_id);
      if (saved.api_key) setApiKey(saved.api_key);
      if (saved.base_url) setBaseUrl(saved.base_url ?? '');
      setRemember(true);
    }
  }, []);

  // ── Derived ──
  const selectedProvider = useMemo(
    () => catalog?.find((p) => p.id === selectedProviderId) ?? null,
    [catalog, selectedProviderId],
  );

  const isCustom = selectedProviderId === 'custom_openai_compatible';

  // Set default model when provider changes
  useEffect(() => {
    if (!selectedProvider || isCustom) {
      if (isCustom) setSelectedModelId('');
      return;
    }
    const defaultModel = selectedProvider.default_model_id
      ?? selectedProvider.models.find((m) => m.recommended)?.id
      ?? selectedProvider.models[0]?.id
      ?? '';
    setSelectedModelId(defaultModel);
  }, [selectedProvider, isCustom]);

  // ── Reset connection/modelReady when sensitive inputs change ──
  useEffect(() => {
    setConnectionStatus('not_configured');
    setModelReady(false);
    setStatusMessage(null);
    onModelClear();
  }, [selectedProviderId, apiKey, baseUrl, customModelId, onModelClear]);
  // Note: selectedModelId changes do NOT reset — user can switch models after connection.

  const canTest =
    connectionStatus !== 'testing' &&
    selectedProviderId.length > 0 &&
    apiKey.trim().length > 0 &&
    (isCustom
      ? baseUrl.trim().length > 0 && customModelId.trim().length > 0
      : true);

  // ── Handlers ──
  const handleTestConnection = useCallback(
    async (e: FormEvent) => {
      e.preventDefault();
      if (!canTest) return;

      const probeModelId = isCustom
        ? customModelId.trim()
        : (selectedProvider?.default_model_id ?? selectedModelId);
      const effectiveBaseUrl = isCustom ? baseUrl.trim() : undefined;

      setConnectionStatus('testing');
      setStatusMessage('Testing connection…');
      setLatencyMs(null);

      try {
        const result: ModelValidateResponse = await validateModel({
          provider: selectedProviderId,
          model_id: probeModelId,
          api_key: apiKey.trim(),
          base_url: effectiveBaseUrl ?? null,
          credential_source: 'browser_key',
        });

        if (result.ok) {
          setConnectionStatus('connected');
          setStatusMessage(null);
          setLatencyMs(result.latency_ms ?? null);
        } else {
          setConnectionStatus('invalid');
          setStatusMessage(result.message ?? 'Connection test failed.');
          setModelReady(false);
        }
      } catch (err) {
        setConnectionStatus('invalid');
        setStatusMessage(err instanceof Error ? err.message : 'Connection test failed.');
        setModelReady(false);
      }
    },
    [canTest, isCustom, selectedProvider, selectedModelId, customModelId, apiKey, baseUrl],
  );

  // ── "Use Model" – applies to all providers ──
  const handleUseModel = useCallback(() => {
    const modelId = isCustom ? customModelId.trim() : selectedModelId;
    if (!modelId || !selectedProviderId || !apiKey.trim()) return;

    setModelReady(true);
    const config: ModelConfig = {
      provider: selectedProviderId,
      model_id: modelId,
      api_key: apiKey.trim(),
      base_url: isCustom ? baseUrl.trim() : undefined,
      credential_source: 'browser_key',
    };
    if (remember) saveConfig(config);
    onModelReady(config);
    setModalOpen(false);
  }, [isCustom, selectedModelId, customModelId, selectedProviderId, apiKey, baseUrl, remember, onModelReady]);

  // ── Remember toggle handler ──
  const handleRememberToggle = useCallback(() => {
    setRemember((prev) => {
      if (prev) clearSavedConfig();
      return !prev;
    });
  }, []);

  // ── Clear config ──
  const handleClearConfig = useCallback(() => {
    setConnectionStatus('not_configured');
    setModelReady(false);
    setStatusMessage(null);
    setLatencyMs(null);
    clearSavedConfig();
    onModelClear();
    setModalOpen(false);
  }, [onModelClear]);

  // ── Modal open/close ──
  const openModal = useCallback(() => {
    setModalOpen(true);
  }, []);

  const closeModal = useCallback(() => {
    setModalOpen(false);
  }, []);

  // ── Focus management ──
  useEffect(() => {
    if (modalOpen && firstInputRef.current) {
      // Small delay to allow the portal to mount
      const id = setTimeout(() => firstInputRef.current?.focus(), 50);
      return () => clearTimeout(id);
    } else if (!modalOpen && triggerRef.current) {
      triggerRef.current.focus();
    }
  }, [modalOpen]);

  // Body scroll lock while modal is open
  useEffect(() => { if (modalOpen) { const prev = document.body.style.overflow; document.body.style.overflow = 'hidden'; return () => { document.body.style.overflow = prev; }; } }, [modalOpen]);

  // Trap focus within modal
  const panelRef = useRef<HTMLDivElement>(null);

  const handleModalKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'Escape') {
      }

      if (e.key === 'Tab' && panelRef.current) {
        const focusable = panelRef.current.querySelectorAll<HTMLElement>(
          'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
        );
        if (focusable.length === 0) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (e.shiftKey) {
          if (document.activeElement === first) { e.preventDefault(); last.focus(); }
        } else {
          if (document.activeElement === last) { e.preventDefault(); first.focus(); }
        }
        e.preventDefault();
        closeModal();
      }
    },
    [closeModal],
  );

  // Backdrop click
  const handleBackdropClick = useCallback(
    (e: React.MouseEvent) => {
      if (e.target === e.currentTarget) closeModal();
    },
    [closeModal],
  );

  // ── Trigger label ──
  const triggerLabel = modelReady
    ? `${selectedProvider?.label ?? selectedProviderId} \u00b7 ${isCustom ? customModelId : selectedModelId} \u00b7 Ready`
    : connectionStatus === 'testing'
      ? 'Testing\u2026'
      : connectionStatus === 'invalid'
        ? 'Error / Needs attention'
        : 'Model Settings';

  const triggerStatus = modelReady
    ? 'ready'
    : connectionStatus === 'testing'
      ? 'testing'
      : connectionStatus === 'invalid'
        ? 'invalid'
        : 'not_configured';

  // ── Modal content ──
  const modal = modalOpen && createPortal(
    <div
      className="v4-modal-backdrop"
      onClick={handleBackdropClick}
      onKeyDown={handleModalKeyDown}
    >
      <div
        ref={panelRef} className="v4-modal-panel"
        data-variant={effectiveVariant}
        role="dialog"
        aria-modal="true"
        aria-labelledby="v4-modal-title"
      >
        <div className="v4-modal-header">
          <div>
            <h2 id="v4-modal-title" className="v4-modal-title">Model Settings</h2>
            <p className="v4-modal-subtitle">Connect your provider key and choose a model.</p>
          </div>
          <button
            type="button"
            className="v4-modal-close"
            onClick={closeModal}
            aria-label="Close model settings"
          >
            &times;
          </button>
        </div>

        <div className="v4-modal-body">
          {catalogError && (
            <div className="v4-modal-error-banner" role="alert">{catalogError}</div>
          )}

          {catalog === null && !catalogError ? (
            <div className="v4-modal-loading">Loading providers\u2026</div>
          ) : catalog && catalog.length > 0 ? (
            <form onSubmit={handleTestConnection} className="v4-modal-form">
              {/* Provider select */}
              <div className="v4-model-select-group">
                <label htmlFor="v4-provider-select">Provider</label>
                <select
                  id="v4-provider-select"
                  ref={firstInputRef}
                  value={selectedProviderId}
                  onChange={(e) => setSelectedProviderId(e.target.value)}
                  className="v4-select"
                >
                  {catalog.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.label}
                    </option>
                  ))}
                </select>
              </div>

              {/* Custom: Base URL */}
              {isCustom && (
                <div className="v4-model-select-group">
                  <label htmlFor="v4-base-url">Base URL</label>
                  <input
                    id="v4-base-url"
                    type="text"
                    value={baseUrl}
                    onChange={(e) => setBaseUrl(e.target.value)}
                    placeholder="https://api.example.com/v1"
                  />
                </div>
              )}

              {/* API Key — always visible */}
              <div className="v4-model-field">
                <label htmlFor="v4-api-key">API Key</label>
                <div className="v4-api-key-wrap">
                  <input
                    id="v4-api-key"
                    type={showKey ? 'text' : 'password'}
                    value={apiKey}
                    onChange={(e) => setApiKey(e.target.value)}
                    placeholder="sk-\u2026"
                    autoComplete="off"
                  />
                  <button
                    type="button"
                    className="v4-key-toggle"
                    onClick={() => setShowKey((v) => !v)}
                    aria-label={showKey ? 'Hide API key' : 'Show API key'}
                  >
                    {showKey ? 'Hide' : 'Show'}
                  </button>
                </div>
                <p className="v4-model-helper">
                  {remember
                    ? 'Your key is saved in this browser\u2019s localStorage.'
                    : 'Your key is stored in browser memory only.'}
                </p>
              </div>

              <label className="v4-model-remember">
                <input
                  type="checkbox"
                  checked={remember}
                  onChange={handleRememberToggle}
                />
                <span>Remember API key in this browser</span>
              </label>

              {/* Custom: Model ID */}
              {isCustom && (
                <div className="v4-model-select-group">
                  <label htmlFor="v4-custom-model">Model ID</label>
                  <input
                    id="v4-custom-model"
                    type="text"
                    value={customModelId}
                    onChange={(e) => setCustomModelId(e.target.value)}
                    placeholder="model-id"
                    autoComplete="off"
                  />
                </div>
              )}

              {/* Test Connection button */}
              <div className="v4-model-actions">
                <button
                  type="submit"
                  className="v4-test-btn"
                  disabled={!canTest}
                >
                  {connectionStatus === 'testing' ? 'Testing\u2026' : 'Test Connection'}
                </button>

                {statusMessage && (
                  <span className={`v4-model-message v4-model-message--${String(connectionStatus)}`} role="alert">
                    {statusMessage}
                  </span>
                )}

                {latencyMs != null && connectionStatus === 'connected' && (
                  <span className="v4-model-latency">{latencyMs}ms</span>
                )}
              </div>

              {/* Model select — visible only after test passes (non-custom) */}
              {!isCustom && connectionStatus === 'connected' && selectedProvider && (
                <div className="v4-model-select-group v4-model-select-post-test">
                  <label htmlFor="v4-model-select">Model</label>
                  <select
                    id="v4-model-select"
                    value={selectedModelId}
                    onChange={(e) => setSelectedModelId(e.target.value)}
                    className="v4-select"
                  >
                    {selectedProvider.models.map((m) => (
                      <option key={m.id} value={m.id}>
                        {m.label}{m.recommended ? ' (recommended)' : ''}
                      </option>
                    ))}
                  </select>
                </div>
              )}

              {/* Connected state hint */}
              {connectionStatus === 'connected' && !modelReady && (
                <div className="v4-modal-status-hint">
                  {isCustom
                    ? 'Connected \u00b7 Click Use Model to finalize.'
                    : 'Connected \u00b7 Select a model and click Use Model to finalize.'}
                </div>
              )}

              {/* Use Model button — visible when connected */}
              {connectionStatus === 'connected' && !modelReady && (
                <button
                  type="button"
                  className="v4-use-model-btn"
                  disabled={isCustom ? !customModelId.trim() : !selectedModelId}
                  onClick={handleUseModel}
                >
                  Use Model
                </button>
              )}

              {/* Clear config link */}
              {modelReady && (
                <button
                  type="button"
                  className="v4-model-clear-link"
                  onClick={handleClearConfig}
                >
                  Clear configuration
                </button>
              )}
            </form>
          ) : (
            <div className="v4-modal-empty">No providers available.</div>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );

  return (
    <>
      {/* Compact trigger in header */}
      <button
        type="button"
        ref={triggerRef}
        className="v4-model-trigger"
        onClick={openModal}
        aria-haspopup="dialog"
        aria-expanded={modalOpen}
      >
        <span className="v4-model-status-dot" data-status={triggerStatus} />
        <span className="v4-model-trigger-label" data-ready={modelReady ? "true" : undefined}>{triggerLabel}</span>
      </button>

      {/* Portal modal */}
      {modal}
    </>
  );
}
