import type {
  FilterDefinition,
  FilterBacktestResult,
  FilterBacktestPreset,
  FilterExpression,
  FilterMode,
  FilterPreviewResponse,
  FilterBacktestRun,
  FilterProfile,
  FilterProfileVersion,
  TrendlineDefinition,
  TrendlinePoint,
  WatchlistFilterAnalysisJob,
} from './filter-types';

// Filter backtests send JSON POST bodies. Calling FastAPI directly avoids the
// local Next rewrite, which currently fails while proxying this request body.
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
  return response.json();
}

export async function getFilterCatalog(): Promise<{
  filters: FilterDefinition[];
  available_count: number;
  planned_count: number;
  categories: string[];
}> {
  return request('/api/filters/catalog');
}

export async function validateFilterExpression(
  mode: FilterMode,
  expression: FilterExpression,
): Promise<{ valid: boolean; requirements: Record<string, unknown>; errors: string[]; expression?: FilterExpression }> {
  return request('/api/filter-profiles/validate', {
    method: 'POST',
    body: JSON.stringify({ mode, expression }),
  });
}

export async function getFilterProfiles(): Promise<FilterProfile[]> {
  const payload = await request<{ profiles: FilterProfile[] }>('/api/filter-profiles');
  return payload.profiles;
}

export async function createFilterProfile(payload: {
  name: string;
  description: string;
  mode: FilterMode;
  expression: FilterExpression;
  risk_config?: Record<string, unknown>;
  timeframe_config?: Record<string, unknown>;
}): Promise<{ profile: FilterProfile; requirements: Record<string, unknown> }> {
  return request('/api/filter-profiles', { method: 'POST', body: JSON.stringify(payload) });
}

export async function updateFilterProfileDraft(
  profileId: number,
  payload: {
    mode: FilterMode;
    expression: FilterExpression;
    expected_version_id?: number;
  },
): Promise<{ version: FilterProfileVersion; requirements: Record<string, unknown> }> {
  return request(`/api/filter-profiles/${profileId}/draft`, { method: 'PUT', body: JSON.stringify(payload) });
}

export async function previewFilterProfile(
  profileId: number,
  payload: { symbols: string[]; mode: FilterMode; limit: number },
): Promise<FilterPreviewResponse> {
  return request(`/api/filter-profiles/${profileId}/preview`, { method: 'POST', body: JSON.stringify(payload) });
}

export async function backtestFilterProfile(
  profileId: number,
  payload: {
    symbol: string;
    start_date: string;
    end_date: string;
    initial_capital: number;
    position_size_pct?: number;
    commission_pct?: number;
    slippage_pct?: number;
    stop_loss_pct?: number;
    take_profit_pct?: number;
    max_hold_bars?: number;
    close_on_filter_fail?: boolean;
  },
): Promise<{ run_id: number; profile_id: number; version_id: number; status: string; result: FilterBacktestResult }> {
  return request(`/api/filter-profiles/${profileId}/backtest`, { method: 'POST', body: JSON.stringify(payload) });
}

export async function getFilterBacktestPresets(): Promise<FilterBacktestPreset[]> {
  const payload = await request<{ presets: FilterBacktestPreset[] }>('/api/filter-backtest-presets');
  return payload.presets;
}

export type FilterBacktestRequestPayload = {
  symbol: string;
  start_date: string;
  end_date: string;
  initial_capital: number;
  position_size_pct?: number;
  commission_pct?: number;
  slippage_pct?: number;
  stop_loss_pct?: number;
  take_profit_pct?: number;
  max_hold_bars?: number;
  close_on_filter_fail?: boolean;
};

export async function runFilterBacktestPreset(
  presetId: string,
  payload: FilterBacktestRequestPayload,
): Promise<{ run_id: number; preset_id: string; preset_name: string; status: string; result: FilterBacktestResult }> {
  return request(`/api/filter-backtest-presets/${encodeURIComponent(presetId)}`, { method: 'POST', body: JSON.stringify(payload) });
}

export type FilterBacktestBatchRow = {
  run_id: number | null;
  preset_id: string;
  preset_name: string;
  status: string;
  result: FilterBacktestResult | null;
  error?: string;
};

export async function runFilterBacktestBatch(
  presetIds: string[],
  payload: FilterBacktestRequestPayload,
): Promise<{ status: string; completed: number; total: number; results: FilterBacktestBatchRow[] }> {
  return request('/api/filter-backtests/batch', {
    method: 'POST',
    body: JSON.stringify({ ...payload, preset_ids: presetIds }),
  });
}

export type WatchlistFilterAnalysisPayload = {
  watchlist_id?: number;
  symbols?: string[];
  start_date: string;
  end_date: string;
  initial_capital: number;
  preset_ids?: string[];
};

export async function startWatchlistFilterAnalysis(
  payload: WatchlistFilterAnalysisPayload,
): Promise<WatchlistFilterAnalysisJob> {
  return request('/api/filter-backtests/watchlist-analysis', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export async function getWatchlistFilterAnalysis(
  jobId: string,
): Promise<WatchlistFilterAnalysisJob> {
  return request(`/api/filter-backtests/watchlist-analysis/${encodeURIComponent(jobId)}`);
}

export async function getFilterBacktest(runId: number): Promise<{ run: FilterBacktestRun }> {
  return request(`/api/filter-backtests/${runId}`);
}

export async function deleteFilterBacktest(runId: number): Promise<void> {
  await request(`/api/filter-backtests/${runId}`, { method: 'DELETE' });
}

export async function getTrendlines(symbol: string, interval: string): Promise<TrendlineDefinition[]> {
  const payload = await request<{ trendlines: TrendlineDefinition[] }>(`/api/trendlines?symbol=${encodeURIComponent(symbol)}&interval=${encodeURIComponent(interval)}`);
  return payload.trendlines;
}

export async function createTrendline(payload: {
  symbol: string;
  interval: string;
  points: TrendlinePoint[];
  trendline_type?: string;
  source?: string;
}): Promise<TrendlineDefinition> {
  const result = await request<{ trendline: TrendlineDefinition }>('/api/trendlines', { method: 'POST', body: JSON.stringify(payload) });
  return result.trendline;
}

export async function deleteTrendline(id: number): Promise<void> {
  await request(`/api/trendlines/${id}`, { method: 'DELETE' });
}
