import { useEffect, useRef, useState } from 'react';

interface Props {
  activeTickers: string[];
  selectedSymbol: string;
  onSelect: (symbol: string) => void;
}

const GROUPS: Record<string, string[]> = {
  'Tech': ['AAPL', 'MSFT', 'GOOGL', 'GOOG', 'META', 'AMZN', 'CRM', 'ORCL', 'IBM', 'CSCO', 'NOW', 'WDAY', 'SNOW', 'DELL', 'ADBE'],
  'AI / Chip': ['NVDA', 'AMD', 'TSM', 'AVGO', 'INTC', 'QCOM', 'ARM', 'AMAT', 'LRCX', 'MU', 'MRVL', 'SMCI', 'CRWV', 'TXN', 'ASML'],
  'AI Software': ['AI', 'SOUN', 'SOUNW', 'CRWD', 'ANET', 'IDCC'],
  'EV / Auto': ['TSLA', 'RIVN', 'LCID', 'NIO', 'LI', 'BYDDY', 'F', 'GM', 'STLA', 'TM'],
  'China': ['BABA', 'JD', 'BIDU', 'NIO', 'LI', 'BILI', 'NTES', 'SE', 'MCHI', 'FXI'],
  'Finance': ['V', 'MA', 'GS', 'MS', 'BAC', 'WFC', 'C', 'BLK', 'COIN', 'HOOD', 'MARA'],
  'Media': ['NFLX', 'DIS', 'ROKU', 'WBD', 'ZM'],
  'Consumer': ['COST', 'WMT', 'HD', 'TGT', 'NKE', 'SBUX', 'MCD', 'CMG', 'KO', 'EBAY', 'MELI'],
  'Health': ['UNH', 'JNJ', 'LLY', 'MRNA', 'NVO'],
  'Energy': ['XOM', 'CVX', 'OXY', 'XLE', 'USO'],
  'Telecom': ['T', 'VZ'],
  'Other': ['BA', 'UBER', 'GME', 'AMC', 'MULN', 'SQ', 'FB', 'AMJB', 'GLD', 'XLU', 'XLY', 'DIDI'],
};

export default function StockSelector({ activeTickers, selectedSymbol, onSelect }: Props) {
  const [showPanel, setShowPanel] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (panelRef.current && !panelRef.current.contains(e.target as Node)) {
        setShowPanel(false);
      }
    }
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  function handleSelectTicker(sym: string) {
    setShowPanel(false);
    onSelect(sym);
  }

  // Build groups filtered to only tickers that exist in our data
  const activeSet = new Set(activeTickers);
  const renderedGroups = Object.entries(GROUPS)
    .map(([label, symbols]) => ({
      label,
      symbols: symbols.filter((s) => activeSet.has(s)),
    }))
    .filter((g) => g.symbols.length > 0);

  const assigned = new Set(renderedGroups.flatMap((g) => g.symbols));
  const ungrouped = activeTickers.filter((s) => !assigned.has(s)).sort();
  if (ungrouped.length > 0) {
    const otherGroup = renderedGroups.find((g) => g.label === 'Other');
    if (otherGroup) {
      otherGroup.symbols.push(...ungrouped);
    } else {
      renderedGroups.push({ label: 'Other', symbols: ungrouped });
    }
  }

  return (
    <div className="stock-selector">
      {/* Current ticker button — click to open dropdown */}
      <div className="ticker-dropdown-wrapper" ref={panelRef}>
        <button
          className="ticker-current"
          onClick={() => setShowPanel((v) => !v)}
        >
          <span className="ticker-current-symbol">{selectedSymbol || '---'}</span>
          <span className={`ticker-arrow ${showPanel ? 'open' : ''}`}>&#9662;</span>
        </button>

        {showPanel && (
          <div className="ticker-panel">
            {renderedGroups.map((group) => (
              <div className="ticker-panel-group" key={group.label}>
                <div className="ticker-panel-group-label">{group.label}</div>
                <div className="ticker-panel-group-items">
                  {group.symbols.map((sym) => (
                    <button
                      key={sym}
                      className={`ticker-panel-item ${sym === selectedSymbol ? 'active' : ''}`}
                      onClick={() => handleSelectTicker(sym)}
                    >
                      {sym}
                    </button>
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
