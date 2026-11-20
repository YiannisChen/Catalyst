import type React from 'react';
import { useState } from 'react';
import { ChevronRight } from 'lucide-react';
import type { AnswerLead, AnswerModel, AnswerQuote } from './model';

export function Lead({ lead }: { lead: AnswerLead }): React.JSX.Element {
  return (
    <a
      className="polish-answer-lead"
      href={lead.url ?? undefined}
      target={lead.url ? '_blank' : undefined}
      rel={lead.url ? 'noopener noreferrer' : undefined}
    >
      <span className="font-numeric">{lead.time}</span>
      <span className="polish-answer-lead-copy">
        <span>{lead.text}</span>
        <span className="type-caption text-ink-300">{lead.publisher}</span>
      </span>
    </a>
  );
}

export function Quote({
  quote,
  selected,
  linked,
  onSelect,
}: {
  quote: AnswerQuote;
  selected: boolean;
  linked: boolean;
  onSelect: () => void;
}): React.JSX.Element {
  return (
    <div className={`polish-answer-quote ${selected ? 'polish-answer-quote-selected' : ''}`}>
      <button type="button" aria-pressed={selected} onClick={onSelect} className="polish-answer-quote-button">
        <span>{quote.text}</span>
        <span className="type-caption text-ink-300">
          {quote.publisher} · {quote.time}
        </span>
        {linked && <span className="type-caption text-accent">News card and timeline dot highlighted</span>}
      </button>
      {quote.url && (
        <a
          className="type-caption text-info hover:underline"
          href={quote.url}
          target="_blank"
          rel="noopener noreferrer"
        >
          Open source
        </a>
      )}
    </div>
  );
}

export function MoreDetails({ answer }: { answer: AnswerModel }): React.JSX.Element {
  const [filingsOpen, setFilingsOpen] = useState(false);
  return (
    <section className="polish-answer-details">
      {answer.filings.length > 0 && (
        <>
          <button
            type="button"
            className="polish-answer-detail-toggle"
            aria-expanded={filingsOpen}
            onClick={() => setFilingsOpen((open) => !open)}
          >
            <span>SEC filings in the window · {answer.filings.length}</span>
            <ChevronRight size={16} aria-hidden="true" />
          </button>
          {filingsOpen && (
            <ul className="polish-answer-filing-list">
              {answer.filings.map((filing) => (
                <li key={filing.url}>
                  <span className="font-numeric">{filing.time} ET</span>
                  <a
                    className="text-info hover:underline"
                    href={filing.url}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    Open filing
                  </a>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
      {answer.gaps.length > 0 && (
        <details className="polish-answer-gap-details">
          <summary className="type-body-sm text-ink-200">Data gaps · {answer.gaps.length}</summary>
          <ul className="polish-answer-timing">
            {answer.gaps.map((gap) => (
              <li key={gap}>{gap}</li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}
