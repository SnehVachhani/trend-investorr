/* ═══════════════════════════════════════════════════════════
   Trend-Investorr — TypeScript Types
   Mirrors the Python dataclasses / DB schema
   ═══════════════════════════════════════════════════════════ */

export interface Stock {
  id: number;
  symbol: string;
  token: string;
  name: string;
  company_name?: string | null;
  sector?: string | null;
  exchange: string;
  series: string;
  sector_id: number | null;
  industry: string;
  market_cap_cr: number;
  is_active: boolean;
  sector_name?: string;
  sector_display?: string;
  latest_price?: number | null;
  last_price?: number | null;
  price_date?: string | null;
  ath_price?: number | null;
}

/** Minimal stock data accepted by the shared stock-details modal. */
export interface StockModalData {
  symbol: string;
  stock_id?: number;
  exchange?: string;
  name?: string | null;
  company_name?: string | null;
  stock_name?: string | null;
  sector?: string | null;
  industry?: string | null;
  as_of_date?: string | null;
  close?: number | null;
  market_cap_cr?: number | null;
  return_20d?: number | null;
  market_relative_60d?: number | null;
  enhanced_score?: number | null;
  breakout_20?: boolean;
  breakout_55?: boolean;
  at_all_time_high?: boolean;
  trend_aligned?: boolean;
}

export interface StockDetail extends Stock {
  indicators: TechnicalIndicators | null;
  latest_candle: {
    open: number;
    high: number;
    low: number;
    close: number;
    volume: number;
    timestamp: string;
  } | null;
  prev_close: number | null;
}

export interface Candle {
  time: number;   // unix timestamp
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface TechnicalIndicators {
  date: string;
  rsi_14: number | null;
  ema_20: number | null;
  ema_50: number | null;
  sma_20: number | null;
  sma_50: number | null;
  sma_200: number | null;
  atr_14: number | null;
  macd_line: number | null;
  macd_signal: number | null;
  macd_histogram: number | null;
  adx_14: number | null;
  bollinger_upper: number | null;
  bollinger_lower: number | null;
  bollinger_width: number | null;
  supertrend: number | null;
  supertrend_direction: number | null;
  obv: number | null;
  vwap_daily: number | null;
  delivery_pct: number | null;
  pivot_point: number | null;
  pivot_r1: number | null;
  pivot_r2: number | null;
  pivot_s1: number | null;
  pivot_s2: number | null;
  support_level: number | null;
  resistance_level: number | null;
  high_52w: number | null;
  low_52w: number | null;
}

export interface Signal {
  id: number;
  symbol: string;
  name: string;
  company_name?: string;
  sector?: string | null;
  signal_type: string;
  strength: string;
  score: number;
  price: number;
  status: string;
  analyzer_name: string;
  analyzer_confidence: number;
  volume_ratio: number;
  created_at: string;
  metadata: Record<string, unknown>;
}

export interface ScannerSignal {
  id: number;
  symbol: string;
  name: string;
  company_name?: string;
  sector?: string | null;
  scanner_name: string;
  pattern: string;
  direction: string;
  confidence: number;
  price: number;
  strength: string;
  details: Record<string, unknown>;
  status: string;
  scan_date: string;
  target_date: string;
}

export interface Order {
  id: number;
  symbol: string;
  name: string;
  company_name?: string;
  sector?: string | null;
  order_type: string;
  product_type: string;
  transaction_type: string;
  quantity: number;
  price: number;
  trigger_price: number;
  status: string;
  is_paper_trade: boolean;
  filled_qty: number;
  avg_fill_price: number;
  slippage: number;
  created_at: string;
}

export interface Position {
  id: number;
  symbol: string;
  name: string;
  company_name?: string;
  sector?: string | null;
  token: string;
  quantity: number;
  avg_buy_price: number;
  current_price: number;
  unrealized_pnl: number;
  is_paper: boolean;
  product_type: string;
  opened_at: string;
}

export interface DailyPnL {
  date: string;
  realized_pnl: number;
  unrealized_pnl: number;
  total_trades: number;
  winning_trades: number;
  losing_trades: number;
  max_drawdown: number;
  portfolio_value: number;
  cash_balance: number;
  is_paper: boolean;
}

export interface PortfolioSummary {
  total_capital: number;
  total_pnl: number;
  today: {
    realized: number;
    unrealized: number;
    trades: number;
    wins: number;
    losses: number;
    portfolio_value: number;
  } | null;
  positions: {
    count: number;
    total_value: number;
    unrealized_pnl: number;
  };
}

export interface Watchlist {
  id: number;
  name: string;
  description: string;
  list_type: string;
  is_active: boolean;
  max_stocks: number;
  stock_count: number;
}

export interface SystemStatus {
  status: string;
  database: string;
  stocks_active: number;
  coverage: Array<{
    interval: string;
    stocks: number;
    candles: number;
  }>;
}

export interface AppConfig {
  key: string;
  value: string;
  data_type: string;
  category: string;
  description: string;
}

export type CandleInterval = '1m' | '5m' | '15m' | '1h' | '1d' | '1w';

// ── Backtest Types ──────────────────────────────────────────

export interface Algorithm {
  name: string;
  display_name: string;
  description: string;
  strategy_type: string;
  candle_intervals: string;
  max_positions: number;
  stop_loss_pct: number;
  is_active: boolean;
}

export interface BacktestRun {
  id: number;
  algorithm_name: string;
  stock_symbol: string;
  stock_name: string;
  company_name?: string;
  sector?: string | null;
  start_date: string;
  end_date: string;
  initial_capital: number;
  status: string;
  total_return_pct: number | null;
  cagr_pct: number | null;
  max_drawdown_pct: number | null;
  sharpe_ratio: number | null;
  win_rate_pct: number | null;
  total_trades: number | null;
  winning_trades: number;
  losing_trades: number;
  avg_win_pct: number | null;
  avg_loss_pct: number | null;
  profit_factor: number | null;
  avg_hold_days: number | null;
  final_capital: number | null;
  total_charges?: number | null;
  equity_curve: EquityPoint[];
  config: Record<string, unknown>;
  error_message: string;
  created_at: string;
  trades?: BacktestTrade[];
}

export type BacktestSection = 'algorithms' | 'filters' | 'watchlist';

export interface BacktestHistoryItem {
  id: number;
  backtest_type: 'algorithm' | 'filter';
  display_name: string;
  stock_symbol: string;
  stock_name: string;
  company_name?: string;
  sector?: string | null;
  start_date: string;
  end_date: string;
  initial_capital: number;
  status: string;
  total_return_pct: number | null;
  max_drawdown_pct: number | null;
  sharpe_ratio: number | null;
  win_rate_pct: number | null;
  total_trades: number | null;
  created_at: string;
}

export interface BacktestTrade {
  id: number;
  run_id: number;
  symbol: string;
  entry_date: string;
  exit_date: string | null;
  entry_price: number;
  exit_price: number | null;
  quantity: number;
  pnl: number;
  pnl_pct: number;
  exit_reason: string;
  hold_days: number;
  metadata: Record<string, unknown>;
}

export interface EquityPoint {
  date: string;
  value: number;
}

export interface BacktestParams {
  algorithm: string;
  symbol: string;
  start_date?: string;
  end_date?: string;
  initial_capital?: number;
}
