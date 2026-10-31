export type FundamentalMetric =
  | 'profit_cagr_5y_pct'
  | 'profit_growth_1y_pct'
  | 'quarter_profit_yoy_pct'
  | 'quarter_profit_qoq_pct'
  | 'roe_pct'
  | 'roce_pct'
  | 'market_cap_cr'
  | 'latest_annual_profit_cr'
  | 'latest_quarter_profit_cr';

export type FundamentalOperator = '>' | '>=' | '<' | '<=';

export interface FundamentalRule {
  metric: FundamentalMetric;
  operator: FundamentalOperator;
  value: number;
}

export interface FundamentalSetupPreset {
  id: string;
  candle_interval: '5m';
  name: string;
  description: string;
  entry_rule: string;
  exit_rule: string;
}

export interface FundamentalQuery {
  id: number;
  name: string;
  version: number;
  rules: FundamentalRule[];
  query_text?: string;
  created_at: string;
  updated_at: string;
}

export interface FundamentalQueryPreview {
  as_of: string;
  query_text: string;
  coverage: {
    total: number;
    eligible: number;
    insufficient: number;
    failed: number;
    incomplete_but_ruled_out: number;
    unresolved: number;
    missing_by_metric?: Record<string, number>;
    missing_by_metric_reason?: Record<string, Record<string, number>>;
    missing_reason_counts?: Record<string, number>;
    missing_reason_labels?: Record<string, string>;
  };
  snapshot_coverage?: {
    stocks_with_snapshots_as_of: number;
    archived_stocks: number;
    first_fetched_at: string | null;
  };
  stocks: Array<{
    stock_id: number;
    symbol: string;
    name: string;
    metrics: Partial<Record<FundamentalMetric, number | null>>;
    market_cap_source: string | null;
  }>;
  missing_stocks: Array<{
    stock_id: number;
    symbol: string;
    name: string;
    missing: Array<{
      metric: FundamentalMetric;
      code: string;
      label: string;
      basis: string | null;
    }>;
    known_failures: Array<{
      metric: FundamentalMetric;
      label: string;
      operator: FundamentalOperator;
      threshold: number;
      actual: number;
    }>;
  }>;
  limitations: string[];
}

export interface FundamentalQueryBacktestJob {
  job_id: string;
  status: 'queued' | 'running' | 'completed' | 'partial' | 'failed' | 'cancelled' | string;
  query: FundamentalQuery;
  universe_selection?: {
    as_of: string;
    stock_count: number;
    symbols: string[];
    source: 'query_preview' | 'saved_query' | string;
  };
  settings?: {
    target_r: number;
    max_hold_bars: number;
    stop_buffer_pct: number;
    capital_per_stock?: number;
    initial_capital?: number;
    candle_interval?: string;
    setup_ids?: string[];
  };
  progress: { completed: number; total: number };
  qualification_timeline: Array<{
    as_of: string;
    data_cutoff?: string;
    eligible_count: number;
    insufficient_count: number;
  }>;
  leaderboard: Array<{
    setup_id: string;
    setup_name: string;
    stock_count: number;
    trades: number;
    win_rate_pct: number | null;
    total_return_pct: number | null;
    portfolio_initial_capital?: number | null;
    portfolio_final_capital?: number | null;
    portfolio_period_return_pct?: number | null;
    average_return_pct: number | null;
    max_drawdown_pct: number | null;
  }>;
  trades: Array<{
    symbol: string;
    stock_name: string;
    setup_id: string;
    signal_date: string;
    entry_date: string;
    entry_price: number;
    exit_date: string;
    exit_price: number;
    return_pct: number;
    exit_reason: string;
    poc: number | null;
    swing_low?: number | null;
    swing_high?: number | null;
    value_area_low?: number | null;
    value_area_high?: number | null;
    stop_price: number | null;
    target_price: number | null;
  }>;
  failed_stocks?: Array<{ symbol: string; error: string }>;
  error: string | null;
  limitations: string[];
}

const BASE_URL = process.env.NEXT_PUBLIC_API_URL || 'http://127.0.0.1:8000';

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE_URL}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
    cache: 'no-store',
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(typeof payload.detail === 'string' ? payload.detail : JSON.stringify(payload.detail));
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}

export async function listFundamentalQueries(): Promise<FundamentalQuery[]> {
  const response = await request<{ queries: FundamentalQuery[] }>('/api/fundamental-queries');
  return response.queries;
}

export async function createFundamentalQuery(name: string, rules: FundamentalRule[]): Promise<FundamentalQuery> {
  const response = await request<{ query: FundamentalQuery }>('/api/fundamental-queries', {
    method: 'POST', body: JSON.stringify({ name, rules }),
  });
  return response.query;
}

export async function updateFundamentalQuery(id: number, name: string, rules: FundamentalRule[]): Promise<FundamentalQuery> {
  const response = await request<{ query: FundamentalQuery }>(`/api/fundamental-queries/${id}`, {
    method: 'PUT', body: JSON.stringify({ name, rules }),
  });
  return response.query;
}

export async function deleteFundamentalQuery(id: number): Promise<void> {
  await request<void>(`/api/fundamental-queries/${id}`, { method: 'DELETE' });
}

export function previewFundamentalQuery(rules: FundamentalRule[], asOf?: string): Promise<FundamentalQueryPreview> {
  return request('/api/fundamental-queries/preview', {
    method: 'POST', body: JSON.stringify({ rules, ...(asOf ? { as_of: asOf } : {}) }),
  });
}

export async function listFundamentalSetupPresets(): Promise<FundamentalSetupPreset[]> {
  const response = await request<{ setups: FundamentalSetupPreset[] }>('/api/fundamental-setup-presets');
  return response.setups;
}

export async function startFundamentalQueryBacktest(payload: {
  query_id: number;
  start_date: string;
  end_date: string;
  selected_stock_ids: number[];
  universe_as_of: string;
  setup_ids: string[];
  target_r?: number;
  max_hold_bars?: number;
  stop_buffer_pct?: number;
}): Promise<{ job_id: string; status: string }> {
  return request('/api/fundamental-query-backtests', { method: 'POST', body: JSON.stringify(payload) });
}

export function getFundamentalQueryBacktest(jobId: string): Promise<FundamentalQueryBacktestJob> {
  return request(`/api/fundamental-query-backtests/${encodeURIComponent(jobId)}`);
}
