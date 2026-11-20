import { useCallback, useEffect, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import type { ApiError } from '@/api/client';
import { queryKeys, useCreateRun } from '@/api/queries';
import type { components } from '@/api/schema';
import { buildCreateRunRequest, resolveAnalysisModel } from '@/lib/run';
import { getProviderKeys, getRunDefaults, setProviderKey } from '@/lib/storage';
import { getTurnstileToken, turnstileEnabled } from '@/lib/turnstile';
import type { RunDialogProps } from './RunDialog';

export type RunChoice = Parameters<RunDialogProps['onSubmit']>[0];

const VERIFY_FAILED = 'Could not confirm this is a person, so the run did not start. Try again.';

type RunStarterInput = {
  ticker: string;
  date: string | null;
  catalog: components['schemas']['ModelCatalogResponse'] | undefined;
  quota: components['schemas']['QuotaResponse'] | undefined;
  // Called before a new run starts and once it is accepted (with its id).
  onBeforeStart: () => void;
  onStarted: (runId: string) => void;
};

// Everything about starting a run: the dialog, the request, its errors and "try again".
export function useRunStarter({ ticker, date, catalog, quota, onBeforeStart, onStarted }: RunStarterInput) {
  const [dialogOpen, setDialogOpenState] = useState(false);
  const setDialogOpen = useCallback(
    (open: boolean) => {
      if (!open || date) setDialogOpenState(open);
    },
    [date],
  );
  const [error, setError] = useState<string | null>(null);
  const [retryable, setRetryable] = useState(false);
  const [quotaReached, setQuotaReached] = useState(false);
  const [starting, setStarting] = useState(false);
  const startingRef = useRef(false);
  const latestRef = useRef({ ticker, date });
  latestRef.current = { ticker, date };
  const [lastChoice, setLastChoice] = useState<RunChoice | null>(null);
  const createRun = useCreateRun();
  const queryClient = useQueryClient();
  // Anonymous visitors on the public site prove they are a person before each run.
  const needsTurnstile = turnstileEnabled && quota?.tier === 'anonymous';

  const start = useCallback(
    async (choice: RunChoice, capOverride?: number) => {
      if (!date || startingRef.current) return;
      startingRef.current = true;
      setStarting(true);
      try {
        setLastChoice(choice);
        onBeforeStart();
        setError(null);
        setQuotaReached(false);
        setRetryable(false);
        setDialogOpen(false);
        if (choice.mode === 'browser_key' && choice.key) {
          setProviderKey(choice.provider.id, { key: choice.key, remember: Boolean(choice.remember) });
        }
        const model = {
          provider: choice.provider.id,
          providerLabel: choice.provider.label,
          modelId: choice.modelId,
          credential: choice.mode,
          key: choice.key,
          keyHint: choice.key?.slice(-4) ?? null,
        } as const;
        let turnstileToken: string | undefined;
        if (needsTurnstile) {
          try {
            turnstileToken = await getTurnstileToken();
          } catch {
            setError(VERIFY_FAILED);
            setRetryable(true);
            return;
          }
        }
        if (latestRef.current.ticker !== ticker || latestRef.current.date !== date) return;
        try {
          const created = await createRun.mutateAsync({
            body: buildCreateRunRequest(
              ticker,
              date,
              model,
              capOverride ?? getRunDefaults().costCapUsd,
              getRunDefaults().maxTokens,
            ),
            turnstileToken,
          });
          if (latestRef.current.ticker === ticker && latestRef.current.date === date) {
            onStarted(created.run_id);
          }
          void queryClient.invalidateQueries({ queryKey: queryKeys.runsForSession(ticker, date) });
          void queryClient.invalidateQueries({ queryKey: queryKeys.recentRuns(5) });
          void queryClient.invalidateQueries({ queryKey: queryKeys.runHistoryAll });
        } catch (caught) {
          const status = (caught as Partial<ApiError>).status;
          if (status === 429 && (caught as Partial<ApiError>).code === 'capacity_exceeded') {
            setError('The server is busy with other runs. Try again in a minute.');
            setRetryable(true);
          } else if (status === 429) {
            setQuotaReached(true);
            setError(
              ['rate_limited_ip', 'rate_limited_token'].includes((caught as Partial<ApiError>).code ?? '') &&
                choice.mode === 'browser_key'
                ? "You reached today's limit of runs with your own key."
                : 'Free demo limit reached',
            );
          } else if (status === 401) {
            setError(VERIFY_FAILED);
            setRetryable(true);
          } else if (status === 422) {
            setError('This session cannot be explained');
          } else if (status === 503) {
            setError('The server is not ready to run: its search index or model setup is incomplete');
            setRetryable(true);
          } else {
            setError('Could not start the run');
            setRetryable(true);
          }
        }
      } finally {
        startingRef.current = false;
        setStarting(false);
      }
    },
    [date, ticker, createRun, queryClient, onBeforeStart, onStarted, needsTurnstile],
  );

  // "Explain this session": start straight away with a usable key, otherwise ask how to run.
  const request = useCallback(() => {
    if (!date || !catalog) return;
    const defaults = getRunDefaults();
    const keys = getProviderKeys();
    const resolved = resolveAnalysisModel(catalog, keys, defaults, quota?.per_caller?.limit !== 0);
    if (defaults.askBeforeEachRun) {
      setDialogOpen(true);
      return;
    }
    if (resolved?.credential === 'browser_key') {
      const provider = catalog.providers?.find((item) => item.id === resolved.provider);
      if (provider)
        void start({
          mode: 'browser_key',
          provider,
          modelId: resolved.modelId,
          key: resolved.key,
          remember: keys[provider.id]?.remember,
        });
    } else if (resolved?.credential === 'server_env') {
      const provider = catalog.providers?.find((item) => item.id === resolved.provider);
      if (provider) {
        void start({ mode: 'server_env', provider, modelId: resolved.modelId });
      }
    } else {
      setDialogOpen(true);
    }
  }, [date, catalog, start, setDialogOpen]);

  const retry = useCallback(
    (capOverride?: number) => {
      if (lastChoice) void start(lastChoice, capOverride);
      else request();
    },
    [lastChoice, start, request],
  );

  return {
    dialogOpen,
    setDialogOpen,
    error,
    quotaReached,
    retryable,
    lastChoice,
    starting: starting || createRun.isPending,
    start,
    request,
    retry,
  };
}

// Enter (outside text fields) explains the selected session when it is ready to run.
export function useExplainShortcut(enabled: boolean, onExplain: () => void): void {
  useEffect(() => {
    if (!enabled) return;
    const keydown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (event.key !== 'Enter' || target?.matches('input, textarea, select, [contenteditable="true"]'))
        return;
      event.preventDefault();
      onExplain();
    };
    window.addEventListener('keydown', keydown);
    return () => window.removeEventListener('keydown', keydown);
  }, [enabled, onExplain]);
}
