import CandlestickChart from '../chart/CandlestickChart';
import type { DemoCase } from '../../mock/demoCases';

interface Props {
  demoCase: DemoCase;
  selectedDate: string | null;
  onSelectDate: (date: string) => void;
}

export default function PriceMoveCard({ demoCase, selectedDate, onSelectDate }: Props) {
  return (
    <section className="wb-card wb-price-card" aria-labelledby="price-move-title">
      <div className="wb-card-header">
        <div>
          <span className="wb-card-kicker">Market selection</span>
          <h2 id="price-move-title">{demoCase.ticker} price history</h2>
        </div>
        <span className="wb-chart-hint">Click a candle to select a trade date</span>
      </div>

      <div className="wb-price-chart">
        <CandlestickChart
          symbol={demoCase.ticker}
          selectedDate={selectedDate ?? undefined}
          onHover={() => {}}
          onDayClick={(date) => onSelectDate(date)}
        />
      </div>
    </section>
  );
}
