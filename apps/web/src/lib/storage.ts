export type Provider = string;
export interface ProviderKey {
  key: string;
  remember: boolean;
  check?: { ok: boolean; at: string; message?: string };
}
export type ProviderKeys = Record<Provider, ProviderKey>;
export type RunDefaults = {
  payer?: 'server' | 'own';
  preferred?: { provider: string; modelId: string } | null;
  costCapUsd?: number; // server-key runs
  maxTokens?: number; // own-key runs
  askBeforeEachRun?: boolean;
};

export const storageKeys = {
  adminToken: 'catalyst.adminToken',
  providerKeys: 'catalyst.providerKeys',
  sidePanelWidth: 'catalyst.sidePanelPx',
  runDefaults: 'catalyst.runDefaults',
} as const;
export type StorageKey = keyof typeof storageKeys;

function storage(kind: 'localStorage' | 'sessionStorage'): Storage | null {
  try {
    return window[kind];
  } catch {
    return null;
  }
}
function read<T>(kind: 'localStorage' | 'sessionStorage', key: string): T | null {
  try {
    const value = storage(kind)?.getItem(key);
    return value === null || value === undefined ? null : (JSON.parse(value) as T);
  } catch {
    return null;
  }
}
function write(kind: 'localStorage' | 'sessionStorage', key: string, value: unknown): void {
  try {
    storage(kind)?.setItem(key, JSON.stringify(value));
  } catch {
    /* Storage can be unavailable or full. */
  }
}
function remove(kind: 'localStorage' | 'sessionStorage', key: string): void {
  try {
    storage(kind)?.removeItem(key);
  } catch {
    /* Storage can be unavailable. */
  }
}

export function getAdminToken(): string | null {
  return read<string>('localStorage', storageKeys.adminToken);
}
export function setAdminToken(value: string): void {
  write('localStorage', storageKeys.adminToken, value);
}
export function removeAdminToken(): void {
  remove('localStorage', storageKeys.adminToken);
}
export const ADMIN_TOKEN_REJECTED_NOTICE = 'The saved admin token was not accepted and has been removed.';
export function notifyAdminTokenRejected(): void {
  window.dispatchEvent(new CustomEvent('catalyst:admin-token-rejected'));
}
export function getProviderKeys(): ProviderKeys {
  return {
    ...(read<ProviderKeys>('localStorage', storageKeys.providerKeys) ?? {}),
    ...(read<ProviderKeys>('sessionStorage', storageKeys.providerKeys) ?? {}),
  };
}
export function getRememberedProviderKeys(): ProviderKeys {
  return read<ProviderKeys>('localStorage', storageKeys.providerKeys) ?? {};
}
export function getRunDefaults(): RunDefaults {
  const defaults = read<RunDefaults>('localStorage', storageKeys.runDefaults) ?? {};
  if (defaults.payer) return defaults;
  const browserKeys = getProviderKeys();
  const payer: RunDefaults['payer'] =
    defaults.preferred && browserKeys[defaults.preferred.provider]?.key ? 'own' : 'server';
  const migrated: RunDefaults = { ...defaults, payer };
  write('localStorage', storageKeys.runDefaults, migrated);
  return migrated;
}
export function setRunDefaults(value: RunDefaults): void {
  write('localStorage', storageKeys.runDefaults, value);
  changed();
}
function changed(): void {
  window.dispatchEvent(new Event('catalyst:keys-changed'));
}
export function removeAllProviderKeys(): void {
  remove('localStorage', storageKeys.providerKeys);
  remove('sessionStorage', storageKeys.providerKeys);
  changed();
}
export function setProviderKey(provider: Provider, value: ProviderKey): void {
  const persistent = read<ProviderKeys>('localStorage', storageKeys.providerKeys) ?? {};
  const temporary = read<ProviderKeys>('sessionStorage', storageKeys.providerKeys) ?? {};
  delete persistent[provider];
  delete temporary[provider];
  (value.remember ? persistent : temporary)[provider] = value;
  write('localStorage', storageKeys.providerKeys, persistent);
  write('sessionStorage', storageKeys.providerKeys, temporary);
  changed();
}
export function removeProviderKey(provider: Provider): void {
  const persistent = read<ProviderKeys>('localStorage', storageKeys.providerKeys) ?? {};
  const temporary = read<ProviderKeys>('sessionStorage', storageKeys.providerKeys) ?? {};
  delete persistent[provider];
  delete temporary[provider];
  write('localStorage', storageKeys.providerKeys, persistent);
  write('sessionStorage', storageKeys.providerKeys, temporary);
  changed();
}
export function getSidePanelWidth(): number | null {
  return read<number>('localStorage', storageKeys.sidePanelWidth);
}
export function setSidePanelWidth(value: number): void {
  write('localStorage', storageKeys.sidePanelWidth, value);
}
