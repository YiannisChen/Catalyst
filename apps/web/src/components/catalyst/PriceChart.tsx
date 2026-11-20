import { useEffect, useRef, useState } from 'react';
import {
  CandlestickSeries,
  ColorType,
  HistogramSeries,
  LineStyle,
  createChart,
  type ISeriesApi,
  type Time,
} from 'lightweight-charts';
import { tokens } from '@/styles/tokens';
import {
  buildCandleData,
  buildMarkerDates,
  buildVolumeData,
  day,
  formatMove,
  shortDate,
  withAlpha,
} from './priceChartData';

export interface PriceRow {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}
export interface PriceChartProps {
  rows: PriceRow[];
  /** Days already explained; their lane mark is brighter. */
  explainedDates?: { date: string }[];
  selectedDate?: string;
  onSelectDate?: (date: string) => void;
  /** Days that can be explained: they get a mark in the lane under the chart and a hover highlight. */
  selectableDates?: readonly string[];
  onHover?: (row: PriceRow | null) => void;
  /** The earliest day Catalyst can explain; bars before it get a quiet shade. */
  coverageStart?: string | null;
  className?: string;
}
/** Pixels per day: about seven months of bars on a desktop-width chart. */
const BAR_SPACING = 7;

type Layout = { barSpacing: number; axisHeight: number; xs: Map<string, number>; coverX: number | null };

export function PriceChart({
  rows,
  explainedDates = [],
  selectedDate,
  onSelectDate,
  selectableDates,
  onHover,
  coverageStart,
  className,
}: PriceChartProps) {
  const host = useRef<HTMLDivElement>(null);
  const chart = useRef<ReturnType<typeof createChart> | null>(null);
  const candle = useRef<ISeriesApi<'Candlestick', Time> | null>(null);
  const volume = useRef<ISeriesApi<'Histogram', Time> | null>(null);
  const rowsRef = useRef(rows);
  const selectRef = useRef(onSelectDate);
  const selectableRef = useRef<Set<string> | null>(null);
  const hoverRef = useRef(onHover);
  const [layout, setLayout] = useState<Layout>({
    barSpacing: 6,
    axisHeight: 26,
    xs: new Map(),
    coverX: null,
  });
  // The hovered day, any day: its column and the hint chip at the top of the chart follow it.
  const [hover, setHover] = useState<{ date: string; x: number; width: number; selectable: boolean } | null>(
    null,
  );
  rowsRef.current = rows;
  selectRef.current = onSelectDate;
  hoverRef.current = onHover;

  useEffect(() => {
    if (!host.current) return;
    const instance = createChart(host.current, {
      autoSize: true,
      layout: {
        attributionLogo: true,
        background: { type: ColorType.Solid, color: tokens['bg-100'] },
        textColor: tokens['ink-300'],
        fontFamily: 'Geist Mono Variable',
        fontSize: 12,
      },
      grid: {
        vertLines: { visible: false },
        horzLines: { color: withAlpha(tokens['bg-200'], 0.55) },
      },
      crosshair: {
        vertLine: {
          color: tokens['line-control'],
          style: LineStyle.Dashed,
          labelBackgroundColor: tokens['bg-300'],
        },
        horzLine: {
          color: tokens['line-control'],
          style: LineStyle.Dashed,
          labelBackgroundColor: tokens['bg-300'],
        },
      },
      rightPriceScale: { borderVisible: false },
      // A fixed scale, panned like a trading chart: drag, or swipe sideways on a trackpad.
      // An up-down wheel or swipe is left to the page, so the news below still scrolls.
      handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
      handleScale: {
        mouseWheel: false,
        pinch: false,
        axisPressedMouseMove: false,
        axisDoubleClickReset: false,
      },
      timeScale: {
        barSpacing: BAR_SPACING,
        borderVisible: false,
        timeVisible: false,
        rightOffset: 2,
        fixLeftEdge: true,
        fixRightEdge: false,
      },
    });
    chart.current = instance;
    const candleSeries = instance.addSeries(CandlestickSeries, {
      upColor: tokens.up,
      downColor: tokens.down,
      borderUpColor: tokens.up,
      borderDownColor: tokens.down,
      wickUpColor: tokens.up,
      wickDownColor: tokens.down,
      priceLineVisible: true,
      priceLineStyle: LineStyle.Dashed,
      priceLineColor: tokens.up,
      lastValueVisible: true,
    });
    const volumeSeries = instance.addSeries(HistogramSeries, {
      priceScaleId: 'volume',
      priceFormat: { type: 'volume' },
      lastValueVisible: false,
      priceLineVisible: false,
    });
    // The bottom margin leaves room for the lane of explainable-day marks above the time axis.
    instance.priceScale('volume').applyOptions({ scaleMargins: { top: 0.82, bottom: 0.04 } });
    instance.priceScale('right').applyOptions({ scaleMargins: { top: 0.08, bottom: 0.2 } });
    candle.current = candleSeries;
    volume.current = volumeSeries;
    instance.subscribeClick((param) => {
      if (typeof param.time === 'string') selectRef.current?.(param.time);
    });
    instance.subscribeCrosshairMove((param) => {
      const date = typeof param.time === 'string' ? param.time : null;
      const x = date ? instance.timeScale().timeToCoordinate(day(date)) : null;
      setHover(
        date && x != null
          ? {
              date,
              x,
              width: host.current?.clientWidth ?? 0,
              selectable: Boolean(selectableRef.current?.has(date)),
            }
          : null,
      );
      if (!date) {
        hoverRef.current?.(null);
        return;
      }
      hoverRef.current?.(rowsRef.current.find((row) => row.date === date) ?? null);
    });
    return () => {
      instance.remove();
      chart.current = null;
      candle.current = null;
      volume.current = null;
    };
  }, []);

  useEffect(() => {
    const candleSeries = candle.current;
    const volumeSeries = volume.current;
    if (!candleSeries || !volumeSeries) return;
    selectableRef.current = selectableDates ? new Set(selectableDates) : null;
    candleSeries.setData(buildCandleData(rows));
    volumeSeries.setData(buildVolumeData(rows));
    const last = rows.at(-1);
    if (last)
      candleSeries.applyOptions({ priceLineColor: last.close >= last.open ? tokens.up : tokens.down });
  }, [rows, selectableDates]);

  // Show the newest bars at the fixed spacing; move only when the selected day is out of view.
  const placedFor = useRef<PriceRow[] | null>(null);
  useEffect(() => {
    // autoSize measures the host on the next frame; placing before that uses a zero width.
    const frame = requestAnimationFrame(() => {
      const timeScale = chart.current?.timeScale();
      // The plot width, without the price axis, sets how many bars fit at the fixed spacing.
      const width = timeScale?.width() ?? 0;
      if (!timeScale || !rows.length || !width) return;
      timeScale.applyOptions({ barSpacing: BAR_SPACING });
      const visibleBars = width / BAR_SPACING;
      const index = selectedDate ? rows.findIndex((row) => row.date === selectedDate) : -1;
      const range = timeScale.getVisibleLogicalRange();
      const inView = index >= 0 && range != null && index >= range.from && index <= range.to;
      if (placedFor.current === rows && (index < 0 || inView)) return;
      placedFor.current = rows;
      if (index < 0 || index > rows.length - visibleBars / 2) {
        timeScale.scrollToRealTime();
      } else {
        timeScale.setVisibleLogicalRange({ from: index - visibleBars / 2, to: index + visibleBars / 2 });
      }
    });
    return () => cancelAnimationFrame(frame);
  }, [rows, selectedDate]);

  // Overlay positions (lane marks, hover column, selected band) follow pans, zooms and resizes.
  useEffect(() => {
    const instance = chart.current;
    if (!instance) return;
    const timeScale = instance.timeScale();
    const wanted = buildMarkerDates(selectableDates, selectedDate);
    let frame = 0;
    const measure = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        const xs = new Map<string, number>();
        wanted.forEach((value) => {
          const x = timeScale.timeToCoordinate(day(value));
          if (x != null) xs.set(value, x);
        });
        // Right edge of the shade for days before coverage: the coverage start, or the whole plot
        // when every visible bar is older, or nothing when none is.
        let coverX: number | null = null;
        if (coverageStart && rows.length && rows[0]!.date < coverageStart) {
          const startX = timeScale.timeToCoordinate(day(coverageStart));
          const visible = timeScale.getVisibleRange();
          if (startX != null) coverX = startX - timeScale.options().barSpacing / 2;
          else if (visible && String(visible.to) < coverageStart) coverX = timeScale.width();
        }
        setLayout({ barSpacing: timeScale.options().barSpacing, axisHeight: timeScale.height(), xs, coverX });
      });
    };
    measure();
    timeScale.subscribeVisibleLogicalRangeChange(measure);
    const observer = new ResizeObserver(measure);
    if (host.current) observer.observe(host.current);
    return () => {
      cancelAnimationFrame(frame);
      timeScale.unsubscribeVisibleLogicalRangeChange(measure);
      observer.disconnect();
    };
  }, [rows, selectableDates, selectedDate, coverageStart]);

  useEffect(() => {
    if (host.current) host.current.style.cursor = hover ? 'pointer' : 'default';
  }, [hover]);

  const explained = new Set(explainedDates.map(({ date }) => date));
  const markWidth = Math.max(3, Math.round(layout.barSpacing * 0.7));
  const bandWidth = Math.max(6, layout.barSpacing * 1.1);
  const selectedX = selectedDate ? layout.xs.get(selectedDate) : undefined;
  const hoverX = hover && hover.date !== selectedDate ? hover.x : undefined;
  const hoverIndex = hover ? rows.findIndex((row) => row.date === hover.date) : -1;
  const hoverMove = hoverIndex > 0 ? (rows[hoverIndex]!.close / rows[hoverIndex - 1]!.close - 1) * 100 : null;
  // Keep the chip inside the chart near either edge (the price axis sits on the right).
  const chipAlign = !hover
    ? ''
    : hover.x < 140
      ? 'translate-x-0'
      : hover.x > hover.width - 300
        ? '-translate-x-full'
        : '-translate-x-1/2';
  return (
    <div ref={host} className={`relative h-full min-h-0 min-w-0 ${className ?? ''}`}>
      {layout.coverX != null && layout.coverX > 0 && (
        <span
          aria-hidden="true"
          className="chart-before-coverage pointer-events-none absolute left-0 top-0 z-10"
          style={{ width: layout.coverX, bottom: layout.axisHeight }}
        >
          {layout.coverX > 160 && (
            <span className="chart-before-coverage-label type-caption">
              Before coverage · cannot be explained
            </span>
          )}
        </span>
      )}
      {hoverX != null && (
        <span
          aria-hidden="true"
          className={`chart-hover-column pointer-events-none absolute top-0 z-10 ${hover?.selectable ? '' : 'chart-hover-column-quiet'}`}
          style={{ left: hoverX, width: bandWidth, bottom: layout.axisHeight }}
        />
      )}
      {hover && (
        <span
          role="status"
          className={`chart-hint pointer-events-none absolute top-2 z-20 inline-flex items-center gap-2 whitespace-nowrap rounded-md border px-3 type-caption ${chipAlign} ${hover.selectable ? 'chart-hint-explain' : 'chart-hint-ordinary'}`}
          style={{ left: hover.x }}
        >
          <span className="font-numeric text-ink-100">
            {shortDate(hover.date)}
            {hoverMove != null && formatMove(hoverMove)}
          </span>
          <span aria-hidden="true" className="chart-hint-rule" />
          <span className="font-semibold">
            {hover.selectable
              ? 'Click to explain'
              : hoverMove != null && Math.abs(hoverMove) < 2
                ? 'Under 2% · click to read the news'
                : coverageStart && hover.date < coverageStart
                  ? 'Before coverage · click to read the news'
                  : 'Not prepared yet · click to read the news'}
          </span>
        </span>
      )}
      {selectedX != null && (
        <>
          <span
            aria-hidden="true"
            className="selected-date-band pointer-events-none absolute top-0 z-10"
            style={{ left: selectedX, width: bandWidth, bottom: layout.axisHeight }}
          />
          <span
            aria-hidden="true"
            className="selected-date-label pointer-events-none absolute bottom-0 z-10 -translate-x-1/2 rounded-sm px-1 font-numeric text-caption"
            style={{ left: selectedX }}
          >
            {shortDate(selectedDate!)}
          </span>
        </>
      )}
      {(selectableDates ?? []).map((value) => {
        const x = layout.xs.get(value);
        if (x == null) return null;
        const tone = value === selectedDate ? 'selected' : explained.has(value) ? 'explained' : 'ready';
        return (
          <span
            key={value}
            aria-hidden="true"
            className={`chart-lane-mark chart-lane-mark-${tone} pointer-events-none absolute z-10`}
            style={{ left: x - markWidth / 2, width: markWidth, bottom: layout.axisHeight + 3 }}
          />
        );
      })}
    </div>
  );
}
