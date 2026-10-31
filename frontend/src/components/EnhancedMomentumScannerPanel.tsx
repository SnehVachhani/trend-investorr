'use client';

import Link from 'next/link';
import { useEffect, useMemo, useRef, useState } from 'react';
import {
  addStocksToWatchlist,
  createWatchlist,
  getEnhancedAllTimeHighMarkers,
  getFundamentalScoreInputs,
  getWatchlists,
  getStockDisplayMetadata,
  type EnhancedMomentumScanRequest,
  type EnhancedMomentumScanResponse,
  type EnhancedMomentumScanResult,
  isEquityScannerResult,
} from '@/lib/api';
import {
  DEFAULT_ENHANCED_MOMENTUM_FILTERS,
  ENHANCED_MOMENTUM_SCAN_TIMINGS_KEY,
  ENHANCED_MOMENTUM_TASK_KEY,
  resumeEnhancedMomentumScan,
  startEnhancedMomentumScan,
  type EnhancedMomentumScanTask,
  type EnhancedMomentumScanTiming,
} from '@/lib/enhanced-momentum-task';
import { usePersistentState } from '@/lib/persistence';
import { parseScannerSelection, selectScannerStockIds } from '@/lib/scanner-selection';
import { getStockCompanyName } from '@/lib/stock-display';
import type { Watchlist } from '@/lib/types';
import StockIdentity from './StockIdentity';
import { assessFundamentals, type FundamentalAssessment } from '@/lib/fundamental-analysis';

function number(value: number | null | undefined, digits = 1, suffix = '') {
  return value == null || Number.isNaN(value) ? '—' : `${value.toFixed(digits)}${suffix}`;
}

function signed(value: number | null | undefined, digits = 1) {
  return value == null || Number.isNaN(value) ? '—' : `${value >= 0 ? '+' : ''}${value.toFixed(digits)}%`;
}

function formatSector(value: string | null | undefined): string {
  const sector = value?.trim();
  if (!sector) return '—';
  const characters = Array.from(sector);
  return characters.length <= 30 ? sector : `${characters.slice(0, 29).join('')}…`;
}

function formatMarketCap(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) || value <= 0
    ? '—'
    : `₹${value.toLocaleString('en-IN', { maximumFractionDigits: 0 })} Cr`;
}

function formatDuration(durationMs: number) {
  const totalSeconds = Math.max(1, Math.round(durationMs / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  if (minutes === 0) return `${seconds} sec`;
  return `${minutes} min${seconds ? ` ${seconds} sec` : ''}`;
}

function updateNumber(current: EnhancedMomentumScanRequest, key: keyof EnhancedMomentumScanRequest, value: string) {
  const parsed = Number(value);
  return { ...current, [key]: Number.isFinite(parsed) ? parsed : 0 };
}

type SortKey = 'rank' | 'stock' | 'sector' | 'marketCap' | 'score' | 'fundamental' | 'return10' | 'return20' | 'return60' | 'return120' | 'riskAdjusted' | 'atr' | 'volume' | 'breakout';
type SortDirection = 'asc' | 'desc';

const SORT_DEFAULTS: Record<SortKey, SortDirection> = {
  rank: 'asc', stock: 'asc', sector: 'asc', marketCap: 'desc', score: 'desc', fundamental: 'desc', return10: 'desc', return20: 'desc',
  return60: 'desc', return120: 'desc', riskAdjusted: 'desc', atr: 'asc', volume: 'desc', breakout: 'desc',
};

function sortValue(result: EnhancedMomentumScanResult, key: SortKey, fundamentals: Record<number, FundamentalAssessment>): string | number | null {
  switch (key) {
    case 'rank': return result.rank;
    case 'stock': return getStockCompanyName(result) || result.symbol;
    case 'sector': return result.sector || null;
    case 'marketCap': return result.market_cap_cr;
    case 'score': return result.enhanced_score;
    case 'fundamental': return fundamentals[result.stock_id]?.score ?? null;
    case 'return10': return result.return_10d;
    case 'return20': return result.return_20d;
    case 'return60': return result.return_60d;
    case 'return120': return result.return_120d;
    case 'riskAdjusted': return result.risk_adjusted_momentum_score;
    case 'atr': return result.atr_pct_20d;
    case 'volume': return result.volume_ratio_20d;
    case 'breakout': return result.at_all_time_high ? 3 : result.breakout_55 ? 2 : result.breakout_20 ? 1 : 0;
  }
}

function SortHeader({ label, sortKey, activeKey, direction, onSort }: {
  label: string;
  sortKey: SortKey;
  activeKey: SortKey;
  direction: SortDirection;
  onSort: (key: SortKey) => void;
}) {
  const active = sortKey === activeKey;
  return <th aria-sort={active ? (direction === 'asc' ? 'ascending' : 'descending') : 'none'}><button type="button" className="momentum-sort-button" onClick={() => onSort(sortKey)}>{label}<span aria-hidden="true">{active ? direction === 'asc' ? '↑' : '↓' : '↕'}</span></button></th>;
}

function EnhancedRow({ result, selected, onToggle, selectionLocked, fundamentalScore, fundamentalScoreLoading, fundamentalScoreError }: {
  result: EnhancedMomentumScanResult;
  selected: boolean;
  onToggle: () => void;
  selectionLocked: boolean;
  fundamentalScore: FundamentalAssessment | null;
  fundamentalScoreLoading: boolean;
  fundamentalScoreError: string;
}) {
  return (
    <tr>
      <td><label className="scanner-select-control"><input className="scanner-select-checkbox" type="checkbox" checked={selected} onChange={onToggle} disabled={selectionLocked} aria-label={`Select ${result.symbol}`} /></label></td>
      <td><strong>{result.rank}</strong></td>
      <td><StockIdentity className="momentum-stock-open" {...result} /></td>
      <td className="enhanced-sector-cell" title={result.sector || undefined}>{formatSector(result.sector)}</td>
      <td>{formatMarketCap(result.market_cap_cr)}</td>
      <td><span className={`momentum-score ${result.enhanced_score >= 70 ? 'is-strong' : ''}`}>{number(result.enhanced_score)}</span></td>
      <td><span className={`momentum-score ${fundamentalScore?.score != null && fundamentalScore.score >= 70 ? 'is-strong' : ''}`} title={fundamentalScoreError || (fundamentalScore ? `${fundamentalScore.rating} · ${fundamentalScore.coverage}% pillar coverage · ${fundamentalScore.signalCoverage}% input coverage` : 'Fundamental data not loaded')}>{fundamentalScoreLoading ? '…' : fundamentalScore?.score == null ? '—' : number(fundamentalScore.score, 0)}</span></td>
      <td className={result.return_10d == null ? '' : result.return_10d >= 0 ? 'momentum-positive' : 'momentum-negative'}>{signed(result.return_10d)}</td>
      <td className={result.return_20d == null ? '' : result.return_20d >= 0 ? 'momentum-positive' : 'momentum-negative'}>{signed(result.return_20d)}</td>
      <td className={result.return_60d == null ? '' : result.return_60d >= 0 ? 'momentum-positive' : 'momentum-negative'}>{signed(result.return_60d)}</td>
      <td className={result.return_120d == null ? '' : result.return_120d >= 0 ? 'momentum-positive' : 'momentum-negative'}>{signed(result.return_120d)}</td>
      <td>{number(result.risk_adjusted_momentum_score)}</td>
      <td><span title={`ATR 20D ${number(result.atr_pct_20d, 2, '%')} · 60D ${number(result.atr_pct_60d, 2, '%')} · 120D ${number(result.atr_pct_120d, 2, '%')} · 252D ${number(result.atr_pct_252d, 2, '%')}`}>{number(result.atr_pct_20d, 2, '%')}</span><small>60D {number(result.atr_pct_60d, 2, '%')} · 252D {number(result.atr_pct_252d, 2, '%')}</small></td>
      <td><span title={`Volume ratio 20D ${number(result.volume_ratio_20d, 2, '×')} · 60D ${number(result.volume_ratio_60d, 2, '×')} · 120D ${number(result.volume_ratio_120d, 2, '×')} · 252D ${number(result.volume_ratio_252d, 2, '×')}`}>{number(result.volume_ratio_20d, 2, '×')}</span><small>60D {number(result.volume_ratio_60d, 2, '×')} · 252D {number(result.volume_ratio_252d, 2, '×')}</small></td>
      <td>{result.breakout_55 ? '55D breakout' : result.breakout_20 ? '20D breakout' : '—'}{result.at_all_time_high && <span role="img" aria-label="All-time high" title="The latest daily high is the highest recorded daily high in available history">{' '}📈</span>}</td>
    </tr>
  );
}

export default function EnhancedMomentumScannerPanel() {
  const [filters, setFilters] = usePersistentState<EnhancedMomentumScanRequest>('workspace.enhancedMomentum.filters', DEFAULT_ENHANCED_MOMENTUM_FILTERS);
  const [scan, setScan] = usePersistentState<EnhancedMomentumScanResponse | null>('workspace.enhancedMomentum.scan', null);
  const metadataAttemptedIds = useRef(new Set<number>());
  const [scanTask] = usePersistentState<EnhancedMomentumScanTask | null>(ENHANCED_MOMENTUM_TASK_KEY, null);
  const [savedScanTimings] = usePersistentState<EnhancedMomentumScanTiming[]>(ENHANCED_MOMENTUM_SCAN_TIMINGS_KEY, []);
  const [currentTime, setCurrentTime] = useState(() => Date.now());
  const [watchlists, setWatchlists] = useState<Watchlist[]>([]);
  const [watchlistId, setWatchlistId] = usePersistentState<number | null>('workspace.enhancedMomentum.watchlistId', null);
  const [selectedIds, setSelectedIds] = usePersistentState<number[]>('workspace.enhancedMomentum.selectedIds', []);
  const [adding, setAdding] = useState(false);
  const [creatingWatchlist, setCreatingWatchlist] = useState(false);
  const [newWatchlistName, setNewWatchlistName] = usePersistentState<string>('workspace.enhancedMomentum.newWatchlistName', '');
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [fundamentalScores, setFundamentalScores] = useState<Record<number, FundamentalAssessment>>({});
  const [loadedScoreKey, setLoadedScoreKey] = useState('');
  const [fundamentalScoreError, setFundamentalScoreError] = useState('');
  const [sort, setSort] = useState<{ key: SortKey; direction: SortDirection }>({ key: 'rank', direction: 'asc' });
  const athRefreshStarted = useRef('');
  const tenDayCacheRefreshStarted = useRef('');
  const selectionSpec = useMemo(() => parseScannerSelection(newWatchlistName), [newWatchlistName]);
  const equityResults = useMemo(
    () => (scan?.results ?? []).filter(isEquityScannerResult),
    [scan?.results],
  );
  const automaticSelectedIds = useMemo(() => (
    scan && selectionSpec ? selectScannerStockIds(equityResults, selectionSpec) : null
  ), [scan, equityResults, selectionSpec]);
  const effectiveSelectedIds = automaticSelectedIds ?? selectedIds;
  const scanning = scanTask?.status === 'running';
  const recentScanTimings = (Array.isArray(savedScanTimings) ? savedScanTimings : [])
    .filter((timing) => timing && Number.isFinite(timing.durationMs) && timing.durationMs >= 0)
    .slice(-10);
  const averageScanDurationMs = recentScanTimings.length
    ? recentScanTimings.reduce((total, timing) => total + timing.durationMs, 0) / recentScanTimings.length
    : null;
  const elapsedScanDurationMs = scanning && scanTask ? Math.max(0, currentTime - scanTask.startedAt) : 0;
  const scanNeedsAthRefresh = Boolean(equityResults.length && equityResults.some((result) => typeof result.at_all_time_high !== 'boolean'));
  const scanNeedsTenDayRefresh = Boolean(scan?.results.length && scan.results.some(
    (result) => !Object.prototype.hasOwnProperty.call(result, 'return_10d'),
  ));
  const staleScanKey = scanNeedsTenDayRefresh && scan
    ? `${scan.as_of_date}:${scan.results.map((result) => result.stock_id).join(',')}`
    : '';
  const athStockIds = useMemo(() => equityResults.map((result) => result.stock_id), [equityResults]);
  const athRefreshKey = `${scan?.as_of_date ?? ''}:${athStockIds.join(',')}`;
  const fundamentalScoreStockIds = useMemo(() => equityResults.map((result) => result.stock_id), [equityResults]);
  const fundamentalSectors = useMemo(() => Object.fromEntries(equityResults.map((result) => [result.stock_id, result.sector])), [equityResults]);
  const fundamentalScoreKey = fundamentalScoreStockIds.join(',');

  useEffect(() => {
    if (!scanning) return;
    setCurrentTime(Date.now());
    const timer = window.setInterval(() => setCurrentTime(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [scanning, scanTask?.startedAt]);

  useEffect(() => {
    if (!scan || !scanNeedsTenDayRefresh || scanTask?.status === 'running' || !staleScanKey) return;
    if (tenDayCacheRefreshStarted.current === staleScanKey) return;
    tenDayCacheRefreshStarted.current = staleScanKey;
    setError('');
    setMessage('Updating saved scanner results to include 10D returns…');
    void startEnhancedMomentumScan(filters, { preserveSavedScan: true })
      .then((nextScan) => {
        const selection = parseScannerSelection(newWatchlistName);
        if (selection) setSelectedIds(selectScannerStockIds(nextScan.results.filter(isEquityScannerResult), selection));
        setMessage('Saved scanner results updated with 10D returns.');
      })
      .catch((requestError: unknown) => setError(requestError instanceof Error ? requestError.message : 'Unable to refresh saved scanner results'));
  }, [scan, scanNeedsTenDayRefresh, scanTask?.status, staleScanKey, filters, newWatchlistName, setSelectedIds]);

  useEffect(() => {
    const allowed = new Set(equityResults.map((result) => result.stock_id));
    setSelectedIds((current) => {
      const next = current.filter((stockId) => allowed.has(stockId));
      return next.length === current.length ? current : next;
    });
  }, [equityResults, setSelectedIds]);

  useEffect(() => {
    if (!fundamentalScoreKey) return;
    let active = true;
    void getFundamentalScoreInputs(fundamentalScoreStockIds)
      .then((inputs) => {
        if (!active) return;
        setFundamentalScores(Object.fromEntries(fundamentalScoreStockIds.map((id) => [id, assessFundamentals(inputs[String(id)] ?? null, fundamentalSectors[id])])));
        setFundamentalScoreError('');
        setLoadedScoreKey(fundamentalScoreKey);
      })
      .catch((requestError: unknown) => {
        if (!active) return;
        setFundamentalScores(Object.fromEntries(fundamentalScoreStockIds.map((id) => [id, assessFundamentals(null)])));
        setFundamentalScoreError(requestError instanceof Error ? requestError.message : 'Fundamental scores unavailable');
        setLoadedScoreKey(fundamentalScoreKey);
      });
    return () => { active = false; };
  }, [fundamentalScoreStockIds, fundamentalSectors, fundamentalScoreKey]);

  const sortResults = (key: SortKey) => setSort((current) => ({
    key,
    direction: current.key === key ? (current.direction === 'asc' ? 'desc' : 'asc') : SORT_DEFAULTS[key],
  }));
  const sortedResults = useMemo(() => [...equityResults].sort((left, right) => {
    const a = sortValue(left, sort.key, fundamentalScores);
    const b = sortValue(right, sort.key, fundamentalScores);
    if (a == null || a === '') return b == null || b === '' ? 0 : 1;
    if (b == null || b === '') return -1;
    const comparison = typeof a === 'number' && typeof b === 'number'
      ? a - b
      : String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: 'base' });
    return sort.direction === 'asc' ? comparison : -comparison;
  }), [equityResults, fundamentalScores, sort]);
  const fundamentalScoreLoading = Boolean(fundamentalScoreKey && loadedScoreKey !== fundamentalScoreKey);

  useEffect(() => {
    if (!scan) return;
    const missingMetadata = equityResults
      .filter((result) => (!getStockCompanyName(result) || !result.sector) && !metadataAttemptedIds.current.has(result.stock_id))
      .map((result) => ({ symbol: result.symbol, stock_id: result.stock_id }));
    if (missingMetadata.length === 0) return;
    for (const stock of missingMetadata) metadataAttemptedIds.current.add(stock.stock_id);

    let active = true;
    void getStockDisplayMetadata(missingMetadata)
      .then((metadata) => {
        if (!active || (Object.keys(metadata.companyNames).length === 0 && Object.keys(metadata.sectorsByStockId).length === 0)) return;
        setScan((current) => current ? {
          ...current,
          results: current.results.map((result) => ({
            ...result,
            ...(metadata.companyNames[result.symbol] ? { company_name: metadata.companyNames[result.symbol] } : {}),
            ...(metadata.sectorsByStockId[String(result.stock_id)] ? { sector: metadata.sectorsByStockId[String(result.stock_id)] } : {}),
          })),
        } : current);
      })
      .catch(() => {});
    return () => { active = false; };
  }, [scan, setScan, equityResults]);

  useEffect(() => {
    getWatchlists()
      .then((items) => {
        setWatchlists(items);
        setWatchlistId((current) => (
          current != null && items.some((watchlist) => watchlist.id === current)
            ? current
            : items[0]?.id ?? null
        ));
      })
      .catch((requestError) => setError(requestError instanceof Error ? requestError.message : 'Unable to load watchlists'));
  }, [setWatchlistId]);

  useEffect(() => {
    if (!scan || !scanNeedsAthRefresh || athRefreshStarted.current === athRefreshKey) return;
    athRefreshStarted.current = athRefreshKey;
    setError('');
    setMessage('Checking the saved candidates for all-time highs…');
    void getEnhancedAllTimeHighMarkers(athStockIds, scan.as_of_date)
      .then((markers) => {
        setScan((current) => {
          if (!current || current.as_of_date !== scan.as_of_date) return current;
          const currentKey = `${current.as_of_date}:${current.results.map((result) => result.stock_id).join(',')}`;
          if (currentKey !== athRefreshKey) return current;
          return {
            ...current,
            results: current.results.map((result) => ({
              ...result,
              at_all_time_high: markers[String(result.stock_id)] ?? false,
            })),
          };
        });
        setMessage('All-time-high markers are up to date.');
      })
      .catch((requestError: unknown) => setError(requestError instanceof Error ? requestError.message : 'Unable to load all-time-high markers'));
  }, [scan, scanNeedsAthRefresh, athStockIds, athRefreshKey, setScan]);

  useEffect(() => {
    if (!scanTask || scanTask.status !== 'running') return;
    const activeRequest = resumeEnhancedMomentumScan(scanTask);
    if (!activeRequest) return;
    void activeRequest
      .then((nextScan) => {
        const selection = parseScannerSelection(newWatchlistName);
        if (selection) setSelectedIds(selectScannerStockIds(nextScan.results.filter(isEquityScannerResult), selection));
      })
      .catch((requestError: unknown) => setError(requestError instanceof Error ? requestError.message : 'Unable to run enhanced momentum scan'));
  }, [scanTask, newWatchlistName, setSelectedIds]);

  const visibleIds = useMemo(() => new Set(equityResults.map((result) => result.stock_id)), [equityResults]);
  const allVisibleSelected = visibleIds.size > 0 && [...visibleIds].every((id) => effectiveSelectedIds.includes(id));

  const toggleAllVisible = () => {
    if (selectionSpec) return;
    setSelectedIds((current) => {
      if (allVisibleSelected) return current.filter((id) => !visibleIds.has(id));
      return [...new Set([...current, ...visibleIds])];
    });
  };

  const runScan = () => {
    setError('');
    setMessage('');
    setSelectedIds([]);
    void startEnhancedMomentumScan(filters)
      .then((nextScan) => {
        const selection = parseScannerSelection(newWatchlistName);
        if (selection) setSelectedIds(selectScannerStockIds(nextScan.results.filter(isEquityScannerResult), selection));
      })
      .catch((requestError: unknown) => setError(requestError instanceof Error ? requestError.message : 'Unable to run enhanced momentum scan'));
  };

  const toggleSelected = (stockId: number) => {
    if (selectionSpec) return;
    setSelectedIds((current) => {
      return current.includes(stockId)
        ? current.filter((id) => id !== stockId)
        : [...current, stockId];
    });
  };

  const addSelected = async () => {
    if (watchlistId == null) {
      setError('Create or select a watchlist before adding stocks.');
      return;
    }
    if (effectiveSelectedIds.length === 0) {
      setError('Select at least one stock first.');
      return;
    }
    setAdding(true);
    setError('');
    try {
      const result = await addStocksToWatchlist(watchlistId, effectiveSelectedIds, 'enhanced_momentum_scanner');
      setMessage(`${result.added_count} stock${result.added_count === 1 ? '' : 's'} added to the watchlist.`);
      setSelectedIds([]);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Unable to add stocks to watchlist');
    } finally {
      setAdding(false);
    }
  };

  const createAndAddSelected = async () => {
    const name = newWatchlistName.trim();
    if (!name) {
      setError('Enter a name for the new watchlist.');
      return;
    }
    const idsToAdd = effectiveSelectedIds;
    if (idsToAdd.length === 0) {
      setError(selectionSpec ? 'This selection does not contain any stocks.' : 'Select stocks first, or end the name with Top N, Bottom N, or All.');
      return;
    }
    setCreatingWatchlist(true);
    setError('');
    try {
      const created = await createWatchlist({
        name,
        description: 'Created from the enhanced momentum scanner',
        list_type: 'custom',
        max_stocks: 500,
      });
      const result = await addStocksToWatchlist(created.id, idsToAdd, 'enhanced_momentum_scanner');
      const lists = await getWatchlists();
      setWatchlists(lists);
      setWatchlistId(created.id);
      setNewWatchlistName('');
      setSelectedIds([]);
      setMessage(`${created.name} created with ${result.added_count} selected stock${result.added_count === 1 ? '' : 's'}.`);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Unable to create the watchlist');
    } finally {
      setCreatingWatchlist(false);
    }
  };

  const market = scan?.market_context;
  return (
    <div className="momentum-page enhanced-momentum-page">
      <div className="momentum-hero">
        <div>
          <div className="momentum-eyebrow"><span className="momentum-eyebrow-dot" /> Market-aware ranking</div>
          <h1 className="page-title">Enhanced swing momentum</h1>
          <p>Ranks daily swing candidates using market-relative strength, volatility-adjusted momentum, breakout quality, and Nifty/Sensex regime context.</p>
        </div>
        <div className="momentum-hero-note">Sector context is reserved for a future point-in-time data layer.</div>
      </div>

      <section className="card momentum-controls">
        <div className="momentum-control-grid">
          <label className="form-label">Minimum score<input className="input" type="number" min="0" max="100" value={filters.min_score ?? ''} onChange={(event) => setFilters((current) => updateNumber(current, 'min_score', event.target.value))} /></label>
          <label className="form-label">Minimum price<input className="input" type="number" min="0" value={filters.min_price ?? ''} onChange={(event) => setFilters((current) => updateNumber(current, 'min_price', event.target.value))} /></label>
          <label className="form-label">Turnover (₹ Cr)<input className="input" type="number" min="0" value={filters.min_avg_turnover_cr ?? ''} onChange={(event) => setFilters((current) => updateNumber(current, 'min_avg_turnover_cr', event.target.value))} /></label>
          <label className="form-label">Minimum history<input className="input" type="number" min="60" value={filters.min_history ?? ''} onChange={(event) => setFilters((current) => updateNumber(current, 'min_history', event.target.value))} /></label>
          <label className="form-label">Market regime<select className="input" value={filters.market_mode} onChange={(event) => setFilters((current) => ({ ...current, market_mode: event.target.value as EnhancedMomentumScanRequest['market_mode'] }))}><option value="all">All regimes</option><option value="bullish_or_neutral">Bullish or neutral</option><option value="bullish_only">Bullish only</option></select></label>
          <label className="form-label">Trend mode<select className="input" value={filters.trend_mode} onChange={(event) => setFilters((current) => ({ ...current, trend_mode: event.target.value as EnhancedMomentumScanRequest['trend_mode'] }))}><option value="aligned">Aligned</option><option value="above_sma50">Above SMA50</option><option value="any">Any</option></select></label>
          <label className="form-label enhanced-check"><input type="checkbox" checked={filters.require_breakout} onChange={(event) => setFilters((current) => ({ ...current, require_breakout: event.target.checked }))} /> Require 20D/55D breakout</label>
        </div>
        <div className="momentum-actions"><button type="button" className="btn btn-primary" onClick={runScan} disabled={scanning}>{scanning ? 'Scanning…' : 'Run enhanced scan'}</button><span className="momentum-action-note momentum-scan-timing" role="status">{averageScanDurationMs == null ? 'Expected wait: building estimate (0/10 scans)' : `Expected wait: about ${formatDuration(averageScanDurationMs)}`}<small>{averageScanDurationMs == null ? 'The first successful scan starts the estimate.' : `Average of the last ${recentScanTimings.length} successful scan${recentScanTimings.length === 1 ? '' : 's'}.`}</small></span>{scan && <Link href="/backtest?section=enhanced-momentum" className="btn btn-secondary">Backtest these scanner filters</Link>}</div>
      </section>

      {(error || (scanTask?.status === 'error' && scanTask.error)) && <div className="bt-error">{error || scanTask?.error}</div>}
      {scanning && <div className="card momentum-message">Enhanced scan is running · elapsed {formatDuration(elapsedScanDurationMs)}. {averageScanDurationMs == null ? 'This run is being timed to build your estimate.' : elapsedScanDurationMs < averageScanDurationMs ? `About ${formatDuration(averageScanDurationMs - elapsedScanDurationMs)} remaining based on recent scans.` : 'This scan has passed the recent average and is still running.'} You can move between pages; the result will appear automatically when it finishes.</div>}
      {message && <div className="card momentum-message">{message}</div>}

      {scan && (
        <>
          <section className="momentum-summary-grid">
            <div className="stat-card"><div className="stat-card-label">Market regime</div><div className="stat-card-value">{market?.regime ?? '—'}</div><div className="stat-card-sub">Score {number(market?.regime_score)}</div></div>
            <div className="stat-card"><div className="stat-card-label">Nifty 60D</div><div className="stat-card-value">{signed(market?.nifty.return_60d)}</div><div className="stat-card-sub">{market?.nifty.trend ?? 'Unavailable'}</div></div>
            <div className="stat-card"><div className="stat-card-label">Sensex 60D</div><div className="stat-card-value">{signed(market?.sensex.return_60d)}</div><div className="stat-card-sub">{market?.sensex.trend ?? 'Unavailable'}</div></div>
            <div className="stat-card"><div className="stat-card-label">Candidates</div><div className="stat-card-value">{scan.returned_stocks}</div><div className="stat-card-sub">{scan.history_eligible_stocks} history-eligible</div></div>
          </section>

          <section className="momentum-formula-grid">
            <div className="card momentum-formula-card"><h2>How the score works</h2><p><strong>45%</strong> base momentum · <strong>25%</strong> relative strength versus Nifty · <strong>20%</strong> multi-horizon risk-adjusted momentum · <strong>10%</strong> breakout quality with multi-horizon volume participation.</p><p className="momentum-muted">Score inputs use 20/60/120/252 sessions; the 10D return is shown as an additional reference. Market regime is shown separately and does not add to this score.</p></div>
            <div className="card momentum-formula-card"><h2>Data notes</h2><p>{market?.available ? 'Benchmark history is available for this scan.' : 'Benchmark history is unavailable or too short; market-relative fields are omitted and the score is renormalized.'}</p>{market?.warnings?.map((warning) => <p className="momentum-muted" key={warning}>{warning}</p>)}</div>
          </section>

          <section className="momentum-results">
            <div className="momentum-results-toolbar"><div><h2>Enhanced candidates</h2><p>Click a header to sort; click a stock name to view its chart and fundamentals.</p></div><div className="momentum-watchlist-actions"><select className="input" value={watchlistId ?? ''} onChange={(event) => setWatchlistId(event.target.value ? Number(event.target.value) : null)}><option value="">Choose watchlist</option>{watchlists.map((watchlist) => <option key={watchlist.id} value={watchlist.id}>{watchlist.name} · {watchlist.stock_count}</option>)}</select><button type="button" className="btn btn-secondary" onClick={addSelected} disabled={adding || creatingWatchlist || effectiveSelectedIds.length === 0}>{adding ? 'Adding…' : `Add ${effectiveSelectedIds.length || ''} selected`}</button><span className="momentum-watchlist-divider">or</span><input className="input momentum-new-watchlist-input" value={newWatchlistName} onChange={(event) => setNewWatchlistName(event.target.value)} placeholder="New watchlist · Top 44 / Bottom 30 / All" maxLength={64} aria-label="New watchlist name" /><button type="button" className="btn btn-primary" onClick={createAndAddSelected} disabled={creatingWatchlist || adding || !newWatchlistName.trim() || effectiveSelectedIds.length === 0}>{creatingWatchlist ? 'Creating…' : 'Create & add'}</button></div></div>
            <div className="card momentum-table-wrap"><table className="data-table momentum-table"><thead><tr><th><label className="scanner-select-control"><input className="scanner-select-checkbox" type="checkbox" checked={allVisibleSelected} onChange={toggleAllVisible} disabled={Boolean(selectionSpec)} aria-label="Select all visible stocks" /></label></th><SortHeader label="#" sortKey="rank" activeKey={sort.key} direction={sort.direction} onSort={sortResults} /><SortHeader label="Stock" sortKey="stock" activeKey={sort.key} direction={sort.direction} onSort={sortResults} /><SortHeader label="Sector" sortKey="sector" activeKey={sort.key} direction={sort.direction} onSort={sortResults} /><SortHeader label="Market cap" sortKey="marketCap" activeKey={sort.key} direction={sort.direction} onSort={sortResults} /><SortHeader label="Score" sortKey="score" activeKey={sort.key} direction={sort.direction} onSort={sortResults} /><SortHeader label="Fundamental" sortKey="fundamental" activeKey={sort.key} direction={sort.direction} onSort={sortResults} /><SortHeader label="10D return" sortKey="return10" activeKey={sort.key} direction={sort.direction} onSort={sortResults} /><SortHeader label="20D return" sortKey="return20" activeKey={sort.key} direction={sort.direction} onSort={sortResults} /><SortHeader label="60D return" sortKey="return60" activeKey={sort.key} direction={sort.direction} onSort={sortResults} /><SortHeader label="120D return" sortKey="return120" activeKey={sort.key} direction={sort.direction} onSort={sortResults} /><SortHeader label="Risk-adjusted" sortKey="riskAdjusted" activeKey={sort.key} direction={sort.direction} onSort={sortResults} /><SortHeader label="ATR 20/60/252" sortKey="atr" activeKey={sort.key} direction={sort.direction} onSort={sortResults} /><SortHeader label="Volume 20/60/252" sortKey="volume" activeKey={sort.key} direction={sort.direction} onSort={sortResults} /><SortHeader label="Breakout / ATH" sortKey="breakout" activeKey={sort.key} direction={sort.direction} onSort={sortResults} /></tr></thead><tbody>{sortedResults.map((result) => <EnhancedRow key={result.stock_id} result={result} selected={effectiveSelectedIds.includes(result.stock_id)} onToggle={() => toggleSelected(result.stock_id)} selectionLocked={Boolean(selectionSpec)} fundamentalScore={loadedScoreKey === fundamentalScoreKey ? fundamentalScores[result.stock_id] ?? null : null} fundamentalScoreLoading={fundamentalScoreLoading} fundamentalScoreError={loadedScoreKey === fundamentalScoreKey ? fundamentalScoreError : ''} />)}</tbody></table>{equityResults.length === 0 && <div className="empty-state"><div className="empty-state-text">No equity candidates matched these thresholds.</div><div className="empty-state-sub">Try lowering the score or selecting all market regimes.</div></div>}</div>
          </section>
        </>
      )}
    </div>
  );
}
