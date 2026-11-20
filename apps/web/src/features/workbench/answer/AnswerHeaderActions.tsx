import { useEffect, useState } from 'react';
import type React from 'react';
import { Check, Copy, RotateCcw } from 'lucide-react';
import { Button } from '@/components/ui';
import type { AnswerModel, ProcessModel } from './model';

export function AnswerHeaderActions({
  answer,
  process,
  tab,
  onRunAgain,
}: {
  answer: AnswerModel;
  process: ProcessModel;
  tab: string;
  onRunAgain?: () => void;
}): React.JSX.Element {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 2000);
    return () => window.clearTimeout(timer);
  }, [copied]);
  const copy = async () => {
    if (!process.traceJson) return;
    try {
      await navigator.clipboard.writeText(process.traceJson);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };
  return (
    <div className="flex items-center gap-2">
      {/* The result badge lives in the answer itself; the header only offers actions. */}
      {tab === 'process' && process.traceJson && (
        // Icon only: with the two badges, a text label pushed the panel title out of the header.
        <Button
          size="sm"
          variant="ghost"
          aria-label={copied ? 'Trace copied' : 'Copy trace JSON'}
          title={copied ? 'Trace copied' : 'Copy trace JSON'}
          icon={copied ? <Check size={16} strokeWidth={1.5} /> : <Copy size={16} strokeWidth={1.5} />}
          onClick={() => void copy()}
        />
      )}
      {/* Always here, whichever tab is open: a saved answer can be explained again with the current model. */}
      {onRunAgain && (
        <Button
          size="sm"
          variant="ghost"
          icon={<RotateCcw size={16} strokeWidth={1.5} aria-hidden="true" />}
          title="Explain this day again with the model analyses use now"
          onClick={onRunAgain}
        >
          Run again
        </Button>
      )}
    </div>
  );
}
