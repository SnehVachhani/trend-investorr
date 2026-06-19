/* ═══════════════════════════════════════════════════════════
   Trend-Investorr — API Client
   Typed fetch wrapper for the FastAPI backend
   ═══════════════════════════════════════════════════════════ */

import type {
  Stock,
  StockDetail,
  Candle,
  TechnicalIndicators,
  Signal,
  ScannerSignal,
  Order,
  Position,
  DailyPnL,
  PortfolioSummary,
  Watchlist,
  SystemStatus,
  AppConfig,
  CandleInterval,
  Algorithm,
  BacktestRun,
  BacktestParams,
} from './types';

const BASE_URL = process.env.NEXT_PUBLIC_API_URL || '';

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    cache: 'no-store',
  });
  if (!res.ok) {
    throw new Error(`API error ${res.status}: ${res.statusText}`);
  }
  return res.json();
}

// ── Stocks ─────────────────────────────────────────────────

export async function searchStocks(q: string, limit = 20): Promise<Stock[]> {
  const data = await get<{ stocks: Stock[] }>(`/api/stocks?q=${encodeURIComponent(q)}&limit=${limit}`);
  return data.stocks;
}

export async function listStocks(limit = 50, offset = 0): Promise<Stock[]> {
  const data = await get<{ stocks: Stock[] }>(`/api/stocks?limit=${limit}&offset=${offset}`);
  return data.stocks;
}

export async function getStock(symbol: string): Promise<StockDetail> {
  return get<StockDetail>(`/api/stocks/${symbol}`);
}

// ── Candles ────────────────────────────────────────────────

export async function getCandles(
  symbol: string,
  interval: CandleInterval = '1d',
  days = 90,
): Promise<Candle[]> {
  const data = await get<{ candles: Candle[] }>(
    `/api/stocks/${symbol}/candles?interval=${interval}&days=${days}`,
  );
  return data.candles;
}

export async function backfillStock(
  symbol: string,
  days = 365,
): Promise<{ symbol: string; status: string; candles_stored: number; intervals_done: number; errors: number }> {
  return post(`/api/stocks/${symbol}/backfill?days=${days}`, {});
}

// ── Indicators ─────────────────────────────────────────────

export async function getIndicators(symbol: string, days = 30) {
  const data = await get<{ indicators: TechnicalIndicators[] }>(
    `/api/stocks/${symbol}/indicators?days=${days}`,
  );
  return data.indicators;
}

// ── Watchlists ─────────────────────────────────────────────

export async function getWatchlists(): Promise<Watchlist[]> {
  const data = await get<{ watchlists: Watchlist[] }>('/api/watchlists');
  return data.watchlists;
}

export async function getWatchlistStocks(id: number): Promise<Stock[]> {
  const data = await get<{ stocks: Stock[] }>(`/api/watchlists/${id}/stocks`);
  return data.stocks;
}

export async function createWatchlist(params: {
  name: string;
  description?: string;
  list_type?: string;
  max_stocks?: number;
}): Promise<{ id: number; name: string; status: string }> {
  return post('/api/watchlists', params);
}

export async function deleteWatchlist(id: number): Promise<void> {
  await del(`/api/watchlists/${id}`);
}

export async function addStockToWatchlist(
  watchlistId: number,
  stockId: number,
  source = 'manual',
): Promise<{ added: boolean; symbol: string }> {
  return post(`/api/watchlists/${watchlistId}/stocks`, {
    stock_id: stockId,
    source,
  });
}

export async function removeStockFromWatchlist(
  watchlistId: number,
  stockId: number,
): Promise<void> {
  await del(`/api/watchlists/${watchlistId}/stocks/${stockId}`);
}

export async function populateWatchlist(
  watchlistId: number,
  method: 'volume' | 'all_active' = 'volume',
  limit = 500,
): Promise<{ stocks_added: number; watchlist_name: string }> {
  return post(`/api/watchlists/${watchlistId}/populate`, { method, limit });
}

export interface LivePrice {
  ltp: number | null;
  prev_close: number | null;
  change: number | null;
  change_pct: number | null;
  day_open: number | null;
  day_high: number | null;
  day_low: number | null;
  volume: number | null;
}

export async function getWatchlistPrices(
  watchlistId: number,
): Promise<Record<string, LivePrice>> {
  const data = await get<{ prices: Record<string, LivePrice> }>(
    `/api/watchlists/${watchlistId}/prices`,
  );
  return data.prices;
}

export async function reorderWatchlistStocks(
  watchlistId: number,
  stockIds: number[],
): Promise<void> {
  await put(`/api/watchlists/${watchlistId}/reorder`, { stock_ids: stockIds });
}

export interface StockChange {
  stock_id: number;
  symbol: string;
  name: string;
  close: number | null;
  '1d': number | null;
  '5d': number | null;
  '10d': number | null;
  '20d': number | null;
  '30d': number | null;
  '60d': number | null;
  '90d': number | null;
}

export async function getWatchlistChanges(
  watchlistId: number,
): Promise<StockChange[]> {
  const data = await get<{ changes: StockChange[] }>(
    `/api/watchlists/${watchlistId}/changes`,
  );
  return data.changes;
}

// ── Signals ────────────────────────────────────────────────

export async function getSignals(dateFilter?: string, limit = 50): Promise<Signal[]> {
  const params = new URLSearchParams();
  if (dateFilter) params.set('date', dateFilter);
  params.set('limit', String(limit));
  const data = await get<{ signals: Signal[] }>(`/api/signals?${params}`);
  return data.signals;
}

export async function getScannerSignals(dateFilter?: string, limit = 50): Promise<ScannerSignal[]> {
  const params = new URLSearchParams();
  if (dateFilter) params.set('date', dateFilter);
  params.set('limit', String(limit));
  const data = await get<{ scanner_signals: ScannerSignal[] }>(`/api/scanner-signals?${params}`);
  return data.scanner_signals;
}

// ── Orders ─────────────────────────────────────────────────

export async function getOrders(limit = 50): Promise<Order[]> {
  const data = await get<{ orders: Order[] }>(`/api/orders?limit=${limit}`);
  return data.orders;
}

// ── Portfolio ──────────────────────────────────────────────

export async function getPortfolioSummary(): Promise<PortfolioSummary> {
  return get<PortfolioSummary>('/api/portfolio/summary');
}

export async function getPnL(days = 30): Promise<DailyPnL[]> {
  const data = await get<{ pnl: DailyPnL[] }>(`/api/portfolio/pnl?days=${days}`);
  return data.pnl;
}

// ── Positions ──────────────────────────────────────────────

export async function getPositions(): Promise<Position[]> {
  const data = await get<{ positions: Position[] }>('/api/positions');
  return data.positions;
}

// ── System ─────────────────────────────────────────────────

export async function getSystemStatus(): Promise<SystemStatus> {
  return get<SystemStatus>('/api/system/status');
}

export async function getSystemConfig(): Promise<AppConfig[]> {
  const data = await get<{ config: AppConfig[] }>('/api/system/config');
  return data.config;
}

// ── Backtest ───────────────────────────────────────────────

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || `API error ${res.status}`);
  }
  return res.json();
}

async function del<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, { method: 'DELETE' });
  if (!res.ok) {
    throw new Error(`API error ${res.status}: ${res.statusText}`);
  }
  return res.json();
}

async function put<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || `API error ${res.status}`);
  }
  return res.json();
}

export async function getAlgorithms(): Promise<Algorithm[]> {
  const data = await get<{ algorithms: Algorithm[] }>('/api/algorithms');
  return data.algorithms;
}

export async function runBacktest(params: BacktestParams): Promise<{ run_id: number; status: string }> {
  return post('/api/backtest', params);
}

export async function getBacktestHistory(limit = 50): Promise<BacktestRun[]> {
  const data = await get<{ runs: BacktestRun[] }>(`/api/backtest/history?limit=${limit}`);
  return data.runs;
}

export async function getBacktestResult(runId: number): Promise<BacktestRun> {
  return get<BacktestRun>(`/api/backtest/${runId}`);
}

export async function deleteBacktest(runId: number): Promise<void> {
  await del(`/api/backtest/${runId}`);
}

