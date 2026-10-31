'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import {
  addStocksToWatchlist,
  createWatchlist,
  getWatchlists,
  getStockDisplayMetadata,
  scanMomentumUniverse,
  type MomentumScanRequest,
  type MomentumScanResponse,
  type MomentumScanResult,
  isEquityScannerResult,
} from '@/lib/api';
import { usePersistentState } from '@/lib/persistence';
import { parseScannerSelection, selectScannerStockIds } from '@/lib/scanner-selection';
import { getStockCompanyName } from '@/lib/stock-display';
import type { Watchlist } from '@/lib/types';
import StockIdentity from './StockIdentity';

type PresetId = 'early' | 'strong' | 'volume';

const PRESETS: Record<PresetId, { label: string; description: string; values: Required<Pick<MomentumScanRequest, 'min_score' | 'min_volume_ratio' | 'min_avg_turnover_cr' | 'min_return_20d' | 'min_return_60d' | 'trend_mode'>> }> = {
  early: {
    label: 'Early movers',
    description: 'Positive acceleration with trend and liquidity confirmation.',
    values: { min_score: 55, min_volume_ratio: 1.2, min_avg_turnover_cr: 1, min_return_20d: 2, min_return_60d: 5, trend_mode: 'aligned' },
  },
  strong: {
    label: 'Strong momentum',
    description: 'Fewer, stronger candidates already showing sustained price strength.',
    values: { min_score: 70, min_volume_ratio: 1.3, min_avg_turnover_cr: 2, min_return_20d: 5, min_return_60d: 12, trend_mode: 'aligned' },
  },
  volume: {
    label: 'Volume surge',
    description: 'Unusual participation first, with a lighter trend gate for fresh moves.',
    values: { min_score: 45, min_volume_ratio: 2, min_avg_turnover_cr: 1, min_return_20d: 0, min_return_60d: -100, trend_mode: 'above_sma50' },
  },
};

const DEFAULTS: MomentumScanRequest = {
  max_results: 200,
  min_price: 20,
  min_avg_turnover_cr: 1,
  min_score: 55,
  min_volume_ratio: 1.2,
  min_return_20d: 2,
  min_return_60d: 5,
  min_delivery_pct: 0,
  trend_mode: 'aligned',
};

function number(value: number | null | undefined, digits = 2, suffix = '') {
  return value == null || Number.isNaN(value) ? '—' : `${value.toFixed(digits)}${suffix}`;
}

function signed(value: number | null | undefined, digits = 2) {
  return value == null || Number.isNaN(value) ? '—' : `${value >= 0 ? '+' : ''}${value.toFixed(digits)}%`;
}

function returnClass(value: number | null | undefined) {
  if (value == null) return '';
  return value >= 0 ? 'momentum-positive' : 'momentum-negative';
}

function formatCr(value: number | null | undefined) {
  return value == null || Number.isNaN(value) ? '—' : `₹${value.toFixed(2)} Cr`;
}

function updateNumber(current: MomentumScanRequest, key: keyof MomentumScanRequest, value: string): MomentumScanRequest {
  const parsed = Number(value);
  return { ...current, [key]: Number.isFinite(parsed) ? parsed : 0 };
}

function MomentumRow({
  result,
  selected,
  onToggle,
}: {
  result: MomentumScanResult;
  selected: boolean;
  onToggle: () => void;
}) {
  return (
    <tr>
      <td><label className="scanner-select-control"><input className="scanner-select-checkbox" type="checkbox" checked={selected} onChange={onToggle} aria-label={`Select ${result.symbol}`} /></label></td>
      <td><strong>{result.rank}</strong></td>
      <td><StockIdentity {...result} /></td>
      <td>{result.sector || '—'}</td>
      <td><span className={`momentum-score ${result.momentum_score >= 70 ? 'is-strong' : ''}`}>{number(result.momentum_score, 1)}</span><small>{result.category}</small></td>
      <td className={returnClass(result.return_20d)}>{signed(result.return_20d)}</td>
      <td className={returnClass(result.return_60d)}>{signed(result.return_60d)}</td>
      <td className={returnClass(result.return_120d)}>{signed(result.return_120d)}</td>
      <td>{number(result.volume_ratio, 2, '×')}</td>
      <td>{formatCr(result.avg_turnover_20d_cr)}</td>
      <td>{number(result.avg_delivery_pct_20d, 1, '%')}</td>
      <td>{signed(result.distance_from_52w_high_pct)}</td>
      <td><span className={result.trend_aligned ? 'momentum-trend-ok' : 'momentum-trend-muted'}>{result.trend_aligned ? 'Aligned' : 'Mixed'}</span></td>
    </tr>
  );
}

export default function MomentumScannerPanel() {
  const [watchlists, setWatchlists] = useState<Watchlist[]>([]);
  const [watchlistId, setWatchlistId] = usePersistentState<number | null>('workspace.momentum.watchlistId', null);
  const [scan, setScan] = usePersistentState<MomentumScanResponse | null>('workspace.momentum.scan', null);
  const metadataAttemptedIds = useRef(new Set<number>());
  const [filters, setFilters] = usePersistentState<MomentumScanRequest>('workspace.momentum.filters', DEFAULTS);
  const [preset, setPreset] = usePersistentState<PresetId>('workspace.momentum.preset', 'early');
  const [selectedIds, setSelectedIds] = usePersistentState<number[]>('workspace.momentum.selectedIds', []);
  const [loading, setLoading] = useState(true);
  const [scanning, setScanning] = useState(false);
  const [adding, setAdding] = useState(false);
  const [creatingWatchlist, setCreatingWatchlist] = useState(false);
  const [newWatchlistName, setNewWatchlistName] = useState('');
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const selectionSpec = useMemo(() => parseScannerSelection(newWatchlistName), [newWatchlistName]);
  const equityResults = useMemo(
    () => (scan?.results ?? []).filter(isEquityScannerResult),
    [scan?.results],
  );

  useEffect(() => {
    const allowed = new Set(equityResults.map((result) => result.stock_id));
    setSelectedIds((current) => {
      const next = current.filter((stockId) => allowed.has(stockId));
      return next.length === current.length ? current : next;
    });
  }, [equityResults, setSelectedIds]);

  const loadWatchlists = async () => {
    const lists = await getWatchlists();
    setWatchlists(lists);
    setWatchlistId((current) => (
      current != null && lists.some((watchlist) => watchlist.id === current)
        ? current
        : lists[0]?.id ?? null
    ));
  };

  useEffect(() => {
    loadWatchlists().catch((requestError) => setError(requestError instanceof Error ? requestError.message : 'Unable to load watchlists'));
      // The first scan is intentionally user-triggered so the page does not
      // start a heavy universe query before the user chooses a preset.
    setLoading(false);
  }, []);

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

  const setPresetAndFilters = (nextPreset: PresetId) => {
    setPreset(nextPreset);
    setFilters((current) => ({ ...current, ...PRESETS[nextPreset].values }));
    setMessage('');
  };

  const handleNewWatchlistNameChange = (value: string) => {
    setNewWatchlistName(value);
    const selection = parseScannerSelection(value);
    if (scan && selection) setSelectedIds(selectScannerStockIds(equityResults, selection));
  };

  const runScan = async () => {
    setScanning(true);
    setError('');
    setMessage('');
    setSelectedIds([]);
    try {
      const nextScan = await scanMomentumUniverse(filters);
      setScan(nextScan);
      const selection = parseScannerSelection(newWatchlistName);
      if (selection) setSelectedIds(selectScannerStockIds(nextScan.results.filter(isEquityScannerResult), selection));
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Unable to run momentum scan');
    } finally {
      setScanning(false);
    }
  };

  const visibleIds = useMemo(() => new Set(equityResults.map((result) => result.stock_id)), [equityResults]);
  const allVisibleSelected = visibleIds.size > 0 && [...visibleIds].every((id) => selectedIds.includes(id));

  const toggleSelected = (stockId: number) => {
    setSelectedIds((current) => {
      return current.includes(stockId)
        ? current.filter((id) => id !== stockId)
        : [...current, stockId];
    });
  };

  const toggleAllVisible = () => {
    setSelectedIds((current) => {
      if (allVisibleSelected) return current.filter((id) => !visibleIds.has(id));
      return [...new Set([...current, ...visibleIds])];
    });
  };

  const addSelected = async () => {
    if (watchlistId == null) {
      setError('Create or select a watchlist before adding stocks.');
      return;
    }
    if (selectedIds.length === 0) {
      setError('Select at least one stock first.');
      return;
    }
    setAdding(true);
    setError('');
    try {
      const result = await addStocksToWatchlist(watchlistId, selectedIds);
      setMessage(`${result.added_count} stock${result.added_count === 1 ? '' : 's'} added to the watchlist${result.capacity_skipped_count ? `; ${result.capacity_skipped_count} skipped because it is full` : ''}.`);
      setSelectedIds([]);
      await loadWatchlists();
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
    const idsToAdd = scan && selectionSpec
      ? selectScannerStockIds(equityResults, selectionSpec)
      : selectedIds;
    if (idsToAdd.length === 0) {
      setError(selectionSpec ? 'This selection does not contain any stocks.' : 'Select stocks first, or end the name with Top N, Bottom N, or All.');
      return;
    }
    setCreatingWatchlist(true);
    setError('');
    try {
      const created = await createWatchlist({
        name,
        description: 'Created from the momentum scanner',
        list_type: 'custom',
        max_stocks: 500,
      });
      const result = await addStocksToWatchlist(created.id, idsToAdd, 'momentum_scanner');
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

  if (loading) return <div><h1 className="page-title">Momentum Scanner</h1><div className="skeleton" style={{ height: 420 }} /></div>;

  return (
    <div className="momentum-page">
      <div className="momentum-hero">
        <div>
          <div className="momentum-eyebrow"><span className="momentum-eyebrow-dot" /> Universe ranking</div>
          <h1 className="page-title">Momentum Scanner</h1>
          <p>Find early movers across active NSE equity stocks using price acceleration, trend alignment, traded value, and volume participation.</p>
        </div>
        <div className="momentum-hero-note">No Nifty, Sensex, or index membership is used.</div>
      </div>

      <section className="card momentum-controls">
        <div className="momentum-preset-row">
          {(Object.entries(PRESETS) as [PresetId, typeof PRESETS[PresetId]][]).map(([id, item]) => (
            <button key={id} type="button" className={`momentum-preset ${preset === id ? 'is-selected' : ''}`} onClick={() => setPresetAndFilters(id)}>
              <strong>{item.label}</strong><small>{item.description}</small>
            </button>
          ))}
        </div>
        <div className="momentum-control-grid">
          <label className="form-label">Minimum score<input className="input" type="number" min="0" max="100" step="1" value={filters.min_score ?? ''} onChange={(event) => setFilters((current) => updateNumber(current, 'min_score', event.target.value))} /></label>
          <label className="form-label">Volume ratio minimum<input className="input" type="number" min="0" step="0.1" value={filters.min_volume_ratio ?? ''} onChange={(event) => setFilters((current) => updateNumber(current, 'min_volume_ratio', event.target.value))} /></label>
          <label className="form-label">Avg turnover minimum (₹ Cr)<input className="input" type="number" min="0" step="0.5" value={filters.min_avg_turnover_cr ?? ''} onChange={(event) => setFilters((current) => updateNumber(current, 'min_avg_turnover_cr', event.target.value))} /></label>
          <label className="form-label">Minimum price (₹)<input className="input" type="number" min="0" step="1" value={filters.min_price ?? ''} onChange={(event) => setFilters((current) => updateNumber(current, 'min_price', event.target.value))} /></label>
          <label className="form-label">Minimum 20-session return (%)<input className="input" type="number" step="1" value={filters.min_return_20d ?? ''} onChange={(event) => setFilters((current) => updateNumber(current, 'min_return_20d', event.target.value))} /></label>
          <label className="form-label">Minimum 60-session return (%)<input className="input" type="number" step="1" value={filters.min_return_60d ?? ''} onChange={(event) => setFilters((current) => updateNumber(current, 'min_return_60d', event.target.value))} /></label>
          <label className="form-label">Minimum avg delivery (%)<input className="input" type="number" min="0" max="100" step="1" value={filters.min_delivery_pct ?? ''} onChange={(event) => setFilters((current) => updateNumber(current, 'min_delivery_pct', event.target.value))} /></label>
          <label className="form-label">Trend gate<select className="input" value={filters.trend_mode} onChange={(event) => setFilters((current) => ({ ...current, trend_mode: event.target.value as MomentumScanRequest['trend_mode'] }))}><option value="aligned">Close &gt; SMA20 &gt; SMA50</option><option value="above_sma50">Close above SMA50</option><option value="any">No trend gate</option></select></label>
        </div>
        <div className="momentum-actions">
          <button type="button" className="btn btn-primary" onClick={runScan} disabled={scanning}>{scanning ? 'Scanning universe…' : 'Run momentum scan'}</button>
          <span className="momentum-action-note">Daily data · up to {filters.max_results} results · latest available session</span>
        </div>
      </section>

      <section className="momentum-formula-grid">
        <div className="card momentum-formula-card"><h2>How the score works</h2><p><strong>45%</strong> price momentum · <strong>30%</strong> trend alignment · <strong>20%</strong> volume and liquidity · <strong>5%</strong> 52-week breakout proximity.</p><p className="momentum-muted">Returns use 20, 60, 120, and 252 trading sessions. Volume ratio is latest volume divided by the previous 20-session average. Turnover is average traded value, not just share count.</p></div>
        <div className="card momentum-formula-card"><h2>Why volume is separate</h2><p>High volume confirms participation, but it does not automatically mean good momentum. The scanner therefore reports both a price/trend score and a volume score, so you can choose early trend candidates or volume-led candidates.</p></div>
      </section>

      {error && <div className="bt-error">{error}</div>}
      {message && <div className="card momentum-message">{message}</div>}

      {scan && (
        <section className="momentum-results">
          <div className="momentum-summary-grid">
            <div className="stat-card"><div className="stat-card-label">{scan.universe_stocks == null ? 'Data-eligible set' : 'NSE company equity universe'}</div><div className="stat-card-value">{(scan.universe_stocks ?? scan.scanned_stocks).toLocaleString('en-IN')}</div><div className="stat-card-sub">{scan.universe_stocks == null ? 'Active symbols with history' : 'ETFs and indexes excluded'}</div></div>
            <div className="stat-card"><div className="stat-card-label">Data eligible</div><div className="stat-card-value">{scan.scanned_stocks.toLocaleString('en-IN')}</div><div className="stat-card-sub">At least 60 daily candles</div></div>
            <div className="stat-card"><div className="stat-card-label">Matches</div><div className="stat-card-value">{scan.matching_stocks.toLocaleString('en-IN')}</div><div className="stat-card-sub">After your thresholds</div></div>
            <div className="stat-card"><div className="stat-card-label">As of</div><div className="stat-card-value momentum-date-value">{scan.as_of_date}</div><div className="stat-card-sub">Latest available candle</div></div>
            <div className="stat-card"><div className="stat-card-label">Selected</div><div className="stat-card-value">{selectedIds.length}</div><div className="stat-card-sub">Ready for watchlist</div></div>
          </div>

          <div className="momentum-results-toolbar">
            <div><h2>Ranked candidates</h2><p>Sorted by the composite score. Review the component columns before adding stocks to auto-trading watchlists.</p></div>
            <div className="momentum-watchlist-actions"><select className="input" value={watchlistId ?? ''} onChange={(event) => setWatchlistId(event.target.value ? Number(event.target.value) : null)}><option value="">Choose watchlist</option>{watchlists.map((watchlist) => <option key={watchlist.id} value={watchlist.id}>{watchlist.name} · {watchlist.stock_count}</option>)}</select><button type="button" className="btn btn-secondary" onClick={addSelected} disabled={adding || creatingWatchlist || selectedIds.length === 0}>{adding ? 'Adding…' : `Add ${selectedIds.length || ''} selected to watchlist`}</button><span className="momentum-watchlist-divider">or</span><input className="input momentum-new-watchlist-input" value={newWatchlistName} onChange={(event) => handleNewWatchlistNameChange(event.target.value)} placeholder="New watchlist name · Top 44 / Bottom 30 / All" maxLength={64} aria-label="New watchlist name" /><button type="button" className="btn btn-primary" onClick={createAndAddSelected} disabled={creatingWatchlist || adding || !newWatchlistName.trim() || (selectedIds.length === 0 && !selectionSpec)}>{creatingWatchlist ? 'Creating…' : 'Create & add'}</button></div>
          </div>

          <div className="card momentum-table-wrap"><table className="data-table momentum-table"><thead><tr><th><label className="scanner-select-control"><input className="scanner-select-checkbox" type="checkbox" checked={allVisibleSelected} onChange={toggleAllVisible} aria-label="Select all visible stocks" /></label></th><th>#</th><th>Stock</th><th title="Latest available Upstox sector metadata">Sector</th><th>Score</th><th>20D</th><th>60D</th><th>120D</th><th>Volume</th><th>Turnover</th><th>Delivery</th><th>52W high</th><th>Trend</th></tr></thead><tbody>{equityResults.map((result) => <MomentumRow key={result.stock_id} result={result} selected={selectedIds.includes(result.stock_id)} onToggle={() => toggleSelected(result.stock_id)} />)}</tbody></table>{equityResults.length === 0 && <div className="empty-state"><div className="empty-state-text">No equity stocks matched these thresholds.</div><div className="empty-state-sub">Try the Volume surge preset or reduce the minimum score.</div></div>}</div>
        </section>
      )}
    </div>
  );
}
