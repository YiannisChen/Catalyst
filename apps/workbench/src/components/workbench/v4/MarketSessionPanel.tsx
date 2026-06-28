import CandlestickChart from '../../chart/CandlestickChart';
import type { DemoCase } from '../../../mock/demoCases';
import { getSelectedDayMetrics } from '../workbench-model';

interface Props {
  demoCase: DemoCase;
  selectedDate: string | null;
  onSelectDate: (date: string) => void;
}

export default function MarketSessionPanel({ demoCase, selectedDate, onSelectDate }: Props) {
  const metrics = getSelectedDayMetrics(demoCase, selectedDate);
  return (
    <div className="v4-market-chart-col">
      <div className="v4-market-chart-header">
        <div className="v4-market-chart-title-row">
          <h2 className="v4-market-chart-title">{demoCase.ticker}</h2>
          {selectedDate && <span className="v4-market-chart-date">{selectedDate}</span>}
          {metrics && (
            <>
              <span className="v4-market-chart-price">
                ${metrics.close.toFixed(2)}
              </span>
              <span className={`v4-market-chart-change ${metrics.closeMove >= 0 ? 'is-up' : 'is-down'}`}>
                {metrics.closeMove >= 0 ? '+' : ''}{metrics.closeMove.toFixed(2)}%
              </span>
            </>
          )}
        </div>
        <span className="v4-hint">Click a candle to select a trade date</span>
      </div>
      <div className="v4-chart-wrap">
        <CandlestickChart
          symbol={demoCase.ticker}
          selectedDate={selectedDate ?? undefined}
          onHover={() => {}}
          onDayClick={(date) => onSelectDate(date)}
        />
      </div>
    </div>
  );
}
