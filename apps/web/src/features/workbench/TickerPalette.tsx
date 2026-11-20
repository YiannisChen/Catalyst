import type React from 'react';
import { Search } from 'lucide-react';
import { Command } from 'cmdk';
import { Badge } from '@/components/ui';
import { formatCompanyName } from '@/lib/format';
import type { components } from '@/api/schema';
import { historyOutcome } from '@/features/history/outcome';

type Ticker = components['schemas']['SelectableTicker'];
type RecentRun = components['schemas']['RunHistoryItem'];

export type TickerPaletteProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  tickers: Ticker[];
  /** Symbols this server has daily prices for; the rest are listed apart. Undefined: all are shown as usual. */
  pricedTickers?: string[];
  recentRuns: RecentRun[];
  onSelectTicker: (ticker: string) => void;
  onSelectRun: (run: RecentRun) => void;
};

export function TickerPalette({
  open,
  onOpenChange,
  tickers,
  pricedTickers,
  recentRuns,
  onSelectTicker,
  onSelectRun,
}: TickerPaletteProps): React.JSX.Element {
  const priced = pricedTickers && pricedTickers.length > 0 ? new Set(pricedTickers) : null;
  const withData = priced ? tickers.filter((item) => priced.has(item.ticker.toUpperCase())) : tickers;
  const withoutData = priced ? tickers.filter((item) => !priced.has(item.ticker.toUpperCase())) : [];
  const row = (item: Ticker, noData: boolean) => (
    <Command.Item
      key={item.ticker}
      value={`${item.ticker} ${item.name}`}
      onSelect={() => onSelectTicker(item.ticker)}
      className="palette-item palette-row grid cursor-pointer items-center gap-3 rounded-md px-3"
    >
      <span className={`font-numeric ${noData ? 'text-ink-300' : ''}`}>{item.ticker}</span>
      <span className={`truncate ${noData ? 'text-ink-300' : 'text-ink-200'}`}>
        {formatCompanyName(item.name)}
      </span>
      <span className="type-caption font-numeric text-ink-300">
        {noData ? 'No price data' : (item.sector_etf ?? '')}
      </span>
    </Command.Item>
  );
  return (
    <Command.Dialog
      open={open}
      onOpenChange={onOpenChange}
      label="Search tickers"
      overlayClassName="palette-backdrop fixed inset-0 z-50"
      contentClassName="palette-content z-50"
      className="palette-size overflow-hidden rounded-lg border border-line-200 bg-bg-200 shadow-pop"
    >
      <div className="palette-search flex items-center gap-3 border-b border-line-100 px-4">
        <Search size={16} strokeWidth={1.5} className="text-ink-300" />
        <Command.Input
          autoFocus
          placeholder="Search tickers"
          className="min-w-0 flex-1 bg-transparent font-numeric text-title text-ink-100 outline-none placeholder:text-ink-300"
        />
        <kbd className="font-numeric type-caption text-ink-300">esc</kbd>
      </div>
      <Command.List className="palette-list overflow-auto p-2">
        <Command.Empty className="p-3 type-body-sm text-ink-300">No matching tickers.</Command.Empty>
        <Command.Group
          heading={
            priced ? `Tickers · ${withData.length} with prices` : `Tickers · ${tickers.length} covered`
          }
          className="palette-group"
        >
          {withData.map((item) => row(item, false))}
        </Command.Group>
        {withoutData.length > 0 && (
          <Command.Group
            heading={`No prices on this server · ${withoutData.length}`}
            className="palette-group"
          >
            {withoutData.map((item) => row(item, true))}
          </Command.Group>
        )}
        <Command.Group heading="Recent runs" className="palette-group">
          {recentRuns.map((item) => (
            <Command.Item
              key={item.run_id}
              value={`${item.ticker ?? ''} ${item.session_date ?? ''}`}
              onSelect={() => onSelectRun(item)}
              className="palette-item palette-row grid cursor-pointer items-center gap-3 rounded-md px-3"
            >
              <span className="font-numeric">{item.ticker}</span>
              <span className="font-numeric type-body-sm text-ink-200">{item.session_date}</span>
              <Badge tone={historyOutcome(item).tone}>{historyOutcome(item).label}</Badge>
            </Command.Item>
          ))}
        </Command.Group>
      </Command.List>
      <div className="flex gap-4 border-t border-line-100 px-4 py-2 type-caption text-ink-300">
        <span>↑↓ move</span>
        <span>↵ open</span>
        <span>Tickers outside the {tickers.length} are not covered</span>
      </div>
    </Command.Dialog>
  );
}
