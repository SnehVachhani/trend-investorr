'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  deleteEnhancedMomentumBacktest,
  getEnhancedMomentumBacktest,
  getEnhancedMomentumBacktestJob,
  getEnhancedMomentumBacktests,
  getEnhancedMomentumBacktestPresets,
  getEnhancedMomentumBacktestSetupPresets,
  getFundamentalScoreInputs,
  runEnhancedMomentumBacktest,
  type EnhancedMomentumBacktestRequest,
  type EnhancedMomentumBacktestJob,
  type EnhancedMomentumBacktestStock,
  type EnhancedMomentumBacktestStrategy,
  type EnhancedMomentumBacktestResult,
  type EnhancedMomentumBacktestRun,
  type EnhancedMomentumScanRequest,
  isEquityScannerResult,
} from '@/lib/api';
import { usePersistentState } from '@/lib/persistence';
import StockIdentity from './StockIdentity';
import { assessFundamentals, type FundamentalAssessment } from '@/lib/fundamental-analysis';

const DEFAULT_FILTERS: EnhancedMomentumScanRequest = {
  top_n: null,
  min_price: 20,
  min_avg_turnover_cr: 1,
  min_score: 55,
  min_volume_ratio: 1.2,
  min_return_20d: 0,
  min_return_60d: 0,
  min_history: 260,
  trend_mode: 'aligned',
  market_mode: 'all',
  require_breakout: false,
};

function localDate() {
  const today = new Date();
  return `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;
}

function shiftCalendarMonths(value: string, offset: number) {
  const [year, month, day] = value.split('-').map(Number);
  const index = year * 12 + month - 1 + offset;
  const nextYear = Math.floor(index / 12);
  const nextMonth = ((index % 12) + 12) % 12 + 1;
  const lastDay = new Date(nextYear, nextMonth, 0).getDate();
  return `${nextYear}-${String(nextMonth).padStart(2, '0')}-${String(Math.min(day, lastDay)).padStart(2, '0')}`;
}

function monthSpan(start: string, end: string) {
  const [startYear, startMonth] = start.split('-').map(Number);
  const [endYear, endMonth] = end.split('-').map(Number);
  return Math.max(1, (endYear - startYear) * 12 + endMonth - startMonth);
}

function displayDate(value?: string | null) {
  if (!value) return '—';
  return new Date(`${value.slice(0, 10)}T00:00:00`).toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' });
}

function refreshFrequencyLabel(value?: string) {
  return ({
    '15_days': 'every 15 days',
    monthly: 'monthly',
    quarterly: 'quarterly',
    semiannual: 'semi-annually',
  } as Record<string, string>)[value ?? ''] ?? 'scheduled';
}

function metric(value: number | null | undefined, suffix = '%', digits = 2) {
  if (value == null || Number.isNaN(value)) return '—';
  return `${value >= 0 && suffix === '%' ? '+' : ''}${value.toFixed(digits)}${suffix}`;
}

type NumericFilterKey = Exclude<keyof EnhancedMomentumScanRequest, 'as_of_date' | 'top_n' | 'trend_mode' | 'market_mode' | 'require_breakout'>;

function updateNumber(current: EnhancedMomentumScanRequest, key: NumericFilterKey, value: string) {
  const parsed = Number(value);
  return { ...current, [key]: Number.isFinite(parsed) ? parsed : 0 };
}

function returnColor(value: number | null | undefined) {
  return { color: value == null ? 'var(--text-tertiary)' : value >= 0 ? 'var(--color-profit)' : 'var(--color-loss)' };
}

function currency(value: number | null | undefined) {
  return value == null || Number.isNaN(value)
    ? '—'
    : `₹${value.toLocaleString('en-IN', { maximumFractionDigits: 0 })}`;
}

function levelOutcome(available: boolean, hit: boolean | undefined, row: EnhancedMomentumBacktestResult) {
  if (!available) return 'N/A';
  if (row.outcome === 'capital_too_small') return 'N/A';
  if (hit) return 'Yes';
  if (!row.horizon_complete && !row.target_hit && !row.stop_hit) return 'Unknown';
  return 'No';
}

function ResultTable({ results, months }: { results: EnhancedMomentumBacktestResult[]; months: number }) {
  if (results.length === 0) return <div className="empty-state"><div className="empty-state-text">No stocks matched these filters at the historical signal date.</div><div className="empty-state-sub">Try a lower score threshold or a broader market regime.</div></div>;
  return (
    <div className="card momentum-table-wrap">
      <table className="data-table momentum-table">
        <thead><tr>
          <th>Rank</th><th>Stock</th><th>Score</th><th>Signal date</th><th>Entry date</th><th>Entry ₹</th>
          <th>{months}M close return</th><th>Target ₹</th><th>Stop ₹</th><th>Target hit</th><th>Stop hit</th>
          <th>Exit date</th><th>Net trade return</th><th>Outcome</th>
        </tr></thead>
        <tbody>{results.map((row) => (
          <tr key={row.stock_id}>
            <td>{row.rank}</td>
            <td><StockIdentity className="momentum-stock-open" {...row} /></td>
            <td>{row.enhanced_score.toFixed(1)}</td>
            <td>{displayDate(row.signal_date)}</td>
            <td>{displayDate(row.entry_date)}</td>
            <td>{row.entry_price == null ? '—' : row.entry_price.toFixed(2)}</td>
            <td style={returnColor(row.horizon_return_pct)}>{metric(row.horizon_return_pct)}</td>
            <td>{row.target_price == null ? '—' : row.target_price.toFixed(2)}</td>
            <td>{row.stop_price == null ? '—' : row.stop_price.toFixed(2)}</td>
            <td>{levelOutcome(row.target_price != null, row.target_hit, row)}</td>
            <td>{levelOutcome(row.stop_price != null, row.stop_hit, row)}</td>
            <td>{displayDate(row.exit_date)}</td>
            <td style={returnColor(row.net_trade_return_pct)}>{metric(row.net_trade_return_pct)}</td>
            <td>{row.outcome === 'completed' ? row.exit_reason?.replaceAll('_', ' ') : row.outcome.replaceAll('_', ' ')}</td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  );
}

function comparisonName(strategy: EnhancedMomentumBacktestStrategy) {
  if (strategy.strategy_type === 'scanner') return 'Automatic screen baseline · daily ATR';
  if (strategy.strategy_type === 'buy_and_hold') {
    const selectionLabel = strategy.strategy_name.split(' · ').slice(1).join(' · ');
    return `Next-refresh buy & hold benchmark${selectionLabel ? ` · ${selectionLabel}` : ''}`;
  }
  return strategy.strategy_name;
}

function comparisonDescription(strategy: EnhancedMomentumBacktestStrategy) {
  if (strategy.strategy_type !== 'scanner') return strategy.description;
  const targetAtr = strategy.risk_rules.target_atr ?? '—';
  const stopAtr = strategy.risk_rules.stop_atr ?? '—';
  return `Uses Enhanced Momentum screen matches only; selected Watchlist Analysis filters are not applied to this row. Enters at the next session open and exits at ${targetAtr} ATR target, ${stopAtr} ATR stop, or period end.`;
}

function StrategyTable({
  strategies,
  onViewStocks,
}: {
  strategies: EnhancedMomentumBacktestStrategy[];
  onViewStocks: (strategy: EnhancedMomentumBacktestStrategy) => void;
}) {
  if (strategies.length === 0) return <div className="card momentum-message">No strategy results are available for this replay.</div>;
  return (
    <div className="card momentum-table-wrap">
      <table className="data-table momentum-table">
        <thead><tr><th>Replay method / setup</th><th title="For each stock: net P&amp;L divided by total cash deployed in that stock's trades, then averaged equally across stocks.">Mean per-stock return</th><th>Starting capital</th><th>Net gain</th><th title="Net gain divided by the initial capital pool. Capital slots are reused after trades close.">Portfolio return</th><th>Traded / evaluated stocks</th><th>Trades</th><th>Targets / stops / exit</th><th>Details</th></tr></thead>
        <tbody>{strategies.map((strategy) => (
          <tr key={`${strategy.strategy_type}:${strategy.strategy_id}`}>
            <td><strong>{comparisonName(strategy)}</strong><small className="momentum-muted">{strategy.timeframe.toUpperCase()} · {comparisonDescription(strategy)}</small>{strategy.summary.stocks_skipped ? <small className="momentum-muted">{strategy.summary.stocks_skipped} stocks skipped because data was unavailable or the setup could not be evaluated.</small> : null}</td>
            <td style={returnColor(strategy.summary.average_stock_return_pct)}>{metric(strategy.summary.average_stock_return_pct)}</td>
            <td>{currency(strategy.summary.starting_capital)}</td>
            <td style={returnColor(strategy.summary.net_pnl)}>{currency(strategy.summary.net_pnl)}</td>
            <td style={returnColor(strategy.summary.total_return_pct)}>{metric(strategy.summary.total_return_pct)}</td>
            <td>{strategy.summary.stocks_traded ?? strategy.summary.stock_count} / {strategy.summary.evaluated_stocks ?? strategy.summary.stock_count}</td>
            <td>{strategy.summary.total_trades}</td>
            <td>{strategy.strategy_type === 'buy_and_hold' ? 'Sell at next refresh' : `${strategy.summary.target_hits}/${strategy.summary.target_eligible_trades} · ${strategy.summary.stop_hits} stops`}</td>
            <td><button type="button" className="btn btn-secondary" onClick={() => onViewStocks(strategy)} disabled={strategy.summary.stock_count === 0}>View stocks</button></td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  );
}

function StockResultsTable({
  strategy,
  onViewTrades,
  fundamentalScores,
  fundamentalScoresLoading,
  fundamentalScoreError,
}: {
  strategy: EnhancedMomentumBacktestStrategy;
  onViewTrades: (stock: EnhancedMomentumBacktestStock) => void;
  fundamentalScores: Record<number, FundamentalAssessment>;
  fundamentalScoresLoading: boolean;
  fundamentalScoreError: string;
}) {
  return (
    <div className="momentum-table-wrap">
      {strategy.stocks.length === 0 ? <div className="empty-state">No stocks completed a trade for this filter in the selected period.</div> : (
        <table className="data-table momentum-table">
          <thead><tr><th>Stock</th><th>Fundamental score</th><th>Status</th><th>Starting amount</th><th>Final amount</th><th>Gain</th><th>Return</th><th>Trades</th><th>Wins</th><th>Targets / stops</th><th /></tr></thead>
          <tbody>{strategy.stocks.map((stock) => {
            const assessment = fundamentalScores[stock.stock_id];
            const score = assessment?.score ?? null;
            return (
            <tr key={stock.stock_id}>
              <td><StockIdentity symbol={stock.symbol} name={stock.name} company_name={stock.company_name} /></td>
              <td data-sort-value={score == null ? '' : String(score)} title={fundamentalScoreError || (assessment ? `${assessment.rating} · ${assessment.coverage}% pillar coverage · ${assessment.signalCoverage}% input coverage. Uses latest available fundamentals and is shown for context only.` : 'Latest fundamental analysis score; informational for this historical replay.')}>{fundamentalScoresLoading ? '…' : score == null ? '—' : <><strong>{score}</strong><small>{assessment?.rating} · {assessment?.coverage}% coverage</small></>}</td>
              <td>{(stock.status ?? 'completed').replaceAll('_', ' ')}</td>
              <td>{currency(stock.starting_capital)}</td>
              <td>{currency(stock.final_capital)}</td>
              <td style={returnColor(stock.net_pnl)}>{currency(stock.net_pnl)}</td>
              <td style={returnColor(stock.return_pct)}>{metric(stock.return_pct)}</td>
              <td>{stock.total_trades}</td>
              <td>{stock.winning_trades} · {metric(stock.win_rate_pct)}</td>
              <td>{stock.target_hits} / {stock.stop_hits}</td>
              <td><button type="button" className="btn btn-secondary" onClick={() => onViewTrades(stock)} disabled={stock.trades.length === 0}>View trades</button></td>
            </tr>
          );
          })}</tbody>
        </table>
      )}
    </div>
  );
}

export default function EnhancedMomentumBacktestPanel() {
  const [filters, setFilters] = usePersistentState<EnhancedMomentumScanRequest>('workspace.enhancedMomentum.filters', DEFAULT_FILTERS);
  const [scan] = usePersistentState<{ as_of_date?: string; filters?: EnhancedMomentumScanRequest } | null>('workspace.enhancedMomentum.scan', null);
  const [endDate, setEndDate] = usePersistentState('workspace.enhancedMomentum.backtest.endDate', scan?.as_of_date ?? localDate());
  const [startDate, setStartDate] = usePersistentState('workspace.enhancedMomentum.backtest.startDate', shiftCalendarMonths(endDate, -3));
  const [refreshFrequency, setRefreshFrequency] = usePersistentState<NonNullable<EnhancedMomentumBacktestRequest['refresh_frequency']>>('workspace.enhancedMomentum.backtest.refreshFrequency', 'monthly');
  const [targetAtr, setTargetAtr] = usePersistentState('workspace.enhancedMomentum.backtest.targetAtr', 2);
  const [stopAtr, setStopAtr] = usePersistentState('workspace.enhancedMomentum.backtest.stopAtr', 1);
  const [capital, setCapital] = usePersistentState('workspace.enhancedMomentum.backtest.capitalPerStock', 100000);
  const [selectedPresetIds, setSelectedPresetIds] = usePersistentState<string[]>('workspace.enhancedMomentum.backtest.presetIds', []);
  const [selectedFundamentalSetupIds, setSelectedFundamentalSetupIds] = usePersistentState<string[]>('workspace.enhancedMomentum.backtest.fundamentalSetupIds', []);
  const [includeTopNBuyHold, setIncludeTopNBuyHold] = usePersistentState('workspace.enhancedMomentum.backtest.includeTopNBuyHold', false);
  const [run, setRun] = useState<EnhancedMomentumBacktestRun | null>(null);
  const [latestRunId, setLatestRunId] = usePersistentState<number | null>('workspace.enhancedMomentum.backtest.latestRunId', null);
  const [activeJobId, setActiveJobId] = usePersistentState<string | null>('workspace.enhancedMomentum.backtest.activeJobId', null);
  const [history, setHistory] = useState<EnhancedMomentumBacktestRun[]>([]);
  const [presets, setPresets] = useState<Awaited<ReturnType<typeof getEnhancedMomentumBacktestPresets>>>([]);
  const [fundamentalSetups, setFundamentalSetups] = useState<Awaited<ReturnType<typeof getEnhancedMomentumBacktestSetupPresets>>>([]);
  const [running, setRunning] = useState(false);
  const [backtestJob, setBacktestJob] = useState<EnhancedMomentumBacktestJob | null>(null);
  const [loadingHistory, setLoadingHistory] = useState(true);
  const [loadingSavedRunId, setLoadingSavedRunId] = useState<number | null>(null);
  const [loadingPresets, setLoadingPresets] = useState(true);
  const [loadingFundamentalSetups, setLoadingFundamentalSetups] = useState(true);
  const [error, setError] = useState('');
  const [selectedStrategy, setSelectedStrategy] = useState<EnhancedMomentumBacktestStrategy | null>(null);
  const [selectedStock, setSelectedStock] = useState<EnhancedMomentumBacktestStock | null>(null);
  const replayComparisonRef = useRef<HTMLElement | null>(null);
  const legacyRunMigrationRef = useRef(false);
  const [fundamentalScoreState, setFundamentalScoreState] = useState<{
    key: string;
    scores: Record<number, FundamentalAssessment>;
    error: string;
  }>({ key: '', scores: {}, error: '' });

  const setActiveRun = useCallback((nextRun: EnhancedMomentumBacktestRun | null) => {
    setRun(nextRun);
    setLatestRunId(nextRun?.id ?? null);
  }, [setLatestRunId]);

  const allPresetIds = presets.map((preset) => preset.preset_id);
  const allFundamentalSetupIds = fundamentalSetups.map((setup) => setup.id);
  const allReplayMethodsSelected = allPresetIds.every((presetId) => selectedPresetIds.includes(presetId))
    && allFundamentalSetupIds.every((setupId) => selectedFundamentalSetupIds.includes(setupId))
    && includeTopNBuyHold;
  const toggleAllReplayMethods = () => {
    if (allReplayMethodsSelected) {
      setSelectedPresetIds([]);
      setSelectedFundamentalSetupIds([]);
      setIncludeTopNBuyHold(false);
      return;
    }
    setSelectedPresetIds(allPresetIds);
    setSelectedFundamentalSetupIds(allFundamentalSetupIds);
    setIncludeTopNBuyHold(true);
  };

  useEffect(() => {
    if (legacyRunMigrationRef.current) return;
    legacyRunMigrationRef.current = true;
    try {
      const legacyKey = 'workspace.enhancedMomentum.backtest.latest';
      const legacyValue = window.localStorage.getItem(legacyKey);
      if (legacyValue) {
        let legacyRunId: number | null = null;
        if (latestRunId == null) {
          const parsedRunId = Number((JSON.parse(legacyValue) as { id?: unknown }).id);
          if (Number.isInteger(parsedRunId) && parsedRunId > 0) legacyRunId = parsedRunId;
        }
        // Older versions stored the complete trade history here, which can exceed the browser's storage quota.
        window.localStorage.removeItem(legacyKey);
        if (legacyRunId != null) setLatestRunId(legacyRunId);
      }
    } catch {
      // The saved run remains available from the history table if browser storage is unavailable.
    }
  }, [latestRunId, setLatestRunId]);

  useEffect(() => {
    if (latestRunId == null || run?.id === latestRunId) return;
    let active = true;
    const runId = latestRunId;
    getEnhancedMomentumBacktest(runId)
      .then((saved) => {
        if (active) setRun(saved);
      })
      .catch((requestError: unknown) => {
        if (active) {
          setError(requestError instanceof Error ? requestError.message : 'Unable to load the latest enhanced momentum replay');
        }
      });
    return () => { active = false; };
  }, [latestRunId, run?.id]);

  const refreshHistory = useCallback(async () => {
    try {
      setHistory(await getEnhancedMomentumBacktests());
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Unable to load enhanced momentum backtests');
    } finally {
      setLoadingHistory(false);
    }
  }, []);

  useEffect(() => {
    let active = true;
    getEnhancedMomentumBacktests()
      .then((items) => { if (active) setHistory(items); })
      .catch((requestError: unknown) => {
        if (active) setError(requestError instanceof Error ? requestError.message : 'Unable to load enhanced momentum backtests');
      })
      .finally(() => { if (active) setLoadingHistory(false); });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    let active = true;
    getEnhancedMomentumBacktestPresets()
      .then((items) => { if (active) setPresets(items); })
      .catch((requestError: unknown) => {
        if (active) setError(requestError instanceof Error ? requestError.message : 'Unable to load Watchlist Analysis filters');
      })
      .finally(() => { if (active) setLoadingPresets(false); });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    let active = true;
    getEnhancedMomentumBacktestSetupPresets()
      .then((items) => { if (active) setFundamentalSetups(items); })
      .catch((requestError: unknown) => {
        if (active) setError(requestError instanceof Error ? requestError.message : 'Unable to load Fundamental Setups');
      })
      .finally(() => { if (active) setLoadingFundamentalSetups(false); });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    if (!activeJobId) return;
    let active = true;
    let finished = false;
    let inFlight = false;

    const pollProgress = async () => {
      if (inFlight || finished) return;
      inFlight = true;
      try {
        const nextJob = await getEnhancedMomentumBacktestJob(activeJobId);
        if (!active || finished) return;
        setBacktestJob(nextJob);
        setRunning(nextJob.status === 'queued' || nextJob.status === 'running');
        if (nextJob.status === 'completed' && nextJob.run_id != null) {
          finished = true;
          try {
            const completedRun = await getEnhancedMomentumBacktest(nextJob.run_id);
            if (active) setActiveRun(completedRun);
          } catch (requestError) {
            if (active) setError(requestError instanceof Error ? requestError.message : 'Unable to load the completed replay');
          }
          if (active) await refreshHistory();
          // Keep this polling effect alive until both requests above finish.
          // Clearing the persisted job ID first runs its cleanup and marks
          // `active` false, which used to discard the result and history refresh.
          setActiveJobId(null);
        } else if (nextJob.status === 'failed') {
          finished = true;
          setActiveJobId(null);
          setError(nextJob.error || 'Enhanced momentum replay failed');
        }
      } catch (requestError) {
        if (!active || finished) return;
        const message = requestError instanceof Error ? requestError.message : 'Unable to read historical replay progress';
        if (/\b404\b/.test(message)) {
          finished = true;
          setActiveJobId(null);
          setRunning(false);
          setError('This replay is no longer available to track. Start a new historical replay.');
        }
      } finally {
        inFlight = false;
      }
    };

    void pollProgress();
    const timer = window.setInterval(() => void pollProgress(), 1000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [activeJobId, refreshHistory, setActiveJobId, setActiveRun]);

  useEffect(() => {
    if (!selectedStrategy && !selectedStock) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      if (selectedStock) setSelectedStock(null);
      else setSelectedStrategy(null);
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [selectedStrategy, selectedStock]);

  useEffect(() => {
    if (!selectedStrategy) return;
    const key = `${run?.id ?? 'unsaved'}:${selectedStrategy.strategy_id}`;
    const stocks = selectedStrategy.stocks;
    const stockIds = [...new Set(stocks.map((stock) => stock.stock_id))];
    if (stockIds.length === 0) return;

    let active = true;
    void getFundamentalScoreInputs(stockIds)
      .then((inputs) => {
        if (!active) return;
        setFundamentalScoreState({
          key,
          scores: Object.fromEntries(stocks.map((stock) => [
            stock.stock_id,
            assessFundamentals(inputs[String(stock.stock_id)] ?? null, stock.industry),
          ])),
          error: '',
        });
      })
      .catch((requestError: unknown) => {
        if (!active) return;
        setFundamentalScoreState({
          key,
          scores: Object.fromEntries(stocks.map((stock) => [stock.stock_id, assessFundamentals(null, stock.industry)])),
          error: requestError instanceof Error ? requestError.message : 'Fundamental scores unavailable',
        });
      });
    return () => { active = false; };
  }, [selectedStrategy, run?.id]);

  const fundamentalScoreKey = selectedStrategy ? `${run?.id ?? 'unsaved'}:${selectedStrategy.strategy_id}` : '';
  const hasCurrentFundamentalScores = Boolean(fundamentalScoreKey && fundamentalScoreState.key === fundamentalScoreKey);
  const fundamentalScores = hasCurrentFundamentalScores ? fundamentalScoreState.scores : {};
  const fundamentalScoresLoading = Boolean(selectedStrategy && !hasCurrentFundamentalScores);
  const fundamentalScoreError = hasCurrentFundamentalScores ? fundamentalScoreState.error : '';

  const updateFilter = (key: keyof EnhancedMomentumScanRequest, value: string | boolean) => {
    let next: EnhancedMomentumScanRequest;
    if (key === 'trend_mode') {
      next = { ...filters, trend_mode: value as EnhancedMomentumScanRequest['trend_mode'] };
    } else if (key === 'market_mode') {
      next = { ...filters, market_mode: value as EnhancedMomentumScanRequest['market_mode'] };
    } else if (key === 'require_breakout') {
      next = { ...filters, require_breakout: Boolean(value) };
    } else if (key === 'top_n') {
      const entered = String(value).trim();
      next = {
        ...filters,
        top_n: entered === '' ? null : Math.max(1, Math.min(5000, Math.trunc(Number(entered) || 1))),
      };
    } else {
      next = updateNumber(filters, key as NumericFilterKey, String(value));
    }
    setFilters(next);
  };

  const handleRun = async () => {
    if (!startDate || !endDate || startDate > endDate) {
      setError('From date must be on or before To date.');
      return;
    }
    setRunning(true);
    setError('');
    setSelectedStrategy(null);
    setSelectedStock(null);
    const params: EnhancedMomentumBacktestRequest = {
      start_date: startDate,
      end_date: endDate,
      lookback_months: monthSpan(startDate, endDate),
      refresh_frequency: refreshFrequency,
      mode: 'filter_replay',
      target_atr: Number(targetAtr),
      stop_atr: Number(stopAtr),
      capital_per_stock: Number(capital),
      preset_ids: selectedPresetIds,
      fundamental_setup_ids: selectedFundamentalSetupIds,
      include_top_n_buy_hold: includeTopNBuyHold,
      filters,
    };
    try {
      setBacktestJob(null);
      const started = await runEnhancedMomentumBacktest(params);
      if ('run' in started) {
        // Continue to support an API process that has not yet been restarted
        // onto the progress-job version.
        setActiveRun(started.run);
        setRunning(false);
        setActiveJobId(null);
        await refreshHistory();
      } else {
        setBacktestJob(started);
        setActiveJobId(started.job_id);
      }
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Enhanced momentum backtest failed');
      setRunning(false);
    }
  };

  const openSavedRun = async (runId: number) => {
    setError('');
    setLoadingSavedRunId(runId);
    setSelectedStrategy(null);
    setSelectedStock(null);
    try {
      const saved = await getEnhancedMomentumBacktest(runId);
      setActiveRun(saved);
      window.requestAnimationFrame(() => replayComparisonRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }));
      const {
        preset_ids: savedPresetIds,
        fundamental_setup_ids: savedFundamentalSetupIds,
        ...savedFilters
      } = saved.filters;
      setFilters({ ...savedFilters, as_of_date: undefined });
      setSelectedPresetIds(saved.preset_ids ?? savedPresetIds ?? []);
      setSelectedFundamentalSetupIds(saved.fundamental_setup_ids ?? savedFundamentalSetupIds ?? []);
      setStartDate(saved.signal_cutoff_date);
      setEndDate(saved.end_date);
      const savedScan = saved.scan as { refresh_frequency?: string } | undefined;
      const savedRefresh = saved.config?.refresh_frequency ?? savedScan?.refresh_frequency;
      if (savedRefresh === '15_days' || savedRefresh === 'monthly' || savedRefresh === 'quarterly' || savedRefresh === 'semiannual') {
        setRefreshFrequency(savedRefresh);
      }
      const savedConfig = saved.config as { include_top_n_buy_hold?: boolean } | undefined;
      const selectedBuiltIns = (saved.scan as { selected_builtin_filters?: string[] } | undefined)?.selected_builtin_filters ?? [];
      setIncludeTopNBuyHold(Boolean(savedConfig?.include_top_n_buy_hold || selectedBuiltIns.includes('top_n_buy_hold')));
      const config = saved.config;
      if (typeof config?.target_atr === 'number') setTargetAtr(config.target_atr);
      if (typeof config?.stop_atr === 'number') setStopAtr(config.stop_atr);
      if (typeof config?.capital_per_stock === 'number') setCapital(config.capital_per_stock);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Unable to load the saved run');
    } finally {
      setLoadingSavedRunId(null);
    }
  };

  const removeSavedRun = async (runId: number) => {
    try {
      await deleteEnhancedMomentumBacktest(runId);
      setHistory((current) => current.filter((item) => item.id !== runId));
      if (run?.id === runId) setActiveRun(null);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Unable to delete the saved run');
    }
  };

  const resultRows = (run?.results ?? []).filter(isEquityScannerResult);
  const strategyRows = run?.strategies ?? [];
  const summary = run?.summary;
  const scanDetails = run?.scan as {
    cohort_mode?: string;
    refresh_frequency?: string;
    refresh_dates?: string[];
    capital_slots?: number;
    starting_portfolio_capital?: number;
    capital_allocation_rule?: string;
    buy_hold_exit_rule?: string;
    cohort_sizes?: Array<{ date: string; selected_stocks: number }>;
    cohort_date?: string | null;
    strategy_mode?: string;
    top_n_requested?: number;
    entry_exit_rule?: string;
    snapshot_returned_stocks?: number;
    fresh_snapshot_stocks?: number;
    stale_snapshot_results_excluded?: number;
    selected_builtin_filters?: string[];
    top_n_selection_behavior?: string;
    result_limit_behavior?: string;
    sessions?: Array<{ returned_stocks?: number }>;
  } | undefined;
  const isFixedCohortReplay = scanDetails?.cohort_mode === 'fixed_snapshot';
  const isScheduledRefreshReplay = scanDetails?.cohort_mode === 'scheduled_refresh';
  const isBuyHoldReplay = scanDetails?.strategy_mode === 'buy_and_hold';
  const snapshotReturnedCount = scanDetails?.snapshot_returned_stocks
    ?? scanDetails?.sessions?.[0]?.returned_stocks
    ?? summary?.scanner_snapshot_returned_stocks
    ?? summary?.returned_stocks
    ?? 0;
  const staleSnapshotCount = scanDetails?.stale_snapshot_results_excluded
    ?? Math.max(0, snapshotReturnedCount - (summary?.returned_stocks ?? 0));
  const isReplayActive = running || activeJobId != null || backtestJob?.status === 'queued' || backtestJob?.status === 'running';
  const replayPercent = Math.max(0, Math.min(100, backtestJob?.progress.percent ?? 0));
  const requestSummary = useMemo(() => {
    if (!run) return '';
    const selectedFilters = run.filters;
    const savedScan = run.scan as { cohort_mode?: string; strategy_mode?: string; top_n_requested?: number; refresh_frequency?: string } | undefined;
    const selectedTopN = selectedFilters.top_n ?? (savedScan?.strategy_mode === 'buy_and_hold' ? savedScan.top_n_requested : null);
    const resultLimitLabel = selectedTopN == null ? 'all matching stocks' : `top ${selectedTopN} ranked stocks`;
    const selectedWatchlistFilters = (run.preset_ids ?? []).map((presetId) => presets.find((preset) => preset.preset_id === presetId)?.name ?? presetId);
    const selectedFundamentalSetups = (run.fundamental_setup_ids ?? run.filters.fundamental_setup_ids ?? []).map((setupId) => fundamentalSetups.find((setup) => setup.id === setupId)?.name ?? setupId);
    const selectedBuiltIns = scanDetails?.selected_builtin_filters ?? [];
    const isStandaloneBuyHold = savedScan?.strategy_mode === 'buy_and_hold';
    const replayMethods: string[] = isStandaloneBuyHold ? [] : ['automatic daily ATR screen baseline'];
    if (selectedBuiltIns.includes('top_n_buy_hold') || isStandaloneBuyHold) replayMethods.push('next-refresh buy & hold benchmark');
    const refreshLabel = savedScan?.refresh_frequency ? refreshFrequencyLabel(savedScan.refresh_frequency) : '';
    return `${resultLimitLabel}${refreshLabel ? ` · refresh ${refreshLabel}` : ''} · score ≥ ${selectedFilters.min_score ?? 0} · price ≥ ₹${selectedFilters.min_price ?? 0} · turnover ≥ ₹${selectedFilters.min_avg_turnover_cr ?? 0} Cr · volume ≥ ${selectedFilters.min_volume_ratio ?? 0}× · 20D ≥ ${selectedFilters.min_return_20d ?? -100}% · 60D ≥ ${selectedFilters.min_return_60d ?? -100}% · history ≥ ${selectedFilters.min_history ?? 0} sessions · ${selectedFilters.trend_mode ?? 'any'} trend · ${selectedFilters.market_mode ?? 'all'} market · ${selectedFilters.require_breakout ? 'breakout required' : 'breakout optional'} · Watchlist Analysis filters: ${selectedWatchlistFilters.join(', ') || 'none'} · Fundamental Setups: ${selectedFundamentalSetups.join(', ') || 'none'} · replay methods: ${replayMethods.join(', ')}`;
  }, [fundamentalSetups, presets, run, scanDetails]);

  return (
    <div className="momentum-page enhanced-momentum-page">
      <div className="momentum-hero">
        <div>
          <div className="momentum-eyebrow"><span className="momentum-eyebrow-dot" /> Scheduled momentum cohort replay</div>
          <h1 className="page-title">Enhanced momentum backtest</h1>
          <p>Refresh the ranked stock cohort on a schedule and test each setup with a shared pool of reusable capital.</p>
        </div>
      </div>

      <section className="card momentum-controls">
        <div className="momentum-results-toolbar"><div><h2>Backtest date range and trade rules</h2><p>Choose the replay dates, refresh schedule, and how many of the highest-ranked stocks each refresh may trade.</p></div></div>
        <div className="momentum-control-grid">
          <label className="form-label">From date<input className="input" type="date" max={endDate || localDate()} value={startDate} onChange={(event) => setStartDate(event.target.value)} /></label>
          <label className="form-label">To date<input className="input" type="date" min={startDate} max={localDate()} value={endDate} onChange={(event) => setEndDate(event.target.value)} /></label>
          <label className="form-label">Refresh momentum list<select className="input" value={refreshFrequency} onChange={(event) => setRefreshFrequency(event.target.value as NonNullable<EnhancedMomentumBacktestRequest['refresh_frequency']>)}><option value="15_days">Every 15 days</option><option value="monthly">Monthly</option><option value="quarterly">Quarterly</option><option value="semiannual">Semi-annually</option></select><small>Each refreshed cohort is ranked using data available at that date.</small></label>
          <label className="form-label">Daily ATR baseline target<input className="input" type="number" min="0.1" max="10" step="0.1" value={targetAtr} onChange={(event) => setTargetAtr(Math.max(0.1, Math.min(10, Number(event.target.value) || 0.1)))} /><small>20-day Average True Range (ATR) multiples</small></label>
          <label className="form-label">Daily ATR baseline stop<input className="input" type="number" min="0.1" max="10" step="0.1" value={stopAtr} onChange={(event) => setStopAtr(Math.max(0.1, Math.min(10, Number(event.target.value) || 0.1)))} /><small>20-day Average True Range (ATR) multiples</small></label>
          <label className="form-label">Initial capital per slot<input className="input" type="number" min="1000" step="10000" value={capital} onChange={(event) => setCapital(Math.max(1000, Number(event.target.value) || 1000))} /><small>Each strategy starts with one slot per selected Top N stock.</small></label>
          <label className="form-label">Stocks to trade per refresh (Top N)<input className="input" type="number" min="1" max="5000" step="1" value={filters.top_n ?? ''} onChange={(event) => updateFilter('top_n', event.target.value)} /><small>Enter 10 or 20 to trade only the highest-ranked stocks from each refreshed cohort. Leave empty to trade every matching stock.</small></label>
        </div>
        {!startDate || !endDate || startDate > endDate ? <p className="bt-error">From date must be on or before To date.</p> : <p className="momentum-muted">The first scan runs after the first available session on or after From date. Each new cohort can enter from the following session. Existing trades keep their original setup and exit rules; the slot and its remaining cash become available for a later qualifying trade when that position closes. Buy and hold exits at the next refresh close.</p>}
      </section>

      <section className="card momentum-controls">
        <div className="momentum-results-toolbar"><div><h2>Enhanced scanner filters</h2><p>These are shared with the Enhanced momentum scanner.</p></div></div>
        <div className="momentum-control-grid">
          <label className="form-label">Minimum score<input className="input" type="number" min="0" max="100" value={filters.min_score ?? ''} onChange={(event) => updateFilter('min_score', event.target.value)} /></label>
          <label className="form-label">Minimum price<input className="input" type="number" min="0" value={filters.min_price ?? ''} onChange={(event) => updateFilter('min_price', event.target.value)} /></label>
          <label className="form-label">Turnover (₹ Cr)<input className="input" type="number" min="0" value={filters.min_avg_turnover_cr ?? ''} onChange={(event) => updateFilter('min_avg_turnover_cr', event.target.value)} /></label>
          <label className="form-label">Minimum volume ratio<input className="input" type="number" min="0" step="0.1" value={filters.min_volume_ratio ?? ''} onChange={(event) => updateFilter('min_volume_ratio', event.target.value)} /></label>
          <label className="form-label">Minimum 20D return (%)<input className="input" type="number" value={filters.min_return_20d ?? ''} onChange={(event) => updateFilter('min_return_20d', event.target.value)} /></label>
          <label className="form-label">Minimum 60D return (%)<input className="input" type="number" value={filters.min_return_60d ?? ''} onChange={(event) => updateFilter('min_return_60d', event.target.value)} /></label>
          <label className="form-label">Minimum history<input className="input" type="number" min="60" max="1000" value={filters.min_history ?? ''} onChange={(event) => updateFilter('min_history', event.target.value)} /></label>
          <label className="form-label">Trend mode<select className="input" value={filters.trend_mode ?? 'aligned'} onChange={(event) => updateFilter('trend_mode', event.target.value as NonNullable<EnhancedMomentumScanRequest['trend_mode']>)}><option value="aligned">Aligned</option><option value="above_sma50">Above SMA50</option><option value="any">Any</option></select></label>
          <label className="form-label">Market regime<select className="input" value={filters.market_mode ?? 'all'} onChange={(event) => updateFilter('market_mode', event.target.value as NonNullable<EnhancedMomentumScanRequest['market_mode']>)}><option value="all">All</option><option value="bullish_or_neutral">Bullish or neutral</option><option value="bullish_only">Bullish only</option></select></label>
          <button
            type="button"
            className={`em-filter-choice em-filter-choice-compact${filters.require_breakout ? ' is-selected' : ''}`}
            onClick={() => updateFilter('require_breakout', !filters.require_breakout)}
            aria-pressed={Boolean(filters.require_breakout)}
            title="Only include stocks breaking above their recent 20-day or 55-day high."
          >
            <span className="em-filter-choice-heading">
              <strong>Require 20D/55D breakout</strong>
              <span className="em-filter-choice-state">{filters.require_breakout ? 'On' : 'Off'}</span>
            </span>
            <span className="em-filter-choice-description">Only include stocks clearing a recent high.</span>
          </button>
        </div>
      </section>

      <section className="card momentum-controls">
        <div className="momentum-results-toolbar"><div><h2>Replay comparison setup</h2><p>Every replay includes a daily screen baseline. Select Watchlist Analysis filters and/or standalone Fundamental Setups to test on the same scheduled Enhanced Momentum cohorts; the optional buy &amp; hold benchmark is also available. Each selected method gets its own shared capital pool.</p></div></div>
        <div className="em-backtest-select-all">
          <span>Select every Watchlist Analysis filter, standalone Fundamental Setup, and the buy &amp; hold benchmark at once.</span>
          <button type="button" className="btn btn-secondary" onClick={toggleAllReplayMethods} disabled={loadingPresets || loadingFundamentalSetups}>
            {allReplayMethodsSelected ? 'Clear all selections' : 'Select all methods and filters'}
          </button>
        </div>
        <div className="em-backtest-setup-options" role="group" aria-label="Select backtest methods and filters">
          <div className="em-backtest-setup-group">
            <h3>Replay methods and Watchlist Analysis filters</h3>
            <div className="momentum-control-grid em-backtest-setup-grid">
          <button
            type="button"
            className={`em-filter-choice${includeTopNBuyHold ? ' is-selected' : ''}`}
            onClick={() => setIncludeTopNBuyHold((current) => !current)}
            aria-pressed={includeTopNBuyHold}
            title="Buy each refreshed cohort at the next session open and sell it at the following refresh close. It uses the optional Top N scanner filter above."
          >
            <span className="em-filter-choice-heading">
              <strong>Next-refresh buy &amp; hold benchmark</strong>
              <span className="em-filter-choice-state">{includeTopNBuyHold ? 'Selected' : 'Select'}</span>
            </span>
            <span className="em-filter-choice-description">Buy each refreshed cohort at the next open and sell it at the following refresh.</span>
            <span className="em-filter-choice-meta">Uses the optional Top N scanner filter</span>
          </button>
          {loadingPresets && <div className="momentum-message">Loading Watchlist Analysis filters…</div>}
          {presets.map((preset) => (
            <button
              type="button"
              className={`em-filter-choice${selectedPresetIds.includes(preset.preset_id) ? ' is-selected' : ''}`}
              key={preset.preset_id}
              onClick={() => setSelectedPresetIds((current) => (
                current.includes(preset.preset_id)
                  ? current.filter((presetId) => presetId !== preset.preset_id)
                  : [...current, preset.preset_id]
              ))}
              aria-pressed={selectedPresetIds.includes(preset.preset_id)}
              title={preset.description}
            >
              <span className="em-filter-choice-heading">
                <strong>{preset.name}</strong>
                <span className="em-filter-choice-state">{selectedPresetIds.includes(preset.preset_id) ? 'Selected' : 'Select'}</span>
              </span>
              <span className="em-filter-choice-description">{preset.description}</span>
              <span className="em-filter-choice-meta">{preset.timeframe.toUpperCase()} · {preset.holding_period}</span>
            </button>
          ))}
            </div>
          </div>
          <div className="em-backtest-setup-group">
            <h3>Standalone Fundamental Setups</h3>
            <p className="momentum-muted">These use the Fundamental Setups 5-minute signal rules, restricted to periods when a stock belongs to an Enhanced Momentum cohort. They use the setup defaults: 2R target, 0.5% stop buffer, and 1,500-bar time exit.</p>
            <div className="momentum-control-grid em-backtest-setup-grid">
              {loadingFundamentalSetups && <div className="momentum-message">Loading Fundamental Setups…</div>}
              {fundamentalSetups.map((setup) => (
                <button
                  type="button"
                  className={`em-filter-choice${selectedFundamentalSetupIds.includes(setup.id) ? ' is-selected' : ''}`}
                  key={setup.id}
                  onClick={() => setSelectedFundamentalSetupIds((current) => (
                    current.includes(setup.id)
                      ? current.filter((setupId) => setupId !== setup.id)
                      : [...current, setup.id]
                  ))}
                  aria-pressed={selectedFundamentalSetupIds.includes(setup.id)}
                  title={`${setup.entry_rule} ${setup.exit_rule}`}
                >
                  <span className="em-filter-choice-heading">
                    <strong>{setup.name}</strong>
                    <span className="em-filter-choice-state">{selectedFundamentalSetupIds.includes(setup.id) ? 'Selected' : 'Select'}</span>
                  </span>
                  <span className="em-filter-choice-description">{setup.description}</span>
                  <span className="em-filter-choice-meta">{setup.candle_interval.toUpperCase()} · 2R target · 1,500-bar time exit</span>
                </button>
              ))}
            </div>
            {!loadingFundamentalSetups && fundamentalSetups.length === 0 && <div className="momentum-message">No standalone Fundamental Setups are available.</div>}
          </div>
        </div>
        {!loadingPresets && presets.length === 0 && <div className="momentum-message">No Watchlist Analysis setup filters are available. Buy and hold can still be selected above.</div>}
        <p className="momentum-muted">The scanner builds a fresh cohort on each scheduled refresh. When Top N is set, each cohort keeps its highest-ranked matches; otherwise every fresh match is used. Selected Watchlist Analysis filters and Fundamental Setups can trigger on later qualifying 5-minute candles. The next-refresh buy &amp; hold benchmark buys each cohort at the next session open and sells it at the following refresh close; the final cohort exits at period end.</p>
        <div className="momentum-actions"><button type="button" className="btn btn-primary" onClick={() => void handleRun()} disabled={isReplayActive || !startDate || !endDate || startDate > endDate}>{isReplayActive ? 'Building replay comparison…' : 'Run Replay Comparison'}</button><span className="momentum-action-note">Automatic daily ATR baseline · selected filters run independently · delivery costs applied</span></div>
      </section>

      {error && <div className="bt-error">{error}</div>}

      {(isReplayActive || backtestJob) && (
        <section className="card wl-analysis-progress em-backtest-progress" aria-live="polite">
          <div className="wl-analysis-progress-title">
            <strong>{isReplayActive ? 'Historical replay in progress' : backtestJob?.status === 'completed' ? 'Historical replay complete' : 'Historical replay failed'}</strong>
            <span>{isReplayActive ? backtestJob ? `${replayPercent.toFixed(0)}%` : 'Starting…' : backtestJob?.status === 'completed' ? '100%' : 'Stopped'}</span>
          </div>
          <div
            className={`wl-analysis-progress-track${isReplayActive && !backtestJob ? ' em-backtest-progress-indeterminate' : ''}`}
            role="progressbar"
            aria-label="Enhanced momentum historical replay progress"
            aria-valuemin={backtestJob ? 0 : undefined}
            aria-valuemax={backtestJob ? 100 : undefined}
            aria-valuenow={isReplayActive && !backtestJob ? undefined : backtestJob?.status === 'completed' ? 100 : replayPercent}
          >
            <span style={{ width: `${isReplayActive && !backtestJob ? 28 : backtestJob?.status === 'completed' ? 100 : replayPercent}%` }} />
          </div>
          <div className="wl-analysis-progress-meta">
            <span>{backtestJob ? `${backtestJob.progress.phase}${backtestJob.progress.total ? ` · ${backtestJob.progress.completed}/${backtestJob.progress.total} ${backtestJob.progress.unit}` : ''}` : 'Waiting for the replay worker to report progress'}</span>
            <span>{isReplayActive ? backtestJob ? `${replayPercent.toFixed(0)}%` : 'Starting' : backtestJob?.status === 'completed' ? 'Done' : 'Failed'}</span>
          </div>
          {backtestJob?.progress.detail && <small className="momentum-muted em-backtest-progress-detail">{backtestJob.progress.detail}</small>}
          {backtestJob?.error && <div className="bt-error">{backtestJob.error}</div>}
        </section>
      )}

      {run && summary && (
        <>
          <section className="card momentum-formula-card">
            <h2>Run #{run.id} · {displayDate(run.signal_cutoff_date)} to {displayDate(run.end_date)}</h2>
            <p>{requestSummary}</p>
            {isFixedCohortReplay && !scanDetails?.result_limit_behavior && !scanDetails?.top_n_selection_behavior && <div className="bt-error">This saved replay predates the exchange-time and full-match fixes. Use a newer corrected saved run for the valid cohort results.</div>}
            {isScheduledRefreshReplay ? (
              <>
                <p className="momentum-muted">The scanner refreshed {scanDetails?.refresh_dates?.length ?? summary.scanner_sessions ?? 0} times ({refreshFrequencyLabel(scanDetails?.refresh_frequency)}). Each list was ranked from its own point-in-time data. A cohort becomes eligible for entries on the next session. Trades already open at a refresh continue with their existing target, stop, or hold rule.</p>
                <p className="momentum-muted">Each replay method has {scanDetails?.capital_slots ?? 0} reusable capital slots, with at most one open position per slot. When a position closes, its slot can fund the next qualifying trade from the current cohort; signals seen while all slots are occupied are not filled retroactively. The buy &amp; hold benchmark sells each batch at the next refresh close. Mean per-stock return divides each stock's net gain by the cash deployed in that stock's trades, then averages stocks equally; evaluated stocks with no fills contribute 0%. Portfolio return is net gain divided by the starting capital pool. Slots can be reused for many rounds of trades, so these percentages measure different things. Capital slots and realized trades include delivery charges; the available historical universe uses currently active NSE EQ instruments, so delisted symbols may be missing.</p>
              </>
            ) : isFixedCohortReplay ? (
              <>
                {isBuyHoldReplay ? (
                  <>
                    <p className="momentum-muted">Enhanced Momentum ranked every matching stock on {displayDate(scanDetails?.cohort_date)} and selected the top {scanDetails?.top_n_requested ?? summary.returned_stocks}. Each position buys at the next session open and sells at the last available close on or before {displayDate(run.end_date)}. No Watchlist Analysis setup, ATR stop, or target is applied.</p>
                    <p className="momentum-muted">Portfolio returns include delivery charges and equal capital per selected stock. The available historical universe uses currently active NSE EQ instruments, so delisted symbols may be missing.</p>
                  </>
                ) : (
                  <>
                    <p className="momentum-muted">Enhanced Momentum selected every stock matching the screen settings on {displayDate(scanDetails?.cohort_date)}. The automatic daily ATR baseline enters at the next daily open and uses the {Number(run.config?.target_atr ?? 2)} ATR target, {Number(run.config?.stop_atr ?? 1)} ATR stop, or period end; it does not apply selected Watchlist Analysis entry filters. Selected Watchlist filters are tested separately on those same stocks and can trigger on later qualifying 5-minute candles. The scanner uses exchange-local dates so later-session candles cannot enter an earlier snapshot. If both ATR levels are touched on one daily candle, the stop is assumed first.</p>
                    <p className="momentum-muted">Average return is equal-weighted across this fixed cohort. Stocks whose selected setup never triggers keep their cash uninvested. Stocks without enough setup data remain visible but are excluded from return and capital totals. The available historical universe uses currently active NSE EQ instruments, so delisted symbols may be missing.</p>
                  </>
                )}
              </>
            ) : (
              <>
                <p className="momentum-muted">This saved replay scanned every daily session point-in-time and collected stocks that appeared during the period. The automatic daily ATR baseline enters at the next session’s open; repeated signals are taken after the current trade exits. It exits at {Number(run.config?.target_atr ?? 2)} ATR target, {Number(run.config?.stop_atr ?? 1)} ATR stop, or period end, and does not apply selected Watchlist Analysis entry filters. Watchlist Analysis filters run separately using their existing 5-minute entry rules and setup-specific risk limits. If both ATR levels are touched on one daily candle, the stop is assumed first.</p>
                <p className="momentum-muted">Average return is equal-weighted across all evaluable stocks that matched the scanner during the period, including stocks whose setup never triggered (their cash stays uninvested). Stocks without enough setup data remain visible but are excluded from return and capital totals. The available historical universe uses currently active NSE EQ instruments, so delisted symbols may be missing.</p>
              </>
            )}
          </section>
          <section className="bt-stats-grid">
            {isScheduledRefreshReplay ? (
              <>
                <div className="bt-stat-card"><div className="bt-stat-label">Momentum refreshes</div><div className="bt-stat-value">{scanDetails?.refresh_dates?.length ?? summary.scanner_sessions ?? 0}</div><div className="bt-stat-sub">{refreshFrequencyLabel(scanDetails?.refresh_frequency)}</div></div>
                <div className="bt-stat-card"><div className="bt-stat-label">Capital slots</div><div className="bt-stat-value">{scanDetails?.capital_slots ?? 0}</div><div className="bt-stat-sub">Up to {summary.maximum_concurrent_positions ?? 0} open positions · {summary.unique_scanner_stocks ?? summary.returned_stocks} unique stocks selected</div></div>
              </>
            ) : isFixedCohortReplay ? (
              <>
                <div className="bt-stat-card"><div className="bt-stat-label">Scanner snapshot</div><div className="bt-stat-value">{displayDate(scanDetails?.cohort_date)}</div><div className="bt-stat-sub">{isBuyHoldReplay ? `${scanDetails?.fresh_snapshot_stocks ?? summary.matching_stocks} dated matches · ${summary.returned_stocks} selected` : `${summary.returned_stocks} fresh selected · ${snapshotReturnedCount} returned / ${summary.matching_stocks} matched`}</div>{staleSnapshotCount > 0 && <div className="bt-stat-sub">{staleSnapshotCount} stale rows excluded</div>}</div>
                <div className="bt-stat-card"><div className="bt-stat-label">{isBuyHoldReplay ? 'Top stocks selected' : 'Cohort stocks traded'}</div><div className="bt-stat-value">{isBuyHoldReplay ? summary.returned_stocks : summary.completed_forward_windows}</div><div className="bt-stat-sub">{isBuyHoldReplay ? `of ${scanDetails?.top_n_requested ?? summary.returned_stocks} requested · ${summary.completed_forward_windows} sold` : `of ${summary.returned_stocks} stocks selected at the snapshot`}</div></div>
              </>
            ) : (
              <>
                <div className="bt-stat-card"><div className="bt-stat-label">Daily sessions replayed</div><div className="bt-stat-value">{summary.scanner_sessions ?? 0}</div><div className="bt-stat-sub">{summary.scanner_signal_count ?? summary.entry_candidates} selected signals</div></div>
                <div className="bt-stat-card"><div className="bt-stat-label">Stocks traded</div><div className="bt-stat-value">{summary.completed_forward_windows}</div><div className="bt-stat-sub">{summary.returned_stocks} unique scanner stocks</div></div>
              </>
            )}
            <div className="bt-stat-card"><div className="bt-stat-label">{isBuyHoldReplay ? 'Trades closed' : 'Target hits'}</div><div className="bt-stat-value">{isBuyHoldReplay ? summary.executed_trades : `${summary.target_hits}/${summary.target_eligible_trades}`}</div><div className="bt-stat-sub">{isBuyHoldReplay ? 'One buy and one end-date sale per completed position' : `${metric(summary.target_hit_rate_pct)} · ${summary.stop_hits} stop hits`}</div></div>
            <div className="bt-stat-card"><div className="bt-stat-label">{isBuyHoldReplay ? 'Next-refresh benchmark return' : 'Daily screen baseline return'}</div><div className="bt-stat-value" style={returnColor(summary.aggregate_net_return_pct)}>{metric(summary.aggregate_net_return_pct)}</div><div className="bt-stat-sub">{currency(summary.aggregate_net_pnl)} gain · {currency(summary.total_final_capital)} final value</div></div>
          </section>
          <section className="momentum-results" ref={replayComparisonRef}>
            <div className="momentum-results-toolbar"><div><h2>Replay comparison · Run #{run.id}</h2><p>The automatic daily ATR baseline uses the screen matches with no Watchlist Analysis entry filter. Each named Watchlist Analysis filter appears as its own separate row; selecting several does not combine them. Next-refresh buy &amp; hold appears only when selected. Open stocks to see per-stock gains and trade history.</p></div></div>
            {strategyRows.length > 0 ? <StrategyTable strategies={strategyRows} onViewStocks={setSelectedStrategy} /> : <ResultTable results={resultRows} months={run.lookback_months} />}
          </section>
        </>
      )}

      {selectedStrategy && (
        <div className="bt-comparison-modal-backdrop" role="presentation" onClick={() => setSelectedStrategy(null)}>
          <div className="bt-comparison-modal" style={{ width: 'min(1280px, 100%)' }} role="dialog" aria-modal="true" aria-labelledby="enhanced-backtest-stocks-title" onClick={(event) => event.stopPropagation()}>
            <div className="bt-comparison-modal-header">
              <div>
                <span className="bt-comparison-detail-label">Strategy stocks</span>
                <h2 id="enhanced-backtest-stocks-title" className="bt-comparison-detail-title">{comparisonName(selectedStrategy)} · {selectedStrategy.summary.total_return_pct == null ? '—' : metric(selectedStrategy.summary.total_return_pct)}</h2>
                <p className="bt-comparison-subtitle">{selectedStrategy.summary.stocks_traded ?? selectedStrategy.summary.stock_count} stocks traded from {selectedStrategy.summary.evaluated_stocks ?? selectedStrategy.summary.stock_count} evaluable stocks · {selectedStrategy.summary.total_trades} trades · starting {currency(selectedStrategy.summary.starting_capital)} · gain {currency(selectedStrategy.summary.net_pnl)}</p>
                <p className="momentum-muted">Fundamental score uses the latest available reports and ratios; it is shown for context and does not affect this historical result.</p>
              </div>
              <button type="button" className="btn btn-secondary bt-comparison-close" onClick={() => setSelectedStrategy(null)}>Close</button>
            </div>
            <StockResultsTable
              strategy={selectedStrategy}
              onViewTrades={setSelectedStock}
              fundamentalScores={fundamentalScores}
              fundamentalScoresLoading={fundamentalScoresLoading}
              fundamentalScoreError={fundamentalScoreError}
            />
          </div>
        </div>
      )}

      {selectedStock && selectedStrategy && (
        <div className="bt-comparison-modal-backdrop" style={{ zIndex: 1010 }} role="presentation" onClick={() => setSelectedStock(null)}>
          <div className="bt-comparison-modal" style={{ width: 'min(1180px, 100%)' }} role="dialog" aria-modal="true" aria-labelledby="enhanced-backtest-trades-title" onClick={(event) => event.stopPropagation()}>
            <div className="bt-comparison-modal-header">
              <div>
                <span className="bt-comparison-detail-label">Trade log · {comparisonName(selectedStrategy)}</span>
                <h2 id="enhanced-backtest-trades-title" className="bt-comparison-detail-title"><StockIdentity symbol={selectedStock.symbol} name={selectedStock.name} company_name={selectedStock.company_name} /></h2>
                <p className="bt-comparison-subtitle">Started with {currency(selectedStock.starting_capital)} · final {currency(selectedStock.final_capital)} · gain {currency(selectedStock.net_pnl)} ({metric(selectedStock.return_pct)})</p>
              </div>
              <button type="button" className="btn btn-secondary bt-comparison-close" onClick={() => setSelectedStock(null)}>Close trades</button>
            </div>
            <div className="momentum-table-wrap">
              <table className="data-table momentum-table">
                <thead><tr><th>#</th><th>Signal date</th><th>Entry date</th><th>Entry price</th><th>Target</th><th>Stop</th><th>Exit date</th><th>Exit price</th><th>Result</th><th>Qty</th><th>Charges</th><th>Net gain</th><th>Return</th></tr></thead>
                <tbody>{selectedStock.trades.map((trade, index) => (
                  <tr key={`${trade.entry_date}-${index}`}>
                    <td>{index + 1}</td>
                    <td>{displayDate(trade.signal_date)}</td>
                    <td>{displayDate(trade.entry_date)}</td>
                    <td>{currency(trade.entry_price)}</td>
                    <td>{currency(trade.target_price)}</td>
                    <td>{currency(trade.stop_price)}</td>
                    <td>{displayDate(trade.exit_date)}</td>
                    <td>{currency(trade.exit_price)}</td>
                    <td>{trade.target_hit ? 'Target' : trade.stop_hit ? 'Stop' : trade.exit_reason.replaceAll('_', ' ')}</td>
                    <td>{trade.quantity}</td>
                    <td>{currency(trade.total_charges ?? ((trade.buy_charges ?? 0) + (trade.sell_charges ?? 0)))}</td>
                    <td style={returnColor(trade.net_pnl)}>{currency(trade.net_pnl)}</td>
                    <td style={returnColor(trade.net_trade_return_pct)}>{metric(trade.net_trade_return_pct)}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      <section className="momentum-results">
        <div className="momentum-results-toolbar"><div><h2>Saved enhanced momentum runs</h2><p>Each saved row keeps its screen settings, Watchlist Analysis filters, and Fundamental Setups.</p></div></div>
        {loadingHistory ? <div className="card momentum-message">Loading saved backtests…</div> : history.length === 0 ? <div className="card momentum-message">No enhanced momentum backtests saved yet.</div> : (
          <div className="card momentum-table-wrap"><table className="data-table"><thead><tr><th>Signal start</th><th>Period end</th><th>Stocks</th><th>Watchlist filters</th><th>Fundamental setups</th><th title="Equal-weight average of the per-stock returns, not the portfolio return.">Mean per-stock return</th><th>Target hit rate</th><th>Net return</th><th>Actions</th></tr></thead><tbody>
            {history.map((item) => {
              const presetIds = item.preset_ids ?? item.filters.preset_ids ?? [];
              const presetNames = presetIds.map((presetId) => presets.find((preset) => preset.preset_id === presetId)?.name ?? presetId);
              const setupIds = item.fundamental_setup_ids ?? item.filters.fundamental_setup_ids ?? [];
              const setupNames = setupIds.map((setupId) => fundamentalSetups.find((setup) => setup.id === setupId)?.name ?? setupId);
              return (
                <tr key={item.id}>
                  <td><button className="bt-text-button" type="button" onClick={() => void openSavedRun(item.id)} disabled={loadingSavedRunId != null}>{loadingSavedRunId === item.id ? `Loading #${item.id}…` : `#${item.id} · ${displayDate(item.signal_cutoff_date)}`}</button></td>
                  <td>{displayDate(item.end_date)}</td>
                  <td>{item.summary.unique_scanner_stocks ?? item.summary.returned_stocks}</td>
                  <td title={presetNames.join(', ') || 'No Watchlist Analysis filters selected'}>{presetNames.length ? `${presetNames.length} selected` : 'None'}</td>
                  <td title={setupNames.join(', ') || 'No standalone Fundamental Setups selected'}>{setupNames.length ? `${setupNames.length} selected` : 'None'}</td>
                  <td style={returnColor(item.summary.average_stock_return_pct ?? item.summary.average_horizon_return_pct)}>{metric(item.summary.average_stock_return_pct ?? item.summary.average_horizon_return_pct)}</td>
                  <td>{metric(item.summary.target_hit_rate_pct)} ({item.summary.target_hits}/{item.summary.target_eligible_trades})</td>
                  <td style={returnColor(item.summary.aggregate_net_return_pct)}>{metric(item.summary.aggregate_net_return_pct)}</td>
                  <td><button type="button" className="bt-history-item-delete em-backtest-delete" aria-label={`Delete enhanced momentum backtest ${item.id}`} onClick={() => void removeSavedRun(item.id)}>✕</button></td>
                </tr>
              );
            })}
          </tbody></table></div>
        )}
      </section>
    </div>
  );
}
