import type React from 'react';
import { StateNotice } from '@/components/catalyst';
import { Badge } from '@/components/ui';
import { DEFAULT_COST_CAP_USD } from '@/lib/run';
import { formatDay, formatPct } from '@/lib/format';
import type { AnswerLead, AnswerModel, AnswerQuote, ProcessModel } from './model';
import { GroupLine } from './GroupLine';
import { Lead, MoreDetails, Quote } from './AnswerSections';

function badge(answer: AnswerModel): { label: string; tone: 'up' | 'info' | 'warn' | 'neutral' } {
  switch (answer.productTier) {
    case 'CONFIRMED':
      return { label: 'Confirmed', tone: 'up' };
    case 'REPORTED':
      return { label: 'Reported', tone: 'info' };
    case 'MOVED_WITH_GROUP':
      return { label: 'With its group', tone: 'info' };
    case 'LARGER_THAN_GROUP':
    case 'NOTHING_FOUND':
      return { label: 'No clear cause', tone: 'warn' };
    default:
      return {
        label: answer.outcome.label,
        tone:
          answer.outcome.tone === 'up'
            ? 'up'
            : answer.outcome.tone === 'info'
              ? 'info'
              : answer.outcome.tone === 'warn'
                ? 'warn'
                : 'neutral',
      };
  }
}

function costCapBody(spent: number | null, cap: number | null): string {
  if (spent != null && cap != null)
    return `It spent $${spent.toFixed(3)} of $${cap.toFixed(2)}. Nothing was published.`;
  if (spent != null)
    return `It spent $${spent.toFixed(3)} before reaching its cost cap. Nothing was published.`;
  if (cap != null) return `It reached its $${cap.toFixed(2)} limit. Nothing was published.`;
  return 'The run reached its cost cap. Nothing was published.';
}

export function AnswerTab({
  answer,
  selectedQuoteId,
  quoteLinked = false,
  onSelectQuote,
  onBack,
  onRunAgain,
  process,
  onShowProcess,
  defaultCapUsd = DEFAULT_COST_CAP_USD,
}: {
  answer: AnswerModel;
  selectedQuoteId: string | null;
  quoteLinked?: boolean;
  onSelectQuote: (quote: AnswerQuote) => void;
  onBack: () => void;
  onRunAgain?: (capOverride?: number) => void;
  process: ProcessModel;
  onShowProcess: () => void;
  defaultCapUsd?: number;
}): React.JSX.Element {
  const retryCap = Math.min(2, (answer.meta.costCapUsd ?? defaultCapUsd) * 2);
  if (answer.failureMessage)
    return (
      <div className="flex flex-col gap-4">
        <StateNotice
          tone={answer.stoppedAtCostCap ? 'warn' : 'down'}
          title={answer.stoppedAtCostCap ? 'Run stopped at the cost cap' : 'Run failed'}
          body={
            answer.stoppedAtCostCap
              ? costCapBody(answer.meta.costUsd, answer.meta.costCapUsd)
              : answer.failureMessage
          }
          action={
            onRunAgain
              ? {
                  label: answer.stoppedAtCostCap ? `Retry with $${retryCap.toFixed(2)}` : 'Run again',
                  onClick: () => onRunAgain(answer.stoppedAtCostCap ? retryCap : undefined),
                }
              : undefined
          }
          secondaryAction={{ label: 'Back', onClick: onBack }}
        />
      </div>
    );
  if (answer.legacyText)
    return (
      <div className="flex flex-col gap-3">
        <p className="m-0 type-caption text-ink-300">
          This run was made with an older version; only its text summary was kept.
        </p>
        <pre className="m-0 whitespace-pre-wrap type-body-sm text-ink-200">{answer.legacyText}</pre>
      </div>
    );

  const result = badge(answer);
  const move = answer.relative.find((row) => row.kind === 'target')?.value ?? answer.group?.stockPct ?? null;
  // The headline shows the key sentence; its source line and the remaining quotes follow from it.
  const headlineQuote =
    answer.quotes.find((quote) => quote.text === answer.mainCause) ??
    answer.quotes.find((quote) => quote.role === 'Primary') ??
    answer.quotes[0];
  const primaryQuote = headlineQuote;
  const otherQuotes = answer.quotes.filter(
    (quote) => quote !== headlineQuote && quote.text !== answer.mainCause,
  );
  const isReported = answer.productTier === 'REPORTED' && answer.reported != null;
  const isConfirmed = answer.productTier === 'CONFIRMED';
  const isWithGroup = answer.productTier === 'MOVED_WITH_GROUP';
  const isNoCause =
    answer.productTier === 'NOTHING_FOUND' ||
    answer.productTier === 'LARGER_THAN_GROUP' ||
    Boolean(answer.abstention && !isReported && !isWithGroup);
  const title = isReported
    ? `Reported: ${answer.reported!.cause}`
    : isWithGroup
      ? answer.group?.peersPct != null &&
        (answer.group.sectorPct == null ||
          Math.abs((move ?? 0) - answer.group.peersPct) <= Math.abs((move ?? 0) - answer.group.sectorPct))
        ? `Moved with its peers (${formatPct(answer.group.peersPct)})`
        : answer.group?.sectorPct != null
          ? `Moved with its sector (${answer.group.sectorTicker ?? 'sector'} ${formatPct(answer.group.sectorPct)})`
          : 'Moved with its group'
      : isNoCause
        ? `No clear cause found${process.decisions.length ? ` in ${process.decisions.length} items` : ''}${process.searches.length ? ` from ${process.searches.length} searches` : ''}`
        : (answer.mainCause ?? answer.moveSizeReason ?? answer.headline);
  const overline = isConfirmed
    ? "The cause, in the source's words"
    : isReported
      ? 'What one source says'
      : isWithGroup
        ? 'Why there is no company-specific story'
        : isNoCause
          ? 'What Catalyst found'
          : 'Answer';
  const headline = answer.headline ? <span className="sr-only">{answer.headline}</span> : null;

  return (
    <div className="polish-answer-content">
      {headline}
      <div className="polish-answer-move-row">
        <span
          className={`font-numeric polish-answer-move ${move == null ? 'text-ink-100' : move > 0 ? 'text-up' : move < 0 ? 'text-down' : 'text-ink-100'}`}
        >
          {move == null ? '—' : formatPct(move)}
        </span>
        <span className="type-body-sm text-ink-200">
          {answer.ticker} · {formatDay(answer.sessionDate)}
        </span>
        <Badge tone={result.tone}>{result.label}</Badge>
      </div>
      <section className="polish-answer-cause">
        <span className="type-label text-ink-300">{overline}</span>
        {title && !isConfirmed && <h1 className="polish-answer-title">{title}</h1>}
        {isConfirmed && answer.mainCause && (
          <blockquote className="polish-answer-primary-quote polish-answer-confirmed-title">
            “{answer.mainCause}”
          </blockquote>
        )}
        {isReported && (
          <blockquote className="polish-answer-primary-quote">“{answer.reported!.quote}”</blockquote>
        )}
        {(isConfirmed || isReported) &&
          (isReported
            ? answer.reported!.publisher || answer.reported!.publishedAt
            : primaryQuote?.publisher || primaryQuote?.time) && (
            <p className="m-0 type-caption text-ink-300">
              {isReported && answer.reported!.url ? (
                <a
                  className="text-info hover:underline"
                  href={answer.reported!.url}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  {answer.reported!.publisher ?? 'Source'}
                </a>
              ) : (
                <>{isReported ? answer.reported!.publisher : primaryQuote?.publisher}</>
              )}
              {(isReported ? answer.reported!.publishedAt : primaryQuote?.time) && (
                <> · {isReported ? answer.reported!.publishedAt : primaryQuote?.time}</>
              )}
            </p>
          )}
        <p className="m-0 type-caption text-ink-200">
          {isConfirmed
            ? 'Confirmed means the quote matches its source, was published before the close, and a second model agreed it explains the move.'
            : isReported
              ? 'A source states this reason. Catalyst could not confirm it with a second check.'
              : isWithGroup
                ? answer.moveSizeReason
                : isNoCause
                  ? answer.abstention?.reason
                  : answer.moveSizeReason}
        </p>
      </section>
      {answer.relative.length > 0 && <GroupLine answer={answer} />}
      {answer.coMovement && <p className="m-0 type-caption text-ink-300">{answer.coMovement}</p>}
      {isConfirmed && otherQuotes.length > 0 && (
        <section className="polish-answer-section">
          <h2 className="type-label text-ink-300">
            Also in the source · {otherQuotes.length} {otherQuotes.length === 1 ? 'quote' : 'quotes'}
          </h2>
          {otherQuotes.map((quote) => (
            <Quote
              key={quote.id}
              quote={quote}
              selected={selectedQuoteId === quote.id}
              linked={selectedQuoteId === quote.id && quoteLinked}
              onSelect={() => onSelectQuote(quote)}
            />
          ))}
        </section>
      )}
      {!isConfirmed && answer.abstention?.possibleExplanations.length ? (
        <section className="polish-answer-section">
          <h2 className="type-label text-ink-300">
            Reported, not confirmed · {answer.abstention.possibleExplanations.length}
          </h2>
          {answer.abstention.possibleExplanations.map((lead, i) => (
            <Lead key={`${i}-${lead.text}`} lead={lead} />
          ))}
        </section>
      ) : null}
      {!isConfirmed && answer.abstention?.leads.length ? (
        <section className="polish-answer-section">
          <h2 className="type-label text-ink-300">
            {isNoCause ? 'Closest headlines' : 'Other headlines we checked'} ·{' '}
            {answer.abstention.leads.length}
          </h2>
          {answer.abstention.leads.map((lead, i) => (
            <Lead key={`${i}-${lead.text}`} lead={lead} />
          ))}
        </section>
      ) : null}
      {(answer.filings.length > 0 || answer.relative.length > 0 || answer.gaps.length > 0) && (
        <MoreDetails answer={answer} />
      )}
      {process.searches.length > 0 && (
        <button type="button" className="polish-answer-process-link" onClick={onShowProcess}>
          Process details · {process.searches.length} searches <span aria-hidden="true">→</span>
        </button>
      )}
    </div>
  );
}
