export type FilterMode = 'screen' | 'signal' | 'backtest';
export type Timeframe = '1m' | '5m' | '15m' | '1h' | '1d' | '1w';
export type GroupOperator = 'ALL' | 'ANY' | 'AT_LEAST_N' | 'WEIGHTED_SCORE' | 'NOT';

export interface ParameterSpec {
  name: string;
  label: string;
  type: 'integer' | 'number' | 'boolean' | 'string' | 'enum';
  required: boolean;
  default: unknown;
  minimum?: number | null;
  maximum?: number | null;
  enum: string[];
}

export interface FilterDefinition {
  filter_id: string;
  name: string;
  description: string;
  category: string;
  tier: number;
  supported_modes: FilterMode[];
  supported_timeframes: Timeframe[];
  parameter_schema: ParameterSpec[];
  evaluator_key: string;
  history_rule: string;
  default_timeframe: Timeframe;
  requires_live: boolean;
  availability: 'available' | 'planned';
}

export interface FilterCondition {
  type: 'condition';
  filter_id: string;
  timeframe: Timeframe;
  params: Record<string, unknown>;
  weight: number;
  required: boolean;
}

export interface FilterGroup {
  type: 'group';
  operator: GroupOperator;
  children: FilterNode[];
  min_count?: number;
  threshold?: number;
  weight?: number;
}

export type FilterNode = FilterCondition | FilterGroup;

export interface FilterExpression {
  schema_version: number;
  operator: GroupOperator;
  children: FilterNode[];
  veto: FilterNode[];
  minimum_score: number;
  maximum_insufficient_data: number;
}

export interface FilterRequirements {
  minimum_candles: number;
  warmup: number;
  required_fields: string[];
  requires_live: boolean;
}

export interface FilterProfile {
  id: number;
  name: string;
  description: string;
  mode: FilterMode;
  is_active: boolean;
  latest_version?: FilterProfileVersion;
  versions?: FilterProfileVersion[];
}

export interface FilterProfileVersion {
  id: number;
  profile_id: number;
  version_number: number;
  status: 'draft' | 'published' | 'archived';
  expression_json: FilterExpression;
  requirements_json: Record<string, FilterRequirements>;
  risk_config_json: Record<string, unknown>;
  timeframe_config_json: Record<string, unknown>;
}

export interface FilterEvaluationResult {
  status: 'pass' | 'fail' | 'insufficient_data' | 'unavailable' | 'error';
  passed: boolean;
  score: number;
  symbol: string;
  company_name?: string;
  sector?: string | null;
  filter_id?: string | null;
  operator?: GroupOperator | null;
  actual_value?: unknown;
  threshold?: unknown;
  reason: string;
  children?: FilterEvaluationResult[];
  veto_results?: FilterEvaluationResult[];
}

export interface FilterPreviewResponse {
  profile_id: number;
  version_id: number;
  as_of: string;
  requirements: Record<string, FilterRequirements>;
  counts: Record<string, number>;
  results: FilterEvaluationResult[];
}

export interface FilterBacktestResult {
  symbol: string;
  company_name?: string;
  sector?: string | null;
  execution_timeframe?: Timeframe;
  start_date: string;
  end_date: string;
  initial_capital: number;
  final_capital: number;
  total_charges: number;
  charge_config?: Record<string, unknown>;
  total_return_pct: number;
  max_drawdown_pct: number;
  sharpe_ratio: number;
  total_trades: number;
  winning_trades: number;
  losing_trades: number;
  win_rate_pct: number;
  signal_counts: Record<string, number>;
  equity_curve: Array<{ timestamp: string; equity: number }>;
  trades: Array<Record<string, unknown>>;
}

export interface FilterBacktestRun {
  id: number;
  profile_id: number | null;
  version_id: number | null;
  preset_id: string | null;
  preset_name: string | null;
  symbol: string;
  company_name?: string;
  sector?: string | null;
  start_date: string;
  end_date: string;
  initial_capital: number;
  status: string;
  config: Record<string, unknown>;
  result: FilterBacktestResult;
  error_message: string;
  created_at: string;
}

export interface FilterBacktestPreset {
  preset_id: string;
  name: string;
  description: string;
  category: string;
  timeframe: Timeframe;
  holding_period: string;
  max_hold_bars: number;
  default_stop_loss_pct: number;
  default_take_profit_pct: number;
  expression: FilterExpression;
}

export interface WatchlistAnalysisCombination {
  preset_id: string;
  preset_name: string;
  run_id: number | null;
  status: 'completed' | 'failed' | string;
  error?: string;
  execution_timeframe?: string;
  start_date?: string;
  end_date?: string;
  total_return_pct?: number;
  final_capital?: number;
  total_charges?: number;
  max_drawdown_pct?: number;
  sharpe_ratio?: number;
  win_rate_pct?: number;
  total_trades?: number;
}

export interface WatchlistMarketBehavior {
  id: string;
  label: string;
  description: string;
}

export interface WatchlistMarketProfile {
  observations?: number;
  first_timestamp?: string | null;
  last_timestamp?: string | null;
  first_close?: number;
  last_close?: number;
  total_return_pct?: number | null;
  annualized_volatility_pct?: number | null;
  max_drawdown_pct?: number | null;
  trend_efficiency_pct?: number | null;
  range_pct?: number | null;
  behavior?: WatchlistMarketBehavior;
}

export interface WatchlistAnalysisStockResult {
  symbol: string;
  stock_name: string;
  company_name?: string;
  sector?: string | null;
  status: 'completed' | 'partial' | 'failed' | string;
  error?: string;
  market_profile: WatchlistMarketProfile;
  best_combination: WatchlistAnalysisCombination | null;
  combinations: WatchlistAnalysisCombination[];
}

export interface WatchlistFilterLeaderboardRow {
  preset_id: string;
  preset_name: string;
  stock_count: number;
  average_return_pct: number;
  median_return_pct: number;
  average_max_drawdown_pct: number;
  average_sharpe_ratio: number;
  average_trade_count: number;
  positive_stock_count: number;
  portfolio_initial_capital?: number;
  portfolio_return_pct?: number;
  portfolio_final_capital?: number;
}

export interface WatchlistRegimeRecommendation {
  regime_id: string;
  regime_label: string;
  description: string;
  stock_count: number;
  symbols: string[];
  recommended_filter: WatchlistFilterLeaderboardRow | null;
  leaderboard: WatchlistFilterLeaderboardRow[];
}

export interface WatchlistFilterAnalysisJob {
  job_id: string;
  status: 'queued' | 'running' | 'completed' | 'partial' | 'failed' | 'cancelled';
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  source: { type: 'watchlist' | 'manual'; id?: number; name: string };
  symbols: string[];
  portfolio_initial_capital: number;
  capital_per_stock: number;
  preset_count: number;
  total_stocks: number;
  completed_stocks: number;
  failed_stocks: number;
  total_backtests: number;
  completed_backtests: number;
  successful_backtests: number;
  stock_results: WatchlistAnalysisStockResult[];
  overall_leaderboard: WatchlistFilterLeaderboardRow[];
  regime_recommendations: WatchlistRegimeRecommendation[];
  average_watchlist_return_pct?: number | null;
  watchlist_return_stock_count?: number;
  error: string | null;
}

export interface TrendlinePoint { time: number; price: number; }
export interface TrendlineDefinition {
  id: number;
  symbol: string;
  interval: Timeframe;
  points_json: TrendlinePoint[];
  trendline_type: string;
  source: string;
  name?: string | null;
  active: boolean;
}

export const emptyExpression = (): FilterExpression => ({
  schema_version: 1,
  operator: 'ALL',
  children: [],
  veto: [],
  minimum_score: 0,
  maximum_insufficient_data: 0,
});

export const newCondition = (definition: FilterDefinition): FilterCondition => ({
  type: 'condition',
  filter_id: definition.filter_id,
  timeframe: definition.default_timeframe,
  params: Object.fromEntries(
    definition.parameter_schema
      .filter((parameter) => parameter.default !== null && parameter.default !== undefined)
      .map((parameter) => [parameter.name, parameter.default]),
  ),
  weight: 1,
  required: true,
});

export const isCondition = (node: FilterNode): node is FilterCondition => node.type === 'condition';
