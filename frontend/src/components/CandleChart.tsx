'use client';

import { useEffect, useRef, useState } from 'react';
import { createChart, CandlestickSeries, HistogramSeries, ColorType, LineStyle, TickMarkType, type IChartApi, type ISeriesApi, type Time } from 'lightweight-charts';
import type { Candle } from '@/lib/types';
import type { TrendlineDefinition, TrendlinePoint } from '@/lib/filter-types';

const NSE_DATE_TIME_FORMATTER = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short', year: '2-digit',
  hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
});
const NSE_DATE_FORMATTER = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short', year: '2-digit',
});
const NSE_DAY_TICK_FORMATTER = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'Asia/Kolkata', day: 'numeric',
});
const NSE_MONTH_TICK_FORMATTER = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'Asia/Kolkata', month: 'short',
});
const NSE_YEAR_TICK_FORMATTER = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'Asia/Kolkata', year: 'numeric',
});
const NSE_TIME_TICK_FORMATTER = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
});
const NSE_TIME_WITH_SECONDS_TICK_FORMATTER = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
});

function chartTimeAsDate(time: Time): Date {
  if (typeof time === 'number') return new Date(time * 1000);
  if (typeof time === 'string') return new Date(`${time}T00:00:00Z`);
  return new Date(Date.UTC(time.year, time.month - 1, time.day));
}

function formatNseCrosshairTime(time: Time): string {
  const date = chartTimeAsDate(time);
  return typeof time === 'number' ? NSE_DATE_TIME_FORMATTER.format(date) : NSE_DATE_FORMATTER.format(date);
}

function formatNseTickMark(time: Time, tickMarkType: TickMarkType): string | null {
  const date = chartTimeAsDate(time);
  switch (tickMarkType) {
    case TickMarkType.Year:
      return NSE_YEAR_TICK_FORMATTER.format(date);
    case TickMarkType.Month:
      return NSE_MONTH_TICK_FORMATTER.format(date);
    case TickMarkType.DayOfMonth:
      return NSE_DAY_TICK_FORMATTER.format(date);
    case TickMarkType.Time:
      return NSE_TIME_TICK_FORMATTER.format(date);
    case TickMarkType.TimeWithSeconds:
      return NSE_TIME_WITH_SECONDS_TICK_FORMATTER.format(date);
    default:
      return null;
  }
}

interface CandleChartProps {
  candles: Candle[];
  height?: number;
  trendlines?: TrendlineDefinition[];
  drawingEnabled?: boolean;
  onCreateTrendline?: (points: TrendlinePoint[]) => void;
}

export default function CandleChart({ candles, height = 500, trendlines = [], drawingEnabled = false, onCreateTrendline }: CandleChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null);
  const openingPriceLineRef = useRef<ReturnType<ISeriesApi<'Candlestick'>['createPriceLine']> | null>(null);
  const volumeSeriesRef = useRef<ISeriesApi<'Histogram'> | null>(null);
  const [draftPoints, setDraftPoints] = useState<TrendlinePoint[]>([]);
  const [renderVersion, setRenderVersion] = useState(0);

  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: '#0d0d15' },
        textColor: '#8888a8',
        fontSize: 11,
        fontFamily: "'JetBrains Mono', 'Inter', monospace",
      },
      grid: {
        vertLines: { color: '#1a1a2e' },
        horzLines: { color: '#1a1a2e' },
      },
      crosshair: {
        mode: 0,
        vertLine: { color: '#4a4a6a', width: 1, style: 2, labelBackgroundColor: '#2a2a48' },
        horzLine: { color: '#4a4a6a', width: 1, style: 2, labelBackgroundColor: '#2a2a48' },
      },
      localization: {
        timeFormatter: formatNseCrosshairTime,
      },
      rightPriceScale: {
        borderColor: '#1e1e38',
        scaleMargins: { top: 0.1, bottom: 0.25 },
      },
      timeScale: {
        borderColor: '#1e1e38',
        timeVisible: true,
        secondsVisible: false,
        tickMarkFormatter: formatNseTickMark,
      },
      width: containerRef.current.clientWidth,
      height,
    });

    // Candlestick series (v5 API)
    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: '#00c853',
      downColor: '#ff1744',
      borderUpColor: '#00c853',
      borderDownColor: '#ff1744',
      wickUpColor: '#00c853',
      wickDownColor: '#ff1744',
    });

    // Volume histogram (v5 API)
    const volumeSeries = chart.addSeries(HistogramSeries, {
      priceFormat: { type: 'volume' },
      priceScaleId: 'volume',
    });

    chart.priceScale('volume').applyOptions({
      scaleMargins: { top: 0.85, bottom: 0 },
    });

    chartRef.current = chart;
    candleSeriesRef.current = candleSeries;
    volumeSeriesRef.current = volumeSeries;

    const resizeObserver = new ResizeObserver((entries) => {
      for (const entry of entries) {
        chart.applyOptions({ width: entry.contentRect.width });
      }
    });
    resizeObserver.observe(containerRef.current);

    const handleVisibleRange = () => setRenderVersion((value) => value + 1);
    chart.timeScale().subscribeVisibleTimeRangeChange(handleVisibleRange);

    return () => {
      resizeObserver.disconnect();
      chart.timeScale().unsubscribeVisibleTimeRangeChange(handleVisibleRange);
      chart.remove();
      chartRef.current = null;
      candleSeriesRef.current = null;
      openingPriceLineRef.current = null;
      volumeSeriesRef.current = null;
    };
  }, [height]);

  useEffect(() => {
    const chart = chartRef.current;
    const candleSeries = candleSeriesRef.current;
    if (!chart || !candleSeries || !drawingEnabled) return;
    const handleClick = (param: Parameters<NonNullable<Parameters<IChartApi['subscribeClick']>[0]>>[0]) => {
      if (!param.point) return;
      const time = chart.timeScale().coordinateToTime(param.point.x);
      const price = candleSeries.coordinateToPrice(param.point.y);
      if (typeof time !== 'number' || price == null) return;
      const point = { time, price };
      setDraftPoints((current) => {
        const next = [...current, point];
        if (next.length === 2) {
          onCreateTrendline?.(next);
          return [];
        }
        return next;
      });
    };
    chart.subscribeClick(handleClick);
    return () => chart.unsubscribeClick(handleClick);
  }, [drawingEnabled, onCreateTrendline]);

  // Update data
  useEffect(() => {
    if (!candleSeriesRef.current || !volumeSeriesRef.current || !candles.length) return;

    const candleData = candles.map((c) => ({
      time: c.time as number,
      open: c.open,
      high: c.high,
      low: c.low,
      close: c.close,
    }));

    const volumeData = candles.map((c) => ({
      time: c.time as number,
      value: c.volume,
      color: c.close >= c.open ? 'rgba(0, 200, 83, 0.25)' : 'rgba(255, 23, 68, 0.25)',
    }));

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    candleSeriesRef.current.setData(candleData as any);

    if (openingPriceLineRef.current) {
      candleSeriesRef.current.removePriceLine(openingPriceLineRef.current);
    }
    openingPriceLineRef.current = candleSeriesRef.current.createPriceLine({
      price: candles[0].open,
      color: '#a6a6b8',
      lineWidth: 2,
      lineStyle: LineStyle.Solid,
      lineVisible: true,
      axisLabelVisible: true,
      title: 'Open',
    });

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    volumeSeriesRef.current.setData(volumeData as any);

    chartRef.current?.timeScale().fitContent();
  }, [candles]);

  const lines = [...trendlines.map((trendline) => trendline.points_json), ...(draftPoints.length === 2 ? [draftPoints] : [])];
  const lineCoordinates = lines.map((points) => {
    if (points.length < 2 || !chartRef.current || !candleSeriesRef.current) return null;
    const mapped = points.map((point) => ({
      x: chartRef.current?.timeScale().timeToCoordinate(point.time as never),
      y: candleSeriesRef.current?.priceToCoordinate(point.price),
    }));
    return mapped.every((point) => point.x != null && point.y != null) ? mapped as Array<{ x: number; y: number }> : null;
  }).filter((line): line is Array<{ x: number; y: number }> => line != null);

  return (
    <div style={{ width: '100%', position: 'relative' }}>
      <div ref={containerRef} style={{ width: '100%' }} />
      <svg key={renderVersion} aria-hidden="true" style={{ position: 'absolute', inset: 0, width: '100%', height, pointerEvents: 'none' }}>
        {lineCoordinates.map((line, index) => <line key={index} x1={line[0].x} y1={line[0].y} x2={line[1].x} y2={line[1].y} stroke={index === lineCoordinates.length - 1 && draftPoints.length === 2 ? '#f59e0b' : '#60a5fa'} strokeWidth="2" strokeDasharray={index === lineCoordinates.length - 1 && draftPoints.length === 2 ? '4 4' : undefined} />)}
      </svg>
    </div>
  );
}
