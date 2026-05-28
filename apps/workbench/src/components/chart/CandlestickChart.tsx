import { useEffect, useRef, useState } from 'react';
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
}

export default function CandlestickChart({
  symbol,
  onHover,
  onDayClick,
}: Props) {
  const svgRef = useRef<SVGSVGElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [loading, setLoading] = useState(false);
  const marginRef = useRef({ top: 16, right: 48, bottom: 28, left: 56 });

  useEffect(() => {
    if (!symbol) return;
    setLoading(true);

    getOhlcv(symbol)
      .then((res) => {
        const validCandles = res.candles.filter(
          (c) => c.date && c.open != null && c.high != null && c.low != null && c.close != null
        );
        if (validCandles.length > 0) {
          const data = validCandles.map((c) => ({
            date: c.date,
            open: c.open!,
            high: c.high!,
            low: c.low!,
            close: c.close!,
            volume: c.volume ?? 0,
          }));
          drawChart(data);
        } else {
          drawChart(buildMockOhlc(symbol));
        }
      })
      .catch(() => {
        drawChart(buildMockOhlc(symbol));
      })
      .finally(() => setLoading(false));
  }, [symbol]);

  function buildMockOhlc(seedText: string): OHLCRow[] {
    const out: OHLCRow[] = [];
    const end = new Date();
    const start = new Date(end);
    start.setDate(end.getDate() - 140);

    let seed = 0;
    for (let i = 0; i < seedText.length; i++) {
      seed = (seed * 31 + seedText.charCodeAt(i)) >>> 0;
    }
    const rand = () => {
      seed = (seed * 1664525 + 1013904223) >>> 0;
      return seed / 4294967296;
    };

    let price = 100 + rand() * 300;
    for (let d = new Date(start); d <= end; d.setDate(d.getDate() + 1)) {
      const day = d.getDay();
      if (day === 0 || day === 6) continue;

      const drift = (rand() - 0.5) * 0.04;
      const open = price;
      const close = open * (1 + drift);
      const high = Math.max(open, close) * (1 + rand() * 0.012);
      const low = Math.min(open, close) * (1 - rand() * 0.012);
      const volume = 1_000_000 + Math.floor(rand() * 40_000_000);

      out.push({
        date: d.toISOString().slice(0, 10),
        open: Number(open.toFixed(2)),
        high: Number(high.toFixed(2)),
        low: Number(low.toFixed(2)),
        close: Number(close.toFixed(2)),
        volume,
      });

      price = close;
    }
    return out;
  }

  function drawChart(rawData: OHLCRow[]) {
    const svg = d3.select(svgRef.current);
    svg.selectAll('*').remove();

    const container = containerRef.current;
    if (!container || rawData.length === 0) return;

    const fullWidth = container.clientWidth;
    const fullHeight = container.clientHeight || 600;
    const margin = marginRef.current;
    const width = fullWidth - margin.left - margin.right;
    const height = fullHeight - margin.top - margin.bottom;

    svg.attr('width', fullWidth).attr('height', fullHeight);

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

    const y = d3.scaleLinear()
      .domain([d3.min(data, (d) => d.low)! * 0.92, d3.max(data, (d) => d.high)! * 1.03])
      .range([height, 0]);

    // Clip path so candles don't overflow
    svg.append('defs').append('clipPath')
      .attr('id', 'chart-clip')
      .append('rect')
      .attr('width', width)
      .attr('height', height);

    const chartArea = g.append('g').attr('clip-path', 'url(#chart-clip)');

    // Grid lines
    g.append('g')
      .attr('class', 'grid-y')
      .call(
        d3.axisLeft(y)
          .ticks(8)
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
      .call(d3.axisBottom(xBase).ticks(8).tickFormat(d3.timeFormat('%b %y') as any));

    xAxisGroup.selectAll('text').style('font-size', '11px').style('fill', '#64748b');

    g.append('g')
      .call(d3.axisLeft(y).ticks(6).tickFormat((d) => `$${Number(d).toFixed(0)}`))
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
      .attr('fill', (d) => (d.close >= d.open ? '#22c55e' : '#ef4444'));

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

        xAxisGroup.call(d3.axisBottom(xCurrent).ticks(8).tickFormat(d3.timeFormat('%b %y') as any));
        xAxisGroup.selectAll('text').style('font-size', '11px').style('fill', '#64748b');

        wicks
          .attr('x1', (d) => xCurrent(d.date))
          .attr('x2', (d) => xCurrent(d.date));

        bodies
          .attr('x', (d) => xCurrent(d.date) - scaledCandleWidth / 2)
          .attr('width', scaledCandleWidth);
      });

    overlay.call(zoom as any).on('dblclick.zoom', null);
  }

  return (
    <div ref={containerRef} className="chart-container">
      {loading && <div className="chart-loading">Loading...</div>}
      <svg ref={svgRef}></svg>
    </div>
  );
}
