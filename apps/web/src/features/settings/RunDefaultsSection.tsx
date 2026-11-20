import type React from 'react';
import { Input } from '@/components/ui';
import type { RunDefaults } from '@/lib/storage';
import { DEFAULT_COST_CAP_USD, DEFAULT_MAX_TOKENS, MAX_MAX_TOKENS, MIN_MAX_TOKENS } from '@/lib/run';

export type RunDefaultsSectionProps = {
  value: RunDefaults;
  onChange: (value: RunDefaults) => void;
};

export function RunDefaultsSection({ value, onChange }: RunDefaultsSectionProps): React.JSX.Element {
  const cap = Math.max(0.05, Math.min(2, value.costCapUsd ?? DEFAULT_COST_CAP_USD));
  const tokens = Math.max(MIN_MAX_TOKENS, Math.min(MAX_MAX_TOKENS, value.maxTokens ?? DEFAULT_MAX_TOKENS));

  return (
    <section id="spending" className="settings-card flex flex-col gap-3">
      <h2 className="type-heading m-0">Spending limits</h2>
      <div className="grid gap-3 border-t border-line-100 py-3 settings-field-row md:items-center">
        <div className="flex flex-col gap-1">
          <span className="text-body-sm font-medium">Server key · most one analysis may cost</span>
          <span className="text-caption text-ink-300">
            A run on the server key stops before a model call that could go over this. Typical runs cost
            $0.005–0.02.
          </span>
        </div>
        <Input
          type="number"
          min="0.05"
          max="2.00"
          step="0.05"
          prefix="$"
          value={cap.toFixed(2)}
          onChange={(event) => {
            const parsed = Number(event.target.value);
            if (Number.isFinite(parsed))
              onChange({ ...value, costCapUsd: Math.max(0.05, Math.min(2, Math.round(parsed * 20) / 20)) });
          }}
          aria-label="Cost cap per run"
          className="settings-cost-cap"
        />
      </div>
      <div className="grid gap-3 border-t border-line-100 py-3 settings-field-row md:items-center">
        <div className="flex flex-col gap-1">
          <span className="text-body-sm font-medium">Your own key · most tokens one analysis may use</span>
          <span className="text-caption text-ink-300">
            Works the same for every model; your provider bills the tokens. A run stops before a model call
            that could go over this. A typical run uses about 20k tokens, but one step may need up to 136k, so
            the limit starts at 150k.
          </span>
        </div>
        <Input
          type="number"
          min={MIN_MAX_TOKENS / 1000}
          max={MAX_MAX_TOKENS / 1000}
          step="50"
          suffix="k"
          value={String(Math.round(tokens / 1000))}
          onChange={(event) => {
            const parsed = Number(event.target.value);
            if (Number.isFinite(parsed))
              onChange({
                ...value,
                maxTokens: Math.max(
                  MIN_MAX_TOKENS,
                  Math.min(MAX_MAX_TOKENS, Math.round(parsed / 50) * 50_000),
                ),
              });
          }}
          aria-label="Token limit per run on your own key"
          className="settings-cost-cap"
        />
      </div>
      <div className="grid gap-3 border-t border-line-100 py-3 settings-field-row md:items-center">
        <div className="flex flex-col gap-1">
          <span className="text-body-sm font-medium">Confirm before each analysis</span>
          <span className="text-caption text-ink-300">
            Show who pays, the model and the cost estimate before starting.
          </span>
        </div>
        <input
          className="settings-switch-input justify-self-end"
          type="checkbox"
          aria-label="Confirm before each analysis"
          checked={value.askBeforeEachRun ?? false}
          onChange={(event) => onChange({ ...value, askBeforeEachRun: event.target.checked })}
        />
      </div>
    </section>
  );
}
