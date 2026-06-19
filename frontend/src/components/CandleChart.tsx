'use client';

import { useEffect, useRef } from 'react';
import { createChart, CandlestickSeries, HistogramSeries, ColorType, type IChartApi, type ISeriesApi } from 'lightweight-charts';
import type { Candle } from '@/lib/types';

interface CandleChartProps {
  candles: Candle[];
  height?: number;
}

export default function CandleChart({ candles, height = 500 }: CandleChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null);
  const volumeSeriesRef = useRef<ISeriesApi<'Histogram'> | null>(null);

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
      rightPriceScale: {
        borderColor: '#1e1e38',
        scaleMargins: { top: 0.1, bottom: 0.25 },
      },
      timeScale: {
        borderColor: '#1e1e38',
        timeVisible: true,
        secondsVisible: false,
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

    return () => {
      resizeObserver.disconnect();
      chart.remove();
      chartRef.current = null;
    };
  }, [height]);

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
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    volumeSeriesRef.current.setData(volumeData as any);

    chartRef.current?.timeScale().fitContent();
  }, [candles]);

  return <div ref={containerRef} style={{ width: '100%' }} />;
}
