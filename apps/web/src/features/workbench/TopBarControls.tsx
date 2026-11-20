import type React from 'react';
import { useEffect, useRef, useState } from 'react';
import { Search } from 'lucide-react';
import { Link } from 'react-router';
import { useAnalysisModel } from '@/lib/useAnalysisModel';

export function TopBarControls({ onOpenPalette }: { onOpenPalette: () => void }): React.JSX.Element {
  const { choice, usableModels, setAnalysisModel, keys } = useAnalysisModel();
  const [open, setOpen] = useState(false);
  const menu = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const outside = (event: MouseEvent) => {
      if (!menu.current?.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', outside);
    document.addEventListener('keydown', escape);
    return () => {
      document.removeEventListener('mousedown', outside);
      document.removeEventListener('keydown', escape);
    };
  }, []);
  const label = choice
    ? `${choice.providerLabel} · ${choice.credential === 'browser_key' ? 'your key' : 'server key'}`
    : 'No key yet';
  return (
    <div className="flex min-w-0 items-center gap-2">
      <button type="button" className="ticker-search" onClick={onOpenPalette}>
        <Search size={15} strokeWidth={1.5} aria-hidden="true" />
        <span className="flex-1">Search ticker or company</span>
        <kbd className="rounded-sm border border-line-200 px-1 font-numeric text-caption text-ink-300">
          ⌘K
        </kbd>
      </button>
      <div className="relative" ref={menu}>
        <button
          type="button"
          className="access-button inline-flex shrink-0 items-center gap-2 rounded-md px-2 type-body-sm text-ink-200 hover:bg-bg-200"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
        >
          <span className="status-dot rounded-full bg-up" />
          {label}
        </button>
        {open && (
          <div className="analysis-model-menu" role="dialog" aria-label="Analysis model">
            <span className="type-caption text-ink-300">Analyses use</span>
            <strong>{choice ? `${choice.providerLabel} · ${choice.modelId}` : 'No model available'}</strong>
            <span className="type-caption text-ink-300">
              {choice?.credential === 'browser_key'
                ? `${keys[choice.provider]?.check?.ok ? 'works' : 'your key'} · saved in this browser`
                : 'server key'}
            </span>
            <div className="analysis-model-options">
              {usableModels.map(({ provider, model, source, failed }) => (
                <label key={`${provider.id}:${model.model_id}`}>
                  <input
                    type="radio"
                    checked={choice?.provider === provider.id && choice.modelId === model.model_id}
                    onChange={() => {
                      setAnalysisModel(provider.id, model.model_id, source);
                      setOpen(false);
                    }}
                  />
                  <span>
                    {provider.label} · <span className="font-numeric">{model.model_id}</span> ·{' '}
                    {source === 'server' ? 'server key' : 'your key'}
                    {failed ? ' · rejected' : ''}
                  </span>
                </label>
              ))}
            </div>
            <Link to="/settings#models" className="text-info" onClick={() => setOpen(false)}>
              Models and keys in Settings
            </Link>
          </div>
        )}
      </div>
    </div>
  );
}
