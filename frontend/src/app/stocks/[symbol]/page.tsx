'use client';

import { useCallback, useEffect, useState } from 'react';
import { useParams } from 'next/navigation';
import { getStock, getCandles, backfillStock } from '@/lib/api';
import { createTrendline, deleteTrendline, getTrendlines } from '@/lib/filter-api';
import type { StockDetail, Candle, CandleInterval } from '@/lib/types';
import type { TrendlineDefinition, TrendlinePoint } from '@/lib/filter-types';
import { formatStockSymbol } from '@/lib/stock-display';
import CandleChart from '@/components/CandleChart';
import StockIdentity from '@/components/StockIdentity';

const INTERVALS: { key: CandleInterval; label: string; days: number }[] = [
  { key: '1m', label: '1m', days: 2 },
  { key: '5m', label: '5m', days: 10 },
  { key: '15m', label: '15m', days: 30 },
  { key: '1h', label: '1H', days: 60 },
  { key: '1d', label: '1D', days: 365 },
  { key: '1w', label: '1W', days: 730 },
];

function formatNum(n: number | null | undefined, decimals = 2): string {
  if (n == null) return '-';
  return n.toFixed(decimals);
}

export default function StockDetailPage() {
  const params = useParams();
  const symbol = (params.symbol as string)?.toUpperCase();

  const [stock, setStock] = useState<StockDetail | null>(null);
  const [candles, setCandles] = useState<Candle[]>([]);
  const [interval, setInterval] = useState<CandleInterval>('1d');
  const [loading, setLoading] = useState(true);
  const [chartLoading, setChartLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [backfilling, setBackfilling] = useState(false);
  const [backfillMsg, setBackfillMsg] = useState<string | null>(null);
  const [trendlines, setTrendlines] = useState<TrendlineDefinition[]>([]);
  const [drawingTrendline, setDrawingTrendline] = useState(false);

  // Load stock detail
  useEffect(() => {
    if (!symbol) return;
    setLoading(true);
    setError(null);
    getStock(symbol)
      .then(setStock)
      .catch(() => setError(`Stock "${symbol}" not found`))
      .finally(() => setLoading(false));
  }, [symbol]);

  // Load candles when interval changes
  useEffect(() => {
    if (!symbol) return;
    setChartLoading(true);
    const config = INTERVALS.find((i) => i.key === interval) || INTERVALS[4];
    getCandles(symbol, interval, config.days)
      .then(setCandles)
      .catch(() => setCandles([]))
      .finally(() => setChartLoading(false));
  }, [symbol, interval]);

  useEffect(() => {
    if (!symbol) return;
    getTrendlines(symbol, interval).then(setTrendlines).catch(() => setTrendlines([]));
  }, [symbol, interval]);

  const handleCreateTrendline = useCallback(async (points: TrendlinePoint[]) => {
    if (!symbol) return;
    try {
      const trendline = await createTrendline({ symbol, interval, points, trendline_type: 'manual' });
      setTrendlines((current) => [...current, trendline]);
      setDrawingTrendline(false);
    } catch (err) {
      setBackfillMsg(`Trendline save failed: ${err instanceof Error ? err.message : 'Unknown error'}`);
    }
  }, [symbol, interval]);

  if (loading) {
    return (
      <div>
        <div className="skeleton" style={{ width: 200, height: 24, marginBottom: 12 }} />
        <div className="skeleton" style={{ width: 300, height: 40, marginBottom: 24 }} />
        <div className="skeleton" style={{ width: '100%', height: 500 }} />
      </div>
    );
  }

  if (error || !stock) {
    return (
      <div className="card">
        <div className="empty-state">
          <div className="empty-state-icon">🔍</div>
          <div className="empty-state-text">{error || 'Stock not found'}</div>
        </div>
      </div>
    );
  }

  const price = stock.latest_candle?.close || 0;
  const prevClose = stock.prev_close || price;
  const change = price - prevClose;
  const changePct = prevClose > 0 ? (change / prevClose) * 100 : 0;
  const isUp = change >= 0;
  const ind = stock.indicators;

  return (
    <div>
      {/* Stock Header */}
      <div className="stock-header">
        <div className="stock-price-section">
          <div>
            <span className="stock-symbol-badge">{stock.exchange}</span>
            <span style={{ fontSize: 14, fontWeight: 600, color: 'var(--text-secondary)' }}>
              {formatStockSymbol(stock.symbol ?? '')}
            </span>
          </div>
          <div className="stock-name"><StockIdentity className="stock-detail-identity" symbol={stock.symbol} name={stock.name} company_name={stock.company_name} sector={stock.sector} close={price} market_cap_cr={stock.market_cap_cr} as_of_date={stock.latest_candle?.timestamp} /></div>
          <div className="stock-price" style={{ color: isUp ? 'var(--color-profit)' : 'var(--color-loss)' }}>
            ₹{price.toFixed(2)}
          </div>
          <div className="stock-change" style={{ color: isUp ? 'var(--color-profit)' : 'var(--color-loss)' }}>
            {isUp ? '▲' : '▼'} ₹{Math.abs(change).toFixed(2)} ({changePct >= 0 ? '+' : ''}{changePct.toFixed(2)}%)
          </div>

          <div className="stock-meta">
            <div className="stock-meta-item">
              <span className="stock-meta-label">Sector</span>
              <span className="stock-meta-value">{stock.sector || stock.sector_display || stock.sector_name || '—'}</span>
            </div>
            {stock.latest_candle && (
              <>
                <div className="stock-meta-item">
                  <span className="stock-meta-label">Day High</span>
                  <span className="stock-meta-value">₹{formatNum(stock.latest_candle.high)}</span>
                </div>
                <div className="stock-meta-item">
                  <span className="stock-meta-label">Day Low</span>
                  <span className="stock-meta-value">₹{formatNum(stock.latest_candle.low)}</span>
                </div>
                <div className="stock-meta-item">
                  <span className="stock-meta-label">Volume</span>
                  <span className="stock-meta-value">{stock.latest_candle.volume?.toLocaleString() || '-'}</span>
                </div>
              </>
            )}
            {ind?.high_52w ? (
              <div className="stock-meta-item">
                <span className="stock-meta-label">52W Range</span>
                <span className="stock-meta-value">
                  ₹{formatNum(ind.low_52w)} - ₹{formatNum(ind.high_52w)}
                </span>
              </div>
            ) : null}
          </div>
        </div>
      </div>

      {/* Chart */}
      <div className="chart-container">
        <div className="chart-toolbar">
          <div className="interval-tabs">
            {INTERVALS.map((i) => (
              <button
                key={i.key}
                className={`interval-tab ${interval === i.key ? 'active' : ''}`}
                onClick={() => setInterval(i.key)}
              >
                {i.label}
              </button>
            ))}
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <div style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>
              {candles.length.toLocaleString()} candles
              {chartLoading && ' · Loading...'}
            </div>
            <button className={`btn ${drawingTrendline ? 'btn-primary' : 'btn-secondary'}`} onClick={() => setDrawingTrendline((current) => !current)}>{drawingTrendline ? 'Click two points…' : 'Draw trendline'}</button>
          </div>
        </div>
        {candles.length > 0 ? (
          <CandleChart candles={candles} height={500} trendlines={trendlines} drawingEnabled={drawingTrendline} onCreateTrendline={handleCreateTrendline} />
        ) : (
          <div className="empty-state" style={{ height: 500 }}>
            <div className="empty-state-icon">📊</div>
            <div className="empty-state-text">
              {chartLoading ? 'Loading chart data...' : 'No candle data available for this interval'}
            </div>
            {!chartLoading && (
              <button
                className="btn btn-primary"
                style={{ marginTop: 16, padding: '10px 24px', fontSize: 14, cursor: backfilling ? 'wait' : 'pointer' }}
                disabled={backfilling}
                onClick={async () => {
                  setBackfilling(true);
                  setBackfillMsg('Fetching historical data... this may take 15-30s');
                  try {
                    const result = await backfillStock(symbol);
                    setBackfillMsg(`Done! ${result.candles_stored.toLocaleString()} candles fetched`);
                    // Reload candles
                    const config = INTERVALS.find((i) => i.key === interval) || INTERVALS[4];
                    const fresh = await getCandles(symbol, interval, config.days);
                    setCandles(fresh);
                  } catch (err: unknown) {
                    setBackfillMsg(`Backfill failed: ${err instanceof Error ? err.message : 'Unknown error'}`);
                  } finally {
                    setBackfilling(false);
                  }
                }}
              >
                {backfilling ? '⏳ Fetching...' : '📥 Fetch Historical Data'}
              </button>
            )}
            {backfillMsg && (
              <div style={{ marginTop: 8, fontSize: 12, color: 'var(--text-tertiary)' }}>
                {backfillMsg}
              </div>
            )}
          </div>
        )}
      </div>

      {trendlines.length > 0 && (
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 10 }}>
          {trendlines.map((trendline) => (
            <button key={trendline.id} className="btn btn-secondary" onClick={async () => { await deleteTrendline(trendline.id); setTrendlines((current) => current.filter((item) => item.id !== trendline.id)); }}>
              Remove line #{trendline.id}
            </button>
          ))}
        </div>
      )}

      {/* Technical Indicators */}
      {ind && (
        <div className="section" style={{ marginTop: 24 }}>
          <div className="section-header">
            <h2 className="section-title">Technical Indicators</h2>
            <span className="section-subtitle">As of {ind.date}</span>
          </div>

          <div className="indicators-grid">
            {/* Momentum */}
            <div className="indicator-item">
              <div className="indicator-label">RSI (14)</div>
              <div
                className="indicator-value"
                style={{
                  color:
                    (ind.rsi_14 || 50) > 70
                      ? 'var(--color-loss)'
                      : (ind.rsi_14 || 50) < 30
                      ? 'var(--color-profit)'
                      : 'var(--text-primary)',
                }}
              >
                {formatNum(ind.rsi_14)}
              </div>
              <div className="indicator-sub">
                {(ind.rsi_14 || 50) > 70 ? 'Overbought' : (ind.rsi_14 || 50) < 30 ? 'Oversold' : 'Neutral'}
              </div>
            </div>

            <div className="indicator-item">
              <div className="indicator-label">MACD</div>
              <div
                className="indicator-value"
                style={{
                  color:
                    (ind.macd_histogram || 0) > 0
                      ? 'var(--color-profit)'
                      : 'var(--color-loss)',
                }}
              >
                {formatNum(ind.macd_line)}
              </div>
              <div className="indicator-sub">
                Signal: {formatNum(ind.macd_signal)} | Hist: {formatNum(ind.macd_histogram)}
              </div>
            </div>

            <div className="indicator-item">
              <div className="indicator-label">ADX (14)</div>
              <div className="indicator-value">{formatNum(ind.adx_14)}</div>
              <div className="indicator-sub">
                {(ind.adx_14 || 0) > 25 ? 'Strong Trend' : 'Ranging'}
              </div>
            </div>

            {/* Trend */}
            <div className="indicator-item">
              <div className="indicator-label">Supertrend</div>
              <div
                className="indicator-value"
                style={{
                  color:
                    ind.supertrend_direction === 1
                      ? 'var(--color-profit)'
                      : 'var(--color-loss)',
                }}
              >
                ₹{formatNum(ind.supertrend)}
              </div>
              <div className="indicator-sub">
                {ind.supertrend_direction === 1 ? '↑ Bullish' : '↓ Bearish'}
              </div>
            </div>

            <div className="indicator-item">
              <div className="indicator-label">SMA 20 / 50 / 200</div>
              <div className="indicator-value" style={{ fontSize: 14 }}>
                {formatNum(ind.sma_20)} / {formatNum(ind.sma_50)} / {formatNum(ind.sma_200)}
              </div>
            </div>

            <div className="indicator-item">
              <div className="indicator-label">EMA 20 / 50</div>
              <div className="indicator-value" style={{ fontSize: 14 }}>
                {formatNum(ind.ema_20)} / {formatNum(ind.ema_50)}
              </div>
            </div>

            {/* Volatility */}
            <div className="indicator-item">
              <div className="indicator-label">ATR (14)</div>
              <div className="indicator-value">₹{formatNum(ind.atr_14)}</div>
              <div className="indicator-sub">Average True Range</div>
            </div>

            <div className="indicator-item">
              <div className="indicator-label">Bollinger Bands</div>
              <div className="indicator-value" style={{ fontSize: 14 }}>
                {formatNum(ind.bollinger_lower)} - {formatNum(ind.bollinger_upper)}
              </div>
              <div className="indicator-sub">Width: {formatNum(ind.bollinger_width, 4)}</div>
            </div>

            {/* Volume */}
            <div className="indicator-item">
              <div className="indicator-label">Delivery %</div>
              <div className="indicator-value">{formatNum(ind.delivery_pct)}%</div>
            </div>

            <div className="indicator-item">
              <div className="indicator-label">OBV</div>
              <div className="indicator-value">
                {ind.obv ? (ind.obv / 1_000_000).toFixed(2) + 'M' : '-'}
              </div>
              <div className="indicator-sub">On-Balance Volume</div>
            </div>

            {/* Pivots */}
            <div className="indicator-item">
              <div className="indicator-label">Pivot Point</div>
              <div className="indicator-value">₹{formatNum(ind.pivot_point)}</div>
              <div className="indicator-sub">
                R1: {formatNum(ind.pivot_r1)} | R2: {formatNum(ind.pivot_r2)}
              </div>
            </div>

            <div className="indicator-item">
              <div className="indicator-label">Support / Resistance</div>
              <div className="indicator-value" style={{ fontSize: 14 }}>
                S: ₹{formatNum(ind.support_level)} | R: ₹{formatNum(ind.resistance_level)}
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
