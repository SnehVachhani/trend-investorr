'use client';

import Link from 'next/link';
import { Fragment, useCallback, useEffect, useRef, useState } from 'react';
import { getWatchlistStocks, getWatchlists } from '@/lib/api';
import { getFilterBacktest, getFilterBacktestPresets, getWatchlistFilterAnalysis, startWatchlistFilterAnalysis } from '@/lib/filter-api';
import { usePersistentState } from '@/lib/persistence';
import type {
  FilterBacktestResult,
  WatchlistAnalysisCombination,
  WatchlistAnalysisStockResult,
  WatchlistFilterAnalysisJob,
  WatchlistFilterLeaderboardRow,
} from '@/lib/filter-types';
import type { Stock, Watchlist } from '@/lib/types';
import FilterBacktestResultDetails from './FilterBacktestResultDetails';
import StockIdentity from './StockIdentity';

type Source = 'watchlist' | 'manual';
type LeaderboardStockSortKey =
  | 'stock'
  | 'behaviour'
  | 'oneYearMove'
  | 'trendEfficiency'
  | 'filterReturn'
  | 'drawdown'
  | 'trades'
  | 'status'
  | 'tradeLog';
type SortDirection = 'asc' | 'desc';

const TERMINAL_STATUSES = new Set(['completed', 'partial', 'failed', 'cancelled']);

function dateInputValue(date: Date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

function defaultDateRange() {
  const end = new Date();
  const start = new Date(end);
  start.setFullYear(start.getFullYear() - 1);
  return { start: dateInputValue(start), end: dateInputValue(end) };
}

function metric(value: number | null | undefined, suffix = '%', digits = 2) {
  return value == null || Number.isNaN(value) ? '—' : `${value.toFixed(digits)}${suffix}`;
}

function actualCandleWindow(combination: WatchlistAnalysisCombination) {
  if (!combination.start_date || !combination.end_date) return '—';
  const format = (value: string) => new Intl.DateTimeFormat('en-IN', {
    timeZone: 'Asia/Kolkata',
    day: '2-digit',
    month: 'short',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  }).format(new Date(value));
  const timeframe = combination.execution_timeframe ? `${combination.execution_timeframe} · ` : '';
  return `${timeframe}${format(combination.start_date)} – ${format(combination.end_date)} IST`;
}

function returnClass(value: number | null | undefined) {
  return (value ?? 0) >= 0 ? 'wl-analysis-positive' : 'wl-analysis-negative';
}

function currency(value: number | null | undefined) {
  return value == null || Number.isNaN(value) ? '—' : `₹${value.toLocaleString('en-IN', { maximumFractionDigits: 0 })}`;
}

function watchlistBuyAndHoldReturn(job: WatchlistFilterAnalysisJob | null) {
  if (job?.average_watchlist_return_pct != null) return job.average_watchlist_return_pct;
  const returns = (job?.stock_results ?? [])
    .map((stock) => stock.market_profile.total_return_pct)
    .filter((value): value is number => typeof value === 'number' && Number.isFinite(value));
  return returns.length > 0 ? returns.reduce((sum, value) => sum + value, 0) / returns.length : null;
}

function SortableLeaderboardStockHeader({
  column,
  label,
  sort,
  onSort,
}: {
  column: LeaderboardStockSortKey;
  label: string;
  sort: { column: LeaderboardStockSortKey; direction: SortDirection };
  onSort: (column: LeaderboardStockSortKey) => void;
}) {
  const active = sort.column === column;
  return (
    <th aria-sort={active ? (sort.direction === 'desc' ? 'descending' : 'ascending') : 'none'}>
      <button type="button" className="wl-analysis-sort-button" onClick={() => onSort(column)}>
        {label} {active && <span aria-hidden="true">{sort.direction === 'desc' ? '↓' : '↑'}</span>}
      </button>
    </th>
  );
}

function parseSymbols(value: string) {
  return Array.from(new Set(
    value
      .split(/[\s,;]+/)
      .map((symbol) => symbol.trim().toUpperCase())
      .filter(Boolean),
  ));
}

function LeaderboardRow({ row, compact = false, onViewStocks }: {
  row: WatchlistFilterLeaderboardRow;
  compact?: boolean;
  onViewStocks?: (row: WatchlistFilterLeaderboardRow) => void;
}) {
  return (
    <div className={`wl-analysis-leader-row${compact ? ' is-compact' : ''}`}>
      <div className="wl-analysis-leader-name">{row.preset_name}</div>
      <div className={returnClass(row.average_return_pct)}>{metric(row.average_return_pct)}</div>
      {!compact && <div className="wl-analysis-portfolio-value">{currency(row.portfolio_final_capital)}</div>}
      {!compact && <div>{metric(row.median_return_pct)}</div>}
      <div>{metric(row.average_max_drawdown_pct)}</div>
      {!compact && <div>{row.positive_stock_count}/{row.stock_count}</div>}
      {!compact && <div><button type="button" className="btn btn-secondary wl-analysis-open-button" onClick={() => onViewStocks?.(row)}>View stocks</button></div>}
    </div>
  );
}

function StockCombinations({ stock, onViewTrades }: {
  stock: WatchlistAnalysisStockResult;
  onViewTrades: (stock: WatchlistAnalysisStockResult, combination: WatchlistAnalysisCombination) => void;
}) {
  const combinations = [...stock.combinations].sort((left, right) => (right.total_return_pct ?? -Infinity) - (left.total_return_pct ?? -Infinity));
  return (
    <div className="wl-analysis-stock-combinations">
      <table className="data-table wl-analysis-combination-table">
        <thead>
          <tr>
            <th>Filter combination</th>
            <th>Return</th>
            <th>Charges</th>
            <th>Drawdown</th>
            <th>Sharpe</th>
            <th>Trades</th>
            <th>Actual candle window (IST)</th>
            <th>Status</th>
            <th>Trade log</th>
          </tr>
        </thead>
        <tbody>
          {combinations.map((combination) => (
            <tr key={combination.preset_id}>
              <td>{combination.preset_name}</td>
              <td className={returnClass(combination.total_return_pct)}>{metric(combination.total_return_pct)}</td>
              <td>{combination.total_charges == null ? '—' : `₹${combination.total_charges.toLocaleString('en-IN', { maximumFractionDigits: 0 })}`}</td>
              <td>{metric(combination.max_drawdown_pct)}</td>
              <td>{metric(combination.sharpe_ratio, '', 3)}</td>
              <td>{combination.total_trades ?? '—'}</td>
              <td>{actualCandleWindow(combination)}</td>
              <td><span className={`badge ${combination.status === 'completed' ? 'badge-buy' : 'badge-sell'}`}>{combination.status === 'completed' ? 'Complete' : 'Failed'}</span></td>
              <td><button type="button" className="btn btn-secondary wl-analysis-open-button" onClick={() => onViewTrades(stock, combination)} disabled={combination.status !== 'completed' || combination.run_id == null}>View trades</button></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function WatchlistBacktestPanel() {
  const defaults = defaultDateRange();
  const [source, setSource] = usePersistentState<Source>('workspace.watchlistAnalysis.source', 'watchlist');
  const [watchlists, setWatchlists] = useState<Watchlist[]>([]);
  const [watchlistId, setWatchlistId] = usePersistentState<number | null>('workspace.watchlistAnalysis.watchlistId', null);
  const [watchlistStocks, setWatchlistStocks] = useState<Stock[]>([]);
  const [manualSymbols, setManualSymbols] = usePersistentState('workspace.watchlistAnalysis.manualSymbols', '');
  const [presetCount, setPresetCount] = useState(0);
  const [startDate, setStartDate] = usePersistentState('workspace.watchlistAnalysis.startDate', defaults.start);
  const [endDate, setEndDate] = usePersistentState('workspace.watchlistAnalysis.endDate', defaults.end);
  const [capital, setCapital] = usePersistentState('workspace.watchlistAnalysis.capital', '1000000');
  const [job, setJob] = usePersistentState<WatchlistFilterAnalysisJob | null>('workspace.watchlistAnalysis.job', null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [expandedSymbol, setExpandedSymbol] = useState<string | null>(null);
  const [selectedTradeDetails, setSelectedTradeDetails] = useState<{ stock: WatchlistAnalysisStockResult; combination: WatchlistAnalysisCombination } | null>(null);
  const [tradeResult, setTradeResult] = useState<FilterBacktestResult | null>(null);
  const [tradeDetailsLoading, setTradeDetailsLoading] = useState(false);
  const [tradeDetailsError, setTradeDetailsError] = useState('');
  const [selectedLeaderboardRow, setSelectedLeaderboardRow] = useState<WatchlistFilterLeaderboardRow | null>(null);
  const [leaderboardStockSort, setLeaderboardStockSort] = useState<{ column: LeaderboardStockSortKey; direction: SortDirection }>({
    column: 'filterReturn',
    direction: 'desc',
  });
  const pollTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const activeJobId = useRef<string | null>(null);
  const tradeDetailsRequest = useRef(0);

  const stopPolling = useCallback(() => {
    if (pollTimer.current) {
      clearInterval(pollTimer.current);
      pollTimer.current = null;
    }
  }, []);

  const refreshJob = useCallback(async (jobId: string) => {
    try {
      const nextJob = await getWatchlistFilterAnalysis(jobId);
      if (activeJobId.current !== jobId) return;
      setJob(nextJob);
      if (TERMINAL_STATUSES.has(nextJob.status)) stopPolling();
    } catch (requestError) {
      if (activeJobId.current !== jobId) return;
      stopPolling();
      setError(requestError instanceof Error ? requestError.message : 'Unable to read analysis progress');
    }
  }, [stopPolling]);

  useEffect(() => {
    Promise.all([getWatchlists(), getFilterBacktestPresets()])
      .then(([availableWatchlists, presets]) => {
        setWatchlists(availableWatchlists);
        setPresetCount(presets.length);
        setWatchlistId((current) => (
          current != null && availableWatchlists.some((watchlist) => watchlist.id === current)
            ? current
            : availableWatchlists[0]?.id ?? null
        ));
        if (availableWatchlists.length === 0) setSource('manual');
      })
      .catch((requestError) => setError(requestError instanceof Error ? requestError.message : 'Unable to load analysis inputs'))
      .finally(() => setLoading(false));
    return () => {
      activeJobId.current = null;
      stopPolling();
    };
  }, [stopPolling]);

  useEffect(() => {
    if (!job || TERMINAL_STATUSES.has(job.status)) return;
    activeJobId.current = job.job_id;
    void refreshJob(job.job_id);
    pollTimer.current = setInterval(() => void refreshJob(job.job_id), 1000);
    return stopPolling;
  }, [job?.job_id, job?.status, refreshJob, stopPolling]);

  useEffect(() => {
    if (source !== 'watchlist' || watchlistId == null) {
      setWatchlistStocks([]);
      return;
    }
    let cancelled = false;
    getWatchlistStocks(watchlistId)
      .then((stocks) => {
        if (!cancelled) setWatchlistStocks(stocks);
      })
      .catch(() => {
        if (!cancelled) setWatchlistStocks([]);
      });
    return () => {
      cancelled = true;
    };
  }, [source, watchlistId]);

  const closeTradeDetails = useCallback(() => {
    tradeDetailsRequest.current += 1;
    setSelectedTradeDetails(null);
    setTradeResult(null);
    setTradeDetailsLoading(false);
    setTradeDetailsError('');
  }, []);

  const closeLeaderboardStocks = useCallback(() => {
    setSelectedLeaderboardRow(null);
  }, []);

  const toggleLeaderboardStockSort = useCallback((column: LeaderboardStockSortKey) => {
    setLeaderboardStockSort((current) => (
      current.column === column
        ? { column, direction: current.direction === 'desc' ? 'asc' : 'desc' }
        : { column, direction: column === 'stock' || column === 'behaviour' || column === 'status' ? 'asc' : 'desc' }
    ));
  }, []);

  const openTradeDetails = async (stock: WatchlistAnalysisStockResult, combination: WatchlistAnalysisCombination) => {
    if (combination.run_id == null || combination.status !== 'completed') return;
    closeLeaderboardStocks();
    const requestId = ++tradeDetailsRequest.current;
    setSelectedTradeDetails({ stock, combination });
    setTradeResult(null);
    setTradeDetailsError('');
    setTradeDetailsLoading(true);
    try {
      const response = await getFilterBacktest(combination.run_id);
      if (requestId === tradeDetailsRequest.current) setTradeResult(response.run.result);
    } catch (requestError) {
      if (requestId === tradeDetailsRequest.current) {
        setTradeDetailsError(requestError instanceof Error ? requestError.message : 'Unable to load trade details');
      }
    } finally {
      if (requestId === tradeDetailsRequest.current) setTradeDetailsLoading(false);
    }
  };

  useEffect(() => {
    if (!selectedTradeDetails) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') closeTradeDetails();
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [closeTradeDetails, selectedTradeDetails]);

  useEffect(() => {
    if (!selectedLeaderboardRow) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') closeLeaderboardStocks();
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [closeLeaderboardStocks, selectedLeaderboardRow]);

  const runAnalysis = async () => {
    const initialCapital = Number(capital);
    const manual = parseSymbols(manualSymbols);
    if (!Number.isFinite(initialCapital) || initialCapital <= 0) {
      setError('Initial capital must be greater than 0.');
      return;
    }
    if (source === 'watchlist' && watchlistId == null) {
      setError('Choose a watchlist first.');
      return;
    }
    if (source === 'manual' && manual.length === 0) {
      setError('Enter at least one stock symbol.');
      return;
    }
    stopPolling();
    // Ignore an already in-flight poll for the job that is about to be
    // replaced; it must not overwrite this request's queued state.
    activeJobId.current = null;
    closeTradeDetails();
    closeLeaderboardStocks();
    setError('');
    setExpandedSymbol(null);
    try {
      const started = await startWatchlistFilterAnalysis({
        ...(source === 'watchlist' ? { watchlist_id: watchlistId! } : { symbols: manual }),
        start_date: startDate,
        end_date: endDate,
        initial_capital: initialCapital,
      });
      activeJobId.current = started.job_id;
      setJob(started);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Unable to start watchlist analysis');
    }
  };

  const isRunning = job?.status === 'queued' || job?.status === 'running';
  const progress = job?.total_backtests ? Math.round((job.completed_backtests / job.total_backtests) * 100) : 0;
  const sharedWatchlistReturn = watchlistBuyAndHoldReturn(job);
  const selectedWatchlist = watchlists.find((watchlist) => watchlist.id === watchlistId);
  const leaderboardStockRows = (selectedLeaderboardRow && job
    ? job.stock_results.map((stock) => ({
      stock,
      combination: stock.combinations.find((combination) => combination.preset_id === selectedLeaderboardRow.preset_id),
    }))
    : []).sort((left, right) => {
    const valueFor = ({ stock, combination }: typeof left): string | number | null => {
      const status = combination?.status ?? stock.status;
      switch (leaderboardStockSort.column) {
        case 'stock': return stock.symbol;
        case 'behaviour': return stock.market_profile.behavior?.label ?? null;
        case 'oneYearMove': return stock.market_profile.total_return_pct ?? null;
        case 'trendEfficiency': return stock.market_profile.trend_efficiency_pct ?? null;
        case 'filterReturn': return combination?.total_return_pct ?? null;
        case 'drawdown': return combination?.max_drawdown_pct ?? null;
        case 'trades': return combination?.total_trades ?? null;
        case 'status': return status;
        case 'tradeLog': return combination?.status === 'completed' && combination.run_id != null ? 1 : 0;
      }
    };
    const leftValue = valueFor(left);
    const rightValue = valueFor(right);
    if (leftValue == null) return rightValue == null ? 0 : 1;
    if (rightValue == null) return -1;
    const comparison = typeof leftValue === 'number' && typeof rightValue === 'number'
      ? leftValue - rightValue
      : String(leftValue).localeCompare(String(rightValue), undefined, { numeric: true, sensitivity: 'base' });
    return leaderboardStockSort.direction === 'asc' ? comparison : -comparison;
  });

  if (loading) {
    return <div className="bt-processing"><div className="bt-processing-spinner" /><div className="bt-processing-text">Loading watchlists…</div></div>;
  }

  return (
    <div className="wl-analysis-panel">
      <div className="wl-analysis-header">
        <div>
          <h1 className="bt-section-title">Watchlist analysis</h1>
          <p>Compare every filter combination across a group of stocks.</p>
        </div>
        <Link href="/watchlists" className="btn btn-secondary">Manage watchlists</Link>
      </div>

      {error && <div className="bt-error">{error}</div>}

      <section className="card wl-analysis-config">
        <div className="wl-analysis-source-toggle" role="group" aria-label="Analysis source">
          <button type="button" className={source === 'watchlist' ? 'is-selected' : ''} onClick={() => setSource('watchlist')} disabled={watchlists.length === 0}>Saved watchlist</button>
          <button type="button" className={source === 'manual' ? 'is-selected' : ''} onClick={() => setSource('manual')}>Paste symbols</button>
        </div>

        {source === 'watchlist' ? (
          <div className="wl-analysis-watchlist-field">
            <label className="bt-field-label" htmlFor="watchlist-analysis-source">Watchlist</label>
            <select id="watchlist-analysis-source" className="bt-field-select" value={watchlistId ?? ''} onChange={(event) => setWatchlistId(Number(event.target.value))}>
              {watchlists.map((watchlist) => <option key={watchlist.id} value={watchlist.id}>{watchlist.name} · {watchlist.stock_count} stocks</option>)}
            </select>
            <small>{selectedWatchlist ? `${watchlistStocks.length || selectedWatchlist.stock_count} stocks will be analyzed independently.` : 'Create a watchlist first, or paste symbols.'}</small>
          </div>
        ) : (
          <div className="wl-analysis-watchlist-field">
            <label className="bt-field-label" htmlFor="watchlist-analysis-symbols">Stock symbols</label>
            <textarea id="watchlist-analysis-symbols" className="bt-field-input wl-analysis-symbol-input" value={manualSymbols} onChange={(event) => setManualSymbols(event.target.value)} placeholder="RELIANCE, TCS, INFY\nOne symbol per line also works" />
            <small>{parseSymbols(manualSymbols).length} unique symbols</small>
          </div>
        )}

        <div className="bt-date-row">
          <div className="bt-field"><label className="bt-field-label" htmlFor="watchlist-analysis-start">Start date</label><input id="watchlist-analysis-start" type="date" className="bt-field-input" value={startDate} onChange={(event) => setStartDate(event.target.value)} /></div>
          <div className="bt-field"><label className="bt-field-label" htmlFor="watchlist-analysis-end">End date</label><input id="watchlist-analysis-end" type="date" className="bt-field-input" value={endDate} onChange={(event) => setEndDate(event.target.value)} /></div>
        </div>
        <div className="bt-field"><label className="bt-field-label" htmlFor="watchlist-analysis-capital">Portfolio capital (₹)</label><input id="watchlist-analysis-capital" type="number" min="1" className="bt-field-input" value={capital} onChange={(event) => setCapital(event.target.value)} /></div>
        <div className="wl-analysis-method-note">Each stock/filter pair is simulated independently. This fund is split equally across the selected stocks, then recombined into the portfolio value shown below.</div>
        <button className="bt-run-btn" type="button" onClick={runAnalysis} disabled={source === 'watchlist' ? watchlistId == null : parseSymbols(manualSymbols).length === 0}>
          {isRunning ? 'Restart with current settings' : `Run all ${presetCount} filter combinations`}
        </button>
      </section>

      {job && (
        <section className="card wl-analysis-progress" aria-live="polite">
          <div className="wl-analysis-progress-title"><strong>{job.status === 'completed' ? 'Analysis complete' : job.status === 'partial' ? 'Analysis completed with some failures' : job.status === 'failed' ? 'Analysis failed' : job.status === 'cancelled' ? 'Analysis replaced by a newer request' : 'Analysis in progress'}</strong><span>{job.completed_stocks}/{job.total_stocks} stocks</span></div>
          <div className="wl-analysis-progress-track"><span style={{ width: `${progress}%` }} /></div>
          <div className="wl-analysis-progress-meta"><span>{job.completed_backtests}/{job.total_backtests} filter backtests finished</span><span>{progress}%</span></div>
          {job.error && <div className="bt-error">{job.error}</div>}
        </section>
      )}

      {job && job.status !== 'cancelled' && TERMINAL_STATUSES.has(job.status) && job.stock_results.length > 0 && (
        <div className="wl-analysis-results">
          <section className="wl-analysis-section">
            <div className="wl-analysis-section-heading"><h2>Best filter by stock behaviour</h2><span>Return first; ties use lower drawdown, then higher Sharpe.</span></div>
            <div className="wl-analysis-regime-grid">
              {job.regime_recommendations.map((recommendation) => (
                <article className="card wl-analysis-regime-card" key={recommendation.regime_id}>
                  <div className="wl-analysis-regime-top"><span className="badge badge-paper">{recommendation.stock_count} stocks</span><strong>{recommendation.regime_label}</strong></div>
                  <p>{recommendation.description}</p>
                  {recommendation.recommended_filter ? <><div className="wl-analysis-recommendation-name">{recommendation.recommended_filter.preset_name}</div><div className={returnClass(recommendation.recommended_filter.average_return_pct)}>{metric(recommendation.recommended_filter.average_return_pct)} average return</div><small className="wl-analysis-symbol-list">{recommendation.symbols.map((symbol) => <StockIdentity key={symbol} className="wl-analysis-symbol-identity" symbol={symbol} />)}</small></> : <small>No completed filter results for this behaviour.</small>}
                </article>
              ))}
            </div>
          </section>

          <section className="wl-analysis-section">
            <div className="wl-analysis-section-heading"><h2>Overall filter leaderboard <span className={`wl-analysis-shared-return ${sharedWatchlistReturn == null ? '' : returnClass(sharedWatchlistReturn)}`}>({sharedWatchlistReturn == null ? 'Watchlist buy-and-hold avg: —' : `Watchlist buy-and-hold avg: ${metric(sharedWatchlistReturn)}`})</span></h2><span>Equal-weight portfolio value from {currency(job.portfolio_initial_capital)}</span></div>
            <div className="card wl-analysis-leaderboard">
              <div className="wl-analysis-leader-row wl-analysis-leader-head"><div>Filter combination</div><div>Avg return</div><div>Portfolio value</div><div>Median return</div><div>Avg drawdown</div><div>Positive stocks</div><div>Stock results</div></div>
              {job.overall_leaderboard.map((row) => <LeaderboardRow key={row.preset_id} row={row} onViewStocks={(selectedRow) => { setLeaderboardStockSort({ column: 'filterReturn', direction: 'desc' }); setSelectedLeaderboardRow(selectedRow); }} />)}
            </div>
          </section>

          <section className="wl-analysis-section">
            <div className="wl-analysis-section-heading"><h2>Stocks and winning filters</h2><span>Open a row to compare every combination for that stock.</span></div>
            <div className="card wl-analysis-stock-table-wrap">
              <table className="data-table wl-analysis-stock-table">
                <thead><tr><th>Stock</th><th>Sector</th><th>Behaviour</th><th>1Y move</th><th>Trend efficiency</th><th>Best filter</th><th>Filter return</th><th /></tr></thead>
                <tbody>
                  {job.stock_results.map((stock) => {
                    const isExpanded = expandedSymbol === stock.symbol;
                    return <Fragment key={stock.symbol}>
                      <tr key={stock.symbol} className={isExpanded ? 'is-expanded' : ''}>
                        <td><StockIdentity symbol={stock.symbol} name={stock.stock_name} company_name={stock.company_name} /></td>
                        <td>{stock.sector || '—'}</td>
                        <td><span className="badge">{stock.market_profile.behavior?.label || 'Unavailable'}</span></td>
                        <td className={returnClass(stock.market_profile.total_return_pct)}>{metric(stock.market_profile.total_return_pct)}</td>
                        <td>{metric(stock.market_profile.trend_efficiency_pct)}</td>
                        <td>{stock.best_combination?.preset_name || '—'}</td>
                        <td className={returnClass(stock.best_combination?.total_return_pct)}>{metric(stock.best_combination?.total_return_pct)}</td>
                        <td><button type="button" className="btn btn-secondary wl-analysis-open-button" onClick={() => setExpandedSymbol(isExpanded ? null : stock.symbol)} disabled={stock.combinations.length === 0}>{isExpanded ? 'Hide filters' : 'View filters'}</button></td>
                      </tr>
                      {isExpanded && <tr className="wl-analysis-stock-detail"><td colSpan={8}><StockCombinations stock={stock} onViewTrades={openTradeDetails} /></td></tr>}
                    </Fragment>;
                  })}
                </tbody>
              </table>
            </div>
          </section>
        </div>
      )}

      {selectedTradeDetails && (
        <div className="bt-comparison-modal-backdrop" role="presentation" onClick={closeTradeDetails}>
          <div className="bt-comparison-modal" role="dialog" aria-modal="true" aria-labelledby="watchlist-trade-modal-title" onClick={(event) => event.stopPropagation()}>
            <div className="bt-comparison-modal-header">
              <div>
                <span className="bt-comparison-detail-label">Selected combination</span>
                <h2 id="watchlist-trade-modal-title" className="bt-comparison-detail-title">{selectedTradeDetails.combination.preset_name}</h2>
                <p className="bt-comparison-subtitle">Trade details for <StockIdentity symbol={selectedTradeDetails.stock.symbol} name={selectedTradeDetails.stock.stock_name} company_name={selectedTradeDetails.stock.company_name} sector={selectedTradeDetails.stock.sector} />.</p>
              </div>
              <button type="button" className="btn btn-secondary bt-comparison-close" onClick={closeTradeDetails}>Close</button>
            </div>
            <FilterBacktestResultDetails result={tradeResult} loading={tradeDetailsLoading} error={tradeDetailsError} />
          </div>
        </div>
      )}

      {selectedLeaderboardRow && job && (
        <div className="bt-comparison-modal-backdrop" role="presentation" onClick={closeLeaderboardStocks}>
          <div className="bt-comparison-modal wl-analysis-leaderboard-modal" role="dialog" aria-modal="true" aria-labelledby="watchlist-leaderboard-modal-title" onClick={(event) => event.stopPropagation()}>
            <div className="bt-comparison-modal-header">
              <div>
                <span className="bt-comparison-detail-label">Filter combination</span>
                <h2 id="watchlist-leaderboard-modal-title" className="bt-comparison-detail-title">{selectedLeaderboardRow.preset_name}</h2>
                <p className="bt-comparison-subtitle">Results for all {job.total_stocks} stocks in this watchlist.</p>
              </div>
              <button type="button" className="btn btn-secondary bt-comparison-close" onClick={closeLeaderboardStocks}>Close</button>
            </div>
            <div className="wl-analysis-leaderboard-stock-table-wrap">
              <table className="data-table wl-analysis-leaderboard-stock-table">
                <thead><tr><SortableLeaderboardStockHeader column="stock" label="Stock" sort={leaderboardStockSort} onSort={toggleLeaderboardStockSort} /><th>Sector</th><SortableLeaderboardStockHeader column="behaviour" label="Market behaviour" sort={leaderboardStockSort} onSort={toggleLeaderboardStockSort} /><SortableLeaderboardStockHeader column="oneYearMove" label="1Y move" sort={leaderboardStockSort} onSort={toggleLeaderboardStockSort} /><SortableLeaderboardStockHeader column="trendEfficiency" label="Trend efficiency" sort={leaderboardStockSort} onSort={toggleLeaderboardStockSort} /><SortableLeaderboardStockHeader column="filterReturn" label="Filter return" sort={leaderboardStockSort} onSort={toggleLeaderboardStockSort} /><SortableLeaderboardStockHeader column="drawdown" label="Drawdown" sort={leaderboardStockSort} onSort={toggleLeaderboardStockSort} /><SortableLeaderboardStockHeader column="trades" label="Trades" sort={leaderboardStockSort} onSort={toggleLeaderboardStockSort} /><SortableLeaderboardStockHeader column="status" label="Status" sort={leaderboardStockSort} onSort={toggleLeaderboardStockSort} /><SortableLeaderboardStockHeader column="tradeLog" label="Trade log" sort={leaderboardStockSort} onSort={toggleLeaderboardStockSort} /></tr></thead>
                <tbody>
                  {leaderboardStockRows.map(({ stock, combination }) => {
                    const status = combination?.status ?? stock.status;
                    return (
                      <tr key={stock.symbol}>
                        <td><StockIdentity symbol={stock.symbol} name={stock.stock_name} company_name={stock.company_name} /></td>
                        <td>{stock.sector || '—'}</td>
                        <td><span className="badge">{stock.market_profile.behavior?.label || 'Unavailable'}</span></td>
                        <td className={returnClass(stock.market_profile.total_return_pct)}>{metric(stock.market_profile.total_return_pct)}</td>
                        <td>{metric(stock.market_profile.trend_efficiency_pct)}</td>
                        <td className={returnClass(combination?.total_return_pct)}>{metric(combination?.total_return_pct)}</td>
                        <td>{metric(combination?.max_drawdown_pct)}</td>
                        <td>{combination?.total_trades ?? '—'}</td>
                        <td><span className={`badge ${status === 'completed' ? 'badge-buy' : 'badge-sell'}`}>{status === 'completed' ? 'Complete' : 'Unavailable'}</span></td>
                        <td><button type="button" className="btn btn-secondary wl-analysis-open-button" onClick={() => combination && void openTradeDetails(stock, combination)} disabled={combination?.status !== 'completed' || combination.run_id == null}>View trades</button></td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
