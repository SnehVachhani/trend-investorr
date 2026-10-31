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
  BacktestHistoryItem,
  BacktestSection,
  BacktestParams,
} from './types';

const BASE_URL = process.env.NEXT_PUBLIC_API_URL || '';
const ENHANCED_API_URL = process.env.NEXT_PUBLIC_ENHANCED_API_URL || '';

function directApiBaseUrl(): string {
  // Use the same-origin Next.js rewrite by default. This keeps local API
  // calls working from localhost, 127.0.0.1, and embedded browser tabs
  // without relying on browser CORS or loopback address resolution.
  return BASE_URL;
}

function enhancedScannerBaseUrl(): string {
  if (ENHANCED_API_URL) return ENHANCED_API_URL;
  return directApiBaseUrl();
}

async function get<T>(path: string, baseUrl = BASE_URL): Promise<T> {
  const res = await fetch(`${baseUrl}${path}`, {
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

export async function getStock(symbol: string, stockId?: number): Promise<StockDetail> {
  const stockIdQuery = stockId == null ? '' : `?stock_id=${stockId}`;
  return get<StockDetail>(`/api/stocks/${encodeURIComponent(symbol)}${stockIdQuery}`);
}

export interface FundamentalRecord {
  [key: string]: unknown;
}

export interface FundamentalRatio extends FundamentalRecord {
  ratio_name?: string;
  company_value?: number | null;
  sector_value?: number | null;
  company_raw?: string | null;
  sector_raw?: string | null;
}

export interface FundamentalLine extends FundamentalRecord {
  statement_kind?: string;
  statement_type?: string;
  frequency?: string;
  period_label?: string;
  period_end?: string | null;
  metric_name?: string;
  value?: number | null;
  change_pct?: number | null;
  unit?: string;
}

export interface FundamentalCompetitor extends FundamentalRecord {
  competitor_isin?: string | null;
  competitor_instrument_key?: string;
  company_name?: string;
  company_profile?: string;
  symbol?: string;
  sector?: string;
  market_cap_cr?: number | null;
  sector_market_cap_inr?: number | null;
  pe_ratio?: number | null;
  pe_snapshot_date?: string | null;
}

export interface FundamentalData {
  profile: FundamentalRecord | null;
  ratios: FundamentalRatio[];
  ratio_history: FundamentalRatio[];
  lines: FundamentalLine[];
  shareholdings: FundamentalRecord[];
  corporate_actions: FundamentalRecord[];
  competitors: FundamentalCompetitor[];
  sync_state: FundamentalRecord[];
  raw_latest: FundamentalRecord[];
}

export interface StockFundamentalsResponse {
  symbol: string;
  name: string;
  company_name?: string;
  sector?: string | null;
  isin: string | null;
  source: string;
  fundamentals: FundamentalData;
}

export type FundamentalScoreInput = Pick<FundamentalData, 'ratios' | 'lines' | 'shareholdings'> & {
  market_cap_cr?: number | null;
};

export type SectorPeriod = '1w' | '1m' | '3m' | '6m' | '1y';

export interface SectorPeriodSummary {
  return_pct: number | null;
  sample_count: number;
  positive_count: number;
}

export interface SectorSummary {
  key: string;
  name: string;
  stock_count: number;
  priced_stock_count: number;
  periods: Record<SectorPeriod, SectorPeriodSummary>;
  advancing_1m: number;
  declining_1m: number;
  advance_pct_1m: number | null;
  average_rsi_14: number | null;
  above_sma200_pct: number | null;
  outlook: string;
  outlook_detail: string;
}

export interface SectorAnalysisResponse {
  as_of_date: string | null;
  universe_stocks: number;
  priced_stocks: number;
  return_method: string;
  period_sessions: Record<SectorPeriod, number>;
  sectors: SectorSummary[];
}

export interface SectorStockMember {
  stock_id: number;
  symbol: string;
  name: string;
  market_cap_cr: number | null;
  latest_close: number;
  latest_at: string | null;
  returns: Record<SectorPeriod, number | null>;
  technical: {
    date: string | null;
    rsi_14: number | null;
    sma_20: number | null;
    sma_50: number | null;
    sma_200: number | null;
    macd_line: number | null;
    macd_signal: number | null;
    adx_14: number | null;
  };
}

export interface SectorStocksResponse {
  sector_key: string;
  sector_name: string | null;
  total_stocks: number;
  priced_stocks: number;
  returned_stocks: number;
  unpriced_stocks: number;
  stocks: SectorStockMember[];
}

export async function getFundamentalScoreInputs(stockIds: number[]): Promise<Record<string, FundamentalScoreInput>> {
  if (stockIds.length === 0) return {};
  const response = await post<{ score_inputs: Record<string, FundamentalScoreInput> }>(
    '/api/stocks/fundamentals/score-inputs',
    { stock_ids: [...new Set(stockIds)] },
  );
  return response.score_inputs;
}

export async function getSectorAnalysis(): Promise<SectorAnalysisResponse> {
  return get<SectorAnalysisResponse>('/api/sectors/analysis');
}

export async function getSectorStocks(sectorKey: string): Promise<SectorStocksResponse> {
  return get<SectorStocksResponse>(`/api/sectors/${encodeURIComponent(sectorKey)}/stocks`);
}

export async function getStockFundamentals(symbol: string, stockId?: number): Promise<StockFundamentalsResponse> {
  const stockIdQuery = stockId == null ? '' : `&stock_id=${stockId}`;
  return get<StockFundamentalsResponse>(`/api/stocks/${encodeURIComponent(symbol)}/fundamentals?line_limit=10000${stockIdQuery}`);
}

export async function getStockDisplayMetadata(
  stocks: Array<{ symbol: string; stock_id?: number }>,
): Promise<{ companyNames: Record<string, string>; sectorsByStockId: Record<string, string>; stockIdsBySymbol: Record<string, number> }> {
  if (stocks.length === 0) return { companyNames: {}, sectorsByStockId: {}, stockIdsBySymbol: {} };
  const data = await post<{
    company_names: Record<string, string>;
    company_names_by_stock_id?: Record<string, string>;
    sectors_by_stock_id?: Record<string, string>;
    stock_ids_by_symbol?: Record<string, number>;
  }>(
    '/api/stocks/company-names',
    {
      symbols: [...new Set(stocks.map((stock) => stock.symbol))],
      stock_ids: [...new Set(stocks.flatMap((stock) => stock.stock_id == null ? [] : [stock.stock_id]))],
    },
  );
  const names = { ...data.company_names };
  const sectorsByStockId = { ...data.sectors_by_stock_id };
  const stockIdsBySymbol = { ...data.stock_ids_by_symbol };
  for (const stock of stocks) {
    const stockId = stock.stock_id ?? stockIdsBySymbol[stock.symbol.trim().toUpperCase()];
    if (!names[stock.symbol] && stockId != null) {
      const companyName = data.company_names_by_stock_id?.[String(stockId)];
      if (companyName) names[stock.symbol] = companyName;
    }
  }
  return { companyNames: names, sectorsByStockId, stockIdsBySymbol };
}

// ── Candles ────────────────────────────────────────────────

export async function getCandles(
  symbol: string,
  interval: CandleInterval = '1d',
  days = 90,
  stockId?: number,
): Promise<Candle[]> {
  const stockIdQuery = stockId == null ? '' : `&stock_id=${stockId}`;
  const data = await get<{ candles: Candle[] }>(
    `/api/stocks/${encodeURIComponent(symbol)}/candles?interval=${interval}&days=${days}${stockIdQuery}`,
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

export async function renameWatchlist(id: number, name: string): Promise<{ id: number; name: string; updated: boolean }> {
  return put(`/api/watchlists/${id}`, { name });
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

export interface MomentumScanRequest {
  as_of_date?: string;
  max_results?: number;
  min_price?: number;
  min_avg_turnover_cr?: number;
  min_score?: number;
  min_volume_ratio?: number;
  min_return_20d?: number;
  min_return_60d?: number;
  min_delivery_pct?: number;
  trend_mode?: 'aligned' | 'above_sma50' | 'any';
}

export interface MomentumScanResult {
  rank: number;
  stock_id: number;
  symbol: string;
  exchange: string;
  name: string;
  company_name?: string;
  sector?: string | null;
  industry: string;
  market_cap_cr: number;
  as_of_date: string;
  close: number;
  return_20d: number | null;
  return_60d: number | null;
  return_120d: number | null;
  return_252d: number | null;
  volume_ratio: number;
  avg_turnover_20d_cr: number;
  latest_delivery_pct: number;
  avg_delivery_pct_20d: number;
  sma_20: number;
  sma_50: number;
  sma_200: number;
  high_52w: number;
  distance_from_52w_high_pct: number | null;
  avg_range_pct_20d: number;
  observations: number;
  momentum_score: number;
  price_momentum_score: number;
  trend_score: number;
  volume_score: number;
  breakout_score: number;
  trend_aligned: boolean;
  category: string;
}

export type MasterCandidateMetrics = Partial<Pick<EnhancedMomentumScanResult,
  | 'return_20d' | 'return_60d' | 'return_120d' | 'return_252d'
  | 'market_relative_20d' | 'market_relative_60d' | 'market_relative_120d'
  | 'volume_ratio' | 'volume_ratio_20d' | 'volume_ratio_60d' | 'volume_ratio_120d' | 'volume_ratio_252d'
  | 'atr_pct_20d' | 'atr_pct_60d' | 'atr_pct_120d' | 'atr_pct_252d'
  | 'realized_volatility_pct' | 'realized_volatility_20d' | 'realized_volatility_60d' | 'realized_volatility_120d' | 'realized_volatility_252d'
  | 'breakout_20' | 'breakout_55' | 'at_all_time_high' | 'enhanced_score' | 'base_momentum_score'
  | 'market_relative_strength_score' | 'risk_adjusted_momentum_score' | 'risk_adjusted_20d' | 'risk_adjusted_60d'
  | 'risk_adjusted_120d' | 'risk_adjusted_252d' | 'volume_participation_score' | 'volume_ratio_score'
  | 'turnover_score' | 'breakout_quality_score' | 'trend_aligned' | 'observations' | 'category'
>>;

export interface MomentumScanResponse {
  as_of_date: string;
  universe: string;
  universe_stocks?: number;
  scanned_stocks: number;
  matching_stocks: number;
  returned_stocks: number;
  filters: MomentumScanRequest;
  formula: Record<string, string>;
  results: MomentumScanResult[];
}

export interface EnhancedMomentumScanRequest {
  as_of_date?: string;
  top_n?: number | null;
  min_price?: number;
  min_avg_turnover_cr?: number;
  min_score?: number;
  min_volume_ratio?: number;
  min_return_20d?: number;
  min_return_60d?: number;
  min_history?: number;
  trend_mode?: 'aligned' | 'above_sma50' | 'any';
  market_mode?: 'all' | 'bullish_or_neutral' | 'bullish_only';
  require_breakout?: boolean;
}

export interface EnhancedMomentumScanResult {
  rank: number;
  stock_id: number;
  symbol: string;
  exchange: string;
  name: string;
  company_name?: string;
  sector?: string | null;
  industry: string;
  market_cap_cr: number;
  as_of_date: string;
  close: number;
  return_10d: number | null;
  return_20d: number | null;
  return_60d: number | null;
  return_120d: number | null;
  return_252d: number | null;
  market_relative_20d: number | null;
  market_relative_60d: number | null;
  market_relative_120d: number | null;
  volume_ratio: number;
  volume_ratio_20d: number;
  volume_ratio_60d: number;
  volume_ratio_120d: number;
  volume_ratio_252d: number;
  avg_turnover_20d_cr: number;
  avg_turnover_60d_cr: number;
  avg_turnover_120d_cr: number;
  avg_turnover_252d_cr: number;
  avg_delivery_pct_20d: number;
  avg_delivery_pct_60d: number;
  avg_delivery_pct_120d: number;
  avg_delivery_pct_252d: number;
  atr_pct_20d: number;
  atr_pct_60d: number;
  atr_pct_120d: number;
  atr_pct_252d: number;
  realized_volatility_pct: number;
  realized_volatility_20d: number;
  realized_volatility_60d: number;
  realized_volatility_120d: number;
  realized_volatility_252d: number;
  positive_ratio_20d: number;
  positive_ratio_60d: number;
  positive_ratio_120d: number;
  positive_ratio_252d: number;
  breakout_20: boolean;
  breakout_55: boolean;
  at_all_time_high: boolean;
  breakout_margin_atr: number | null;
  extension_atr: number | null;
  observations: number;
  enhanced_score: number;
  base_momentum_score: number | null;
  market_relative_strength_score: number | null;
  risk_adjusted_momentum_score: number | null;
  risk_adjusted_20d: number | null;
  risk_adjusted_60d: number | null;
  risk_adjusted_120d: number | null;
  risk_adjusted_252d: number | null;
  volume_participation_score: number | null;
  volume_ratio_score: number | null;
  turnover_score: number | null;
  breakout_quality_score: number | null;
  market_regime_score: number | null;
  sector_score: number | null;
  sector_context_available: boolean;
  trend_aligned: boolean;
  category: string;
}

/** Defensive client-side guard for cached scanner responses from older runs. */
export function isEquityScannerResult(result: Pick<EnhancedMomentumScanResult, 'symbol' | 'name'>): boolean {
  const normalise = (value: string) => value.trim().toUpperCase().replace(/-EQ$/, '');
  const symbol = normalise(result.symbol || '');
  const name = normalise(result.name || '');
  const indexes = new Set([
    'NIFTY', 'NIFTY50', 'NIFTY 50', 'NIFTYNXT50', 'NIFTY NEXT 50',
    'BANKNIFTY', 'BANK NIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'MIDCAP SELECT',
    'NIFTY MIDCAP SELECT', 'SENSEX', 'BSESN',
  ]);
  if (indexes.has(symbol) || indexes.has(name)) return false;
  const fundMarker = /(INAV|IETF|BEES|ETF$|(GOLD|SILVER|NIFTY|SENSEX|VALUE)$)/;
  return !fundMarker.test(symbol) && !fundMarker.test(name);
}

export interface EnhancedMomentumScanResponse {
  scanner: string;
  as_of_date: string;
  universe: string;
  universe_stocks: number;
  scanned_stocks: number;
  history_eligible_stocks: number;
  matching_stocks: number;
  returned_stocks: number;
  filters: EnhancedMomentumScanRequest;
  market_context: {
    available: boolean;
    regime: string;
    regime_score: number | null;
    primary_benchmark: string | null;
    nifty: { available: boolean; observations: number; trend: string; return_60d: number | null };
    sensex: { available: boolean; observations: number; trend: string; return_60d: number | null };
    sector: { available: boolean; weight: number; status: string };
    warnings: string[];
  };
  formula: Record<string, string>;
  results: EnhancedMomentumScanResult[];
}

export interface ScreenerQueryCandidateRow {
  symbol: string;
  name: string;
  industry: string | null;
  close: number | null;
  marketCapCr: number | null;
  roePct: number | null;
  profitGrowth1yPct: number | null;
  rank: number;
}

export interface ScreenerQueryCandidateRefreshJob {
  job_id: string;
  query: string;
  status: 'queued' | 'running' | 'waiting_for_login' | 'completed' | 'failed';
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  updated_at: string | null;
  progress: {
    phase: string;
    pages_completed: number;
    candidates_found: number;
    detail: string;
  };
  result: {
    query: string;
    page_count: number;
    result_count: number;
    rows: ScreenerQueryCandidateRow[];
    updated_at: string;
  } | null;
  error: string | null;
}

export function startScreenerQueryCandidateRefresh(query: string): Promise<ScreenerQueryCandidateRefreshJob> {
  return post('/api/master/screener-query-candidates', { query }, directApiBaseUrl());
}

export function getScreenerQueryCandidateRefresh(jobId: string): Promise<ScreenerQueryCandidateRefreshJob> {
  return get<ScreenerQueryCandidateRefreshJob>(
    `/api/master/screener-query-candidates/${encodeURIComponent(jobId)}`,
    directApiBaseUrl(),
  );
}

export function reportScreenerQueryCandidateProgress(
  jobId: string,
  progress: {
    status: 'running' | 'waiting_for_login';
    phase: string;
    pages_completed: number;
    candidates_found: number;
    detail: string;
  },
): Promise<ScreenerQueryCandidateRefreshJob> {
  return post(
    `/api/master/screener-query-candidates/${encodeURIComponent(jobId)}/progress`,
    progress,
    directApiBaseUrl(),
  );
}

export function completeScreenerQueryCandidateRefresh(
  jobId: string,
  result: { page_count: number; rows: ScreenerQueryCandidateRow[] },
): Promise<ScreenerQueryCandidateRefreshJob> {
  return post(
    `/api/master/screener-query-candidates/${encodeURIComponent(jobId)}/complete`,
    result,
    directApiBaseUrl(),
  );
}

export function failScreenerQueryCandidateRefresh(jobId: string, error: string): Promise<ScreenerQueryCandidateRefreshJob> {
  return post(
    `/api/master/screener-query-candidates/${encodeURIComponent(jobId)}/fail`,
    { error },
    directApiBaseUrl(),
  );
}

export interface EnhancedMomentumBacktestResult extends EnhancedMomentumScanResult {
  signal_date?: string;
  entry_date: string | null;
  entry_price: number | null;
  target_price?: number | null;
  stop_price?: number | null;
  target_hit?: boolean;
  stop_hit?: boolean;
  exit_date?: string | null;
  exit_price?: number | null;
  exit_reason?: string;
  horizon_end_date?: string | null;
  horizon_close?: number | null;
  horizon_complete?: boolean;
  horizon_return_pct?: number | null;
  gross_trade_return_pct?: number | null;
  net_trade_return_pct?: number | null;
  quantity?: number;
  buy_charges?: number;
  sell_charges?: number;
  net_pnl?: number | null;
  outcome: string;
}

export interface EnhancedMomentumBacktestTrade {
  signal_date?: string | null;
  scanner_signal_date?: string | null;
  entry_date: string;
  entry_price: number;
  target_price?: number | null;
  stop_price?: number | null;
  target_hit?: boolean;
  stop_hit?: boolean;
  exit_date: string;
  exit_price: number;
  exit_reason: string;
  quantity: number;
  gross_pnl: number;
  total_charges?: number;
  buy_charges?: number;
  sell_charges?: number;
  net_pnl: number;
  net_trade_return_pct: number | null;
  hold_bars?: number;
}

export interface EnhancedMomentumBacktestStock {
  stock_id: number;
  symbol: string;
  name: string;
  company_name?: string;
  industry?: string;
  scanner_signal_count: number;
  starting_capital: number;
  final_capital: number | null;
  net_pnl: number | null;
  return_pct: number | null;
  total_trades: number;
  winning_trades: number;
  win_rate_pct: number | null;
  target_hits: number;
  stop_hits: number;
  status?: string;
  trades: EnhancedMomentumBacktestTrade[];
}

export interface EnhancedMomentumBacktestStrategy {
  strategy_id: string;
  strategy_name: string;
  strategy_type: 'scanner' | 'watchlist_analysis_preset' | 'fundamental_setup' | 'buy_and_hold';
  description: string;
  timeframe: string;
  risk_rules: Record<string, number | string>;
  summary: {
    stock_count: number;
    evaluated_stocks?: number;
    stocks_traded?: number;
    capital_slots?: number;
    maximum_concurrent_positions?: number;
    blocked_trade_signals?: number;
    average_stock_return_pct: number | null;
    median_stock_return_pct: number | null;
    positive_stocks: number;
    positive_stock_rate_pct: number | null;
    starting_capital: number;
    final_capital: number;
    net_pnl: number;
    total_return_pct: number | null;
    total_trades: number;
    target_eligible_trades: number;
    target_hits: number;
    target_hit_rate_pct: number | null;
    stop_hits: number;
    scanner_eligible_stocks?: number;
    stocks_skipped?: number;
  };
  stocks: EnhancedMomentumBacktestStock[];
}

export interface EnhancedMomentumBacktestPreset {
  preset_id: string;
  name: string;
  description: string;
  timeframe: string;
  holding_period: string;
  default_stop_loss_pct: number;
  default_take_profit_pct: number;
  max_hold_bars: number;
}

export interface EnhancedMomentumBacktestSetupPreset {
  id: string;
  candle_interval: '5m';
  name: string;
  description: string;
  entry_rule: string;
  exit_rule: string;
}

export interface EnhancedMomentumBacktestSummary {
  matching_stocks: number;
  returned_stocks: number;
  entry_candidates: number;
  executed_trades: number;
  completed_forward_windows: number;
  partial_forward_windows: number;
  average_horizon_return_pct: number | null;
  median_horizon_return_pct: number | null;
  positive_horizon_trades: number;
  horizon_win_rate_pct: number | null;
  target_eligible_trades: number;
  target_hits: number;
  target_hit_rate_pct: number | null;
  stop_hits: number;
  average_net_trade_return_pct: number | null;
  aggregate_net_pnl: number | null;
  aggregate_invested_capital: number;
  aggregate_net_return_pct: number | null;
  average_stock_return_pct?: number | null;
  total_final_capital?: number;
  scanner_sessions?: number;
  scanner_signal_count?: number;
  unique_scanner_stocks?: number;
  scanner_snapshot_returned_stocks?: number;
  stale_snapshot_results_excluded?: number;
  refresh_frequency?: string;
  capital_slots?: number;
  maximum_concurrent_positions?: number;
  blocked_trade_signals?: number;
  strategy_count?: number;
}

export interface EnhancedMomentumBacktestRun {
  id: number;
  signal_cutoff_date: string;
  end_date: string;
  lookback_months: number;
  filters: EnhancedMomentumScanRequest & { preset_ids?: string[]; fundamental_setup_ids?: string[] };
  preset_ids?: string[];
  fundamental_setup_ids?: string[];
  config?: Record<string, unknown>;
  summary: EnhancedMomentumBacktestSummary;
  scan?: Record<string, unknown>;
  results?: EnhancedMomentumBacktestResult[];
  strategies?: EnhancedMomentumBacktestStrategy[];
  created_at: string;
}

export interface EnhancedMomentumBacktestJob {
  job_id: string;
  status: 'queued' | 'running' | 'completed' | 'failed';
  progress: {
    percent: number;
    phase: string;
    completed: number;
    total: number;
    unit: string;
    detail: string;
  };
  run_id: number | null;
  error?: string | null;
}

export interface EnhancedMomentumBacktestRequest {
  start_date?: string;
  end_date?: string;
  lookback_months?: number;
  mode?: 'filter_replay' | 'buy_and_hold';
  top_n?: 30 | 50 | 100;
  refresh_frequency?: '15_days' | 'monthly' | 'quarterly' | 'semiannual';
  target_atr?: number;
  stop_atr?: number;
  capital_per_stock?: number;
  preset_ids?: string[];
  fundamental_setup_ids?: string[];
  include_top_n_buy_hold?: boolean;
  filters: EnhancedMomentumScanRequest;
}

function currentEnhancedMomentumFilters(params: EnhancedMomentumScanRequest): EnhancedMomentumScanRequest {
  // Older browser state stored a hidden 100-row display cap. Keep accepting
  // that state locally, but never send it as a scanner selection rule.
  const cleaned = { ...params } as EnhancedMomentumScanRequest & { max_results?: number };
  delete cleaned.max_results;
  return cleaned;
}

function liveEnhancedMomentumFilters(params: EnhancedMomentumScanRequest): EnhancedMomentumScanRequest {
  const cleaned = currentEnhancedMomentumFilters(params);
  // Top N is a backtest trading-cap setting. The live scanner always returns
  // every stock that passes its screening rules.
  delete cleaned.top_n;
  return cleaned;
}

export async function scanMomentumUniverse(
  params: MomentumScanRequest = {},
): Promise<MomentumScanResponse> {
  // The scan can take longer than Next's local rewrite proxy timeout, which
  // surfaces as a blank 500 even though FastAPI handles the request. Call the
  // local API directly, as the filter utilities do, so the normal scanner is
  // not dependent on that proxy path.
  return post('/api/momentum-scanner/scan', params, directApiBaseUrl());
}

export async function scanEnhancedMomentumUniverse(
  params: EnhancedMomentumScanRequest = {},
): Promise<EnhancedMomentumScanResponse> {
  return post('/api/enhanced-momentum-scanner/scan', liveEnhancedMomentumFilters(params), enhancedScannerBaseUrl());
}

export async function getEnhancedMomentumBacktestPresets(): Promise<EnhancedMomentumBacktestPreset[]> {
  const data = await get<{ presets: EnhancedMomentumBacktestPreset[] }>(
    '/api/filter-backtest-presets',
    enhancedScannerBaseUrl(),
  );
  return data.presets;
}

export async function getEnhancedMomentumBacktestSetupPresets(): Promise<EnhancedMomentumBacktestSetupPreset[]> {
  const data = await get<{ setups: EnhancedMomentumBacktestSetupPreset[] }>(
    '/api/fundamental-setup-presets',
    enhancedScannerBaseUrl(),
  );
  const watchlistBackedIds = new Set(['trend_continuation', 'liquid_swing_momentum']);
  return data.setups.filter((setup) => !watchlistBackedIds.has(setup.id));
}

export async function runEnhancedMomentumBacktest(
  params: EnhancedMomentumBacktestRequest,
): Promise<EnhancedMomentumBacktestJob | { run: EnhancedMomentumBacktestRun }> {
  return post('/api/enhanced-momentum-scanner/backtest', {
    ...params,
    filters: currentEnhancedMomentumFilters(params.filters),
  }, enhancedScannerBaseUrl());
}

export async function getEnhancedMomentumBacktestJob(jobId: string): Promise<EnhancedMomentumBacktestJob> {
  return get<EnhancedMomentumBacktestJob>(
    `/api/enhanced-momentum-scanner/backtest-jobs/${jobId}`,
    enhancedScannerBaseUrl(),
  );
}

export async function getEnhancedMomentumBacktests(limit = 50): Promise<EnhancedMomentumBacktestRun[]> {
  const data = await get<{ runs: EnhancedMomentumBacktestRun[] }>(
    `/api/enhanced-momentum-scanner/backtests?limit=${limit}`,
    enhancedScannerBaseUrl(),
  );
  return data.runs;
}

export async function getEnhancedMomentumBacktest(runId: number): Promise<EnhancedMomentumBacktestRun> {
  const data = await get<{ run: EnhancedMomentumBacktestRun }>(
    `/api/enhanced-momentum-scanner/backtests/${runId}`,
    enhancedScannerBaseUrl(),
  );
  return data.run;
}

export async function deleteEnhancedMomentumBacktest(runId: number): Promise<void> {
  const res = await fetch(`${enhancedScannerBaseUrl()}/api/enhanced-momentum-scanner/backtests/${runId}`, { method: 'DELETE' });
  if (!res.ok) throw new Error(`API error ${res.status}: ${res.statusText}`);
}

export async function getEnhancedAllTimeHighMarkers(
  stockIds: number[],
  asOfDate: string,
): Promise<Record<string, boolean>> {
  if (stockIds.length === 0) return {};
  const response = await post<{ markers: Record<string, boolean> }>(
    '/api/enhanced-momentum-scanner/ath-markers',
    { stock_ids: [...new Set(stockIds)], as_of_date: asOfDate },
    enhancedScannerBaseUrl(),
  );
  return response.markers;
}

export async function getEnhancedSetupCandles(
  stockIds: number[],
  asOfDate: string,
  days = 120,
): Promise<Record<string, Candle[]>> {
  if (stockIds.length === 0) return {};
  const response = await post<{ candles_by_stock: Record<string, Candle[]> }>(
    '/api/enhanced-momentum-scanner/setup-candles',
    { stock_ids: [...new Set(stockIds)], as_of_date: asOfDate, days },
    enhancedScannerBaseUrl(),
  );
  return response.candles_by_stock;
}

export async function getMasterCandidateMetrics(
  stockIds: number[],
  asOfDate: string,
): Promise<Record<string, MasterCandidateMetrics>> {
  if (stockIds.length === 0) return {};
  const response = await post<{ metrics_by_stock_id: Record<string, MasterCandidateMetrics> }>(
    '/api/master/candidate-metrics',
    { stock_ids: [...new Set(stockIds)], as_of_date: asOfDate },
    enhancedScannerBaseUrl(),
  );
  return response.metrics_by_stock_id;
}

export async function addStocksToWatchlist(
  watchlistId: number,
  stockIds: number[],
  source = 'momentum_scanner',
): Promise<{ added_count: number; added_symbols: string[]; already_present_count: number; capacity_skipped_count: number }> {
  return post(`/api/watchlists/${watchlistId}/stocks/bulk`, { stock_ids: stockIds, source });
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
  company_name?: string;
  sector?: string | null;
  close: number | null;
  '1d': number | null;
  '5d': number | null;
  '10d': number | null;
  '20d': number | null;
  '30d': number | null;
  '60d': number | null;
  '90d': number | null;
  '180d': number | null;
  '1y': number | null;
  '3y': number | null;
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

export interface TradingChargesConfig {
  enabled: boolean;
  product_type: string;
  exchange: string;
  brokerage_rate_pct: number;
  brokerage_cap_rupees: number;
  brokerage_min_rupees: number;
  stt_buy_pct: number;
  stt_sell_pct: number;
  transaction_charge_pct: number;
  ipft_charge_pct: number;
  stamp_duty_buy_pct: number;
  sebi_fee_per_crore: number;
  dp_charge_per_sell_rupees: number;
  gst_pct: number;
}

export interface TradingChargesResponse {
  config: TradingChargesConfig;
  formula: Record<string, string>;
}

export async function getTradingCharges(): Promise<TradingChargesResponse> {
  return get<TradingChargesResponse>('/api/trading/charges');
}

export async function updateTradingCharges(config: TradingChargesConfig): Promise<TradingChargesResponse> {
  return put<TradingChargesResponse>('/api/trading/charges', config);
}

// ── Backtest ───────────────────────────────────────────────

async function post<T>(path: string, body: unknown, baseUrl = BASE_URL): Promise<T> {
  const res = await fetch(`${baseUrl}${path}`, {
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

export async function getBacktestHistory(section: BacktestSection = 'algorithms', limit = 50): Promise<BacktestHistoryItem[]> {
  const data = await get<{ runs: BacktestHistoryItem[] }>(`/api/backtest/history?section=${section}&limit=${limit}`);
  return data.runs;
}

export async function getBacktestResult(runId: number): Promise<BacktestRun> {
  return get<BacktestRun>(`/api/backtest/${runId}`);
}

export async function deleteBacktest(runId: number): Promise<void> {
  await del(`/api/backtest/${runId}`);
}
