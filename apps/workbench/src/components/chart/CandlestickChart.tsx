import { useEffect, useId, useRef, useState } from 'react';
import * as d3 from 'd3';
import { getOhlcv } from '../../api/client';

interface OHLCRow {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

interface HoverData {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  change: number;
}

interface Props {
  symbol: string;
  onHover: (date: string | null, ohlc?: HoverData) => void;
  onDayClick?: (date: string, ohlc?: HoverData) => void;
  selectedDate?: string;
}

export default function CandlestickChart({
  symbol,
  onHover,
  onDayClick,
  selectedDate,
}: Props) {
  const svgRef = useRef<SVGSVGElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [chartData, setChartData] = useState<OHLCRow[]>([]);
  const clipId = `chart-clip-${useId().replace(/:/g, '')}`;

  useEffect(() => {
    if (!symbol) return;

    let active = true;
    setLoading(true);
    setError(null);
    setChartData([]);

    getOhlcv(symbol)
      .then((res) => {
        const validCandles = res.candles.filter(
          (c) => c.date && c.open != null && c.high != null && c.low != null && c.close != null
        );
        if (!active) return;
        if (validCandles.length === 0) {
          setError(`No OHLCV data is available for ${symbol}.`);
          return;
        }

        setChartData(validCandles.map((c) => ({
          date: c.date,
          open: c.open!,
          high: c.high!,
          low: c.low!,
          close: c.close!,
          volume: c.volume ?? 0,
        })));
      })
      .catch((requestError) => {
        if (!active) return;
        setError(requestError instanceof Error
          ? requestError.message
          : `Unable to load OHLCV data for ${symbol}.`);
      })
      .finally(() => {
        if (active) setLoading(false);
      });

    return () => {
      active = false;
    };
  }, [symbol]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    if (chartData.length === 0) {
      d3.select(svgRef.current).selectAll('*').remove();
      return;
    }

    const redraw = () => drawChart(chartData);
    redraw();

    const observer = new ResizeObserver(redraw);
    observer.observe(container);
    return () => observer.disconnect();
  }, [chartData, selectedDate]);

  function drawChart(rawData: OHLCRow[]) {
    const svg = d3.select(svgRef.current);
    svg.selectAll('*').remove();

    const container = containerRef.current;
    if (!container || rawData.length === 0) return;

    const fullWidth = container.clientWidth;
    const fullHeight = container.clientHeight || 600;
    const compact = fullWidth < 520;
    const margin = compact
      ? { top: 14, right: 12, bottom: 26, left: 44 }
      : { top: 16, right: 24, bottom: 28, left: 52 };
    const width = fullWidth - margin.left - margin.right;
    const height = fullHeight - margin.top - margin.bottom;

    if (width <= 0 || height <= 0) return;

    svg
      .attr('width', fullWidth)
      .attr('height', fullHeight)
      .attr('viewBox', `0 0 ${fullWidth} ${fullHeight}`);

    const g = svg.append('g').attr('transform', `translate(${margin.left},${margin.top})`);

    const data = rawData.map((d, i) => ({
      date: new Date(d.date),
      dateStr: d.date,
      open: +d.open,
      high: +d.high,
      low: +d.low,
      close: +d.close,
      volume: +d.volume,
      change: i > 0 ? ((+d.close - +rawData[i - 1].close) / +rawData[i - 1].close) * 100 : 0,
    }));

    const xBase = d3.scaleTime()
      .domain(d3.extent(data, (d) => d.date) as [Date, Date])
      .range([0, width]);

    const targetTickCount = compact ? 4 : 5;
    const tickStep = Math.max(1, Math.ceil((data.length - 1) / targetTickCount));
    const axisTickValues = data
      .filter((_, index) => index === 0 || index === data.length - 1 || index % tickStep === 0)
      .map((datum) => datum.date);

    const y = d3.scaleLinear()
      .domain([d3.min(data, (d) => d.low)! * 0.92, d3.max(data, (d) => d.high)! * 1.03])
      .range([height, 0]);

    // Clip path so candles don't overflow
    svg.append('defs').append('clipPath')
      .attr('id', clipId)
      .append('rect')
      .attr('width', width)
      .attr('height', height);

    const chartArea = g.append('g').attr('clip-path', `url(#${clipId})`);

    // Grid lines
    g.append('g')
      .attr('class', 'grid-y')
      .call(
        d3.axisLeft(y)
          .ticks(compact ? 4 : 7)
          .tickSize(-width)
          .tickFormat(() => '')
      )
      .selectAll('line')
      .style('stroke', '#1a2030')
      .style('stroke-width', 1);
    g.selectAll('.grid-y .domain').remove();

    const xAxisGroup = g.append('g')
      .attr('class', 'x-axis')
      .attr('transform', `translate(0,${height})`)
      .call(d3.axisBottom(xBase).tickValues(axisTickValues).tickFormat(d3.timeFormat('%b %d') as any));

    xAxisGroup.selectAll('text').style('font-size', '11px').style('fill', '#64748b');

    g.append('g')
      .call(d3.axisLeft(y).ticks(compact ? 4 : 6).tickFormat((d) => `$${Number(d).toFixed(0)}`))
      .selectAll('text')
      .style('font-size', '11px')
      .style('fill', '#64748b');

    g.selectAll('.domain').style('stroke', '#1e293b');
    g.selectAll('.tick line').style('stroke', '#1e293b');

    const candleWidthBase = Math.max(1.5, (width / data.length) * 0.65);

    const candles = chartArea.selectAll('.candle').data(data).enter().append('g').attr('class', 'candle');

    const wicks = candles.append('line')
      .attr('x1', (d) => xBase(d.date))
      .attr('x2', (d) => xBase(d.date))
      .attr('y1', (d) => y(d.high))
      .attr('y2', (d) => y(d.low))
      .attr('stroke', (d) => (d.close >= d.open ? '#22c55e' : '#ef4444'))
      .attr('stroke-width', 1);

    const bodies = candles.append('rect')
      .attr('x', (d) => xBase(d.date) - candleWidthBase / 2)
      .attr('y', (d) => y(Math.max(d.open, d.close)))
      .attr('width', candleWidthBase)
      .attr('height', (d) => Math.max(1, Math.abs(y(d.open) - y(d.close))))
      .attr('fill', (d) => (d.close >= d.open ? '#22c55e' : '#ef4444'))
      .attr('stroke', (d) => d.dateStr === selectedDate ? '#79a9ff' : 'none')
      .attr('stroke-width', (d) => d.dateStr === selectedDate ? 2 : 0);

    if (selectedDate) {
      const selected = data.find((datum) => datum.dateStr === selectedDate);
      if (selected) {
        const selectedX = xBase(selected.date);
        chartArea.append('line')
          .attr('class', 'event-date-line')
          .attr('x1', selectedX)
          .attr('x2', selectedX)
          .attr('y1', 0)
          .attr('y2', height)
          .attr('stroke', '#79a9ff')
          .attr('stroke-width', 1)
          .attr('stroke-dasharray', '4,4');

        const markerX = Math.min(Math.max(selectedX - 42, 0), width - 84);
        const marker = g.append('g')
          .attr('class', 'event-date-label')
          .attr('transform', `translate(${markerX},0)`)
          .style('pointer-events', 'none');
        marker.append('rect')
          .attr('width', 84)
          .attr('height', 20)
          .attr('rx', 4)
          .attr('fill', '#1e293b')
          .attr('stroke', '#79a9ff');
        marker.append('text')
          .attr('x', 42)
          .attr('y', 14)
          .attr('text-anchor', 'middle')
          .attr('fill', '#e2e8f0')
          .attr('font-size', '11px')
          .text(selectedDate);
      }
    }

    // Crosshairs
    const crossV = g.append('line')
      .style('stroke', '#334155')
      .style('stroke-width', 0.5)
      .style('stroke-dasharray', '4,3')
      .style('display', 'none')
      .style('pointer-events', 'none');

    const crossH = g.append('line')
      .style('stroke', '#334155')
      .style('stroke-width', 0.5)
      .style('stroke-dasharray', '4,3')
      .style('display', 'none')
      .style('pointer-events', 'none');

    // Price label on Y axis
    const priceLabel = g.append('g').style('display', 'none').style('pointer-events', 'none');
    priceLabel.append('rect')
      .attr('fill', '#1e293b')
      .attr('rx', 3)
      .attr('width', 56)
      .attr('height', 18);
    priceLabel.append('text')
      .attr('fill', '#94a3b8')
      .attr('font-size', '11px')
      .attr('font-family', "'SF Mono', 'Fira Code', monospace")
      .attr('text-anchor', 'middle')
      .attr('dy', '13px');

    // Date label on X axis
    const dateLabel = g.append('g').style('display', 'none').style('pointer-events', 'none');
    dateLabel.append('rect')
      .attr('fill', '#1e293b')
      .attr('rx', 3)
      .attr('width', 80)
      .attr('height', 20);
    dateLabel.append('text')
      .attr('fill', '#94a3b8')
      .attr('font-size', '11px')
      .attr('font-family', "'SF Mono', 'Fira Code', monospace")
      .attr('text-anchor', 'middle')
      .attr('dy', '14px');

    // Current transform state
    let currentTransform = d3.zoomIdentity;

    function getXCurrent() {
      return currentTransform.rescaleX(xBase);
    }

    const bisect = d3.bisector<typeof data[0], Date>((d) => d.date).left;
    function snapToData(px: number) {
      const xCurrent = getXCurrent();
      const xDate = xCurrent.invert(px);
      const idx = bisect(data, xDate, 1);
      const d0 = data[idx - 1];
      const d1 = data[idx];
      if (!d0) return data[0];
      return d1 && xDate.getTime() - d0.date.getTime() > d1.date.getTime() - xDate.getTime() ? d1 : d0;
    }

    // Interaction overlay — sits on top, handles hover/click
    const overlay = g.append('rect')
      .attr('class', 'interaction-overlay')
      .attr('width', width)
      .attr('height', height)
      .attr('fill', 'transparent')
      .style('cursor', 'crosshair');

    overlay
      .on('mousemove', function (event) {
        const [mx, my] = d3.pointer(event);
        const xCurrent = getXCurrent();
        const d = snapToData(mx);
        const cx = xCurrent(d.date);
        const priceAtY = y.invert(my);

        crossV.attr('x1', cx).attr('x2', cx).attr('y1', 0).attr('y2', height).style('display', null);
        crossH.attr('x1', 0).attr('x2', width).attr('y1', my).attr('y2', my).style('display', null);

        priceLabel.style('display', null).attr('transform', `translate(${-56},${my - 9})`);
        priceLabel.select('text').attr('x', 28).text(`$${priceAtY.toFixed(2)}`);

        dateLabel.style('display', null).attr('transform', `translate(${cx - 40},${height})`);
        dateLabel.select('text').attr('x', 40).text(d.dateStr);

        onHover(d.dateStr, {
          date: d.dateStr,
          open: d.open,
          high: d.high,
          low: d.low,
          close: d.close,
          change: d.change,
        });
      })
      .on('mouseleave', function () {
        crossV.style('display', 'none');
        crossH.style('display', 'none');
        priceLabel.style('display', 'none');
        dateLabel.style('display', 'none');
        // Do NOT call onHover(null) — keep last hovered data visible
      })
      .on('click', function (event) {
        const [mx] = d3.pointer(event);
        const d = snapToData(mx);
        onDayClick?.(d.dateStr, {
          date: d.dateStr,
          open: d.open,
          high: d.high,
          low: d.low,
          close: d.close,
          change: d.change,
        });
      });

    // Zoom + Pan behavior — applied to overlay
    const zoom = d3.zoom<SVGRectElement, unknown>()
      .scaleExtent([0.5, 24])
      .translateExtent([[-width * 0.5, 0], [width * 1.5, height]])
      .extent([[0, 0], [width, height]])
      .filter((event: any) => {
        // Allow wheel (zoom) and drag (pan) always
        // Block right-click
        if (event.button) return false;
        return true;
      })
      .on('zoom', (event) => {
        currentTransform = event.transform;
        const xCurrent = currentTransform.rescaleX(xBase);
        const scaledCandleWidth = Math.max(1, Math.min(36, candleWidthBase * currentTransform.k));

        const visibleTicks = axisTickValues.filter((date) => {
          const position = xCurrent(date);
          return position >= 0 && position <= width;
        });
        xAxisGroup.call(d3.axisBottom(xCurrent).tickValues(visibleTicks).tickFormat(d3.timeFormat('%b %d') as any));
        xAxisGroup.selectAll('text').style('font-size', '11px').style('fill', '#64748b');

        wicks
          .attr('x1', (d) => xCurrent(d.date))
          .attr('x2', (d) => xCurrent(d.date));

        bodies
          .attr('x', (d) => xCurrent(d.date) - scaledCandleWidth / 2)
          .attr('width', scaledCandleWidth);

        if (selectedDate) {
          const selected = data.find((datum) => datum.dateStr === selectedDate);
          if (selected) {
            const selectedX = xCurrent(selected.date);
            chartArea.select('.event-date-line')
              .attr('x1', selectedX)
              .attr('x2', selectedX);
            const markerX = Math.min(Math.max(selectedX - 42, 0), width - 84);
            g.select('.event-date-label').attr('transform', `translate(${markerX},0)`);
          }
        }
      });

    overlay.call(zoom as any).on('dblclick.zoom', null);
  }

  return (
    <div ref={containerRef} className="chart-container">
      {loading && <div className="chart-loading">Loading...</div>}
      {error && <div className="chart-error" role="alert">{error}</div>}
      <svg ref={svgRef} role="img" aria-label={`${symbol} candlestick price chart`}></svg>
    </div>
  );
}
