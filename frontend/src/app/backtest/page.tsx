'use client';

import { Suspense, useState, useEffect, useRef, useCallback } from 'react';
import { useSearchParams } from 'next/navigation';
import type { Algorithm, BacktestHistoryItem, BacktestRun, BacktestSection, BacktestTrade, Stock } from '@/lib/types';
import FilterBacktestPanel from '@/components/FilterBacktestPanel';
import FundamentalQueryBacktestPanel from '@/components/FundamentalQueryBacktestPanel';
import WatchlistBacktestPanel from '@/components/WatchlistBacktestPanel';
import EnhancedMomentumBacktestPanel from '@/components/EnhancedMomentumBacktestPanel';
import StockIdentity from '@/components/StockIdentity';
import {
  getAlgorithms,
  runBacktest,
  getBacktestHistory,
  getBacktestResult,
  deleteBacktest,
  searchStocks,
} from '@/lib/api';
import { deleteFilterBacktest } from '@/lib/filter-api';
import { usePersistentState } from '@/lib/persistence';
import { formatStockSymbol, getStockCompanyName } from '@/lib/stock-display';

// ── Algo display names ──────────────────────────────────────
const ALGO_LABELS: Record<string, string> = {
  connors_rsi_pullback: 'Connors RSI Pullback',
  rayner_dip_buyer: 'Rayner Dip Buyer',
  swing_volume_profile_pullback: 'Bullish Swing Volume-Profile Pullback',
  scanner_confluence: 'Scanner Confluence',
};

// ── Format helpers ──────────────────────────────────────────
function fmt(n: number | null | undefined, decimals = 2): string {
  if (n == null) return '—';
  return n.toFixed(decimals);
}

function fmtCurrency(n: number | null | undefined): string {
  if (n == null) return '—';
  return '₹' + n.toLocaleString('en-IN', { maximumFractionDigits: 0 });
}

function fmtDate(d: string | null | undefined): string {
  if (!d) return '—';
  return new Date(d).toLocaleDateString('en-IN', {
    day: '2-digit',
    month: 'short',
    year: '2-digit',
  });
}

function exitReasonLabel(reason: string): string {
  const map: Record<string, string> = {
    sma5_exit: 'SMA5 ↑',
    rsi_recovery: 'RSI ↑',
    gap_stop: 'Gap Stop',
    gap_target: 'Gap Target',
    target_2r: '2R Target',
    stop_loss: 'Stop Loss',
    time_stop: 'Time Stop',
    backtest_end: 'End',
  };
  return map[reason] || reason;
}

// ═══════════════════════════════════════════════════════════════
// COMPONENT
// ═══════════════════════════════════════════════════════════════

function BacktestPageContent({ backtestSection }: { backtestSection: Exclude<BacktestSection, 'watchlist'> }) {
  // ── State ──────────────────────────────────────────────
  const storagePrefix = `workspace.backtest.${backtestSection}`;
  const [algorithms, setAlgorithms] = useState<Algorithm[]>([]);
  const [history, setHistory] = useState<BacktestHistoryItem[]>([]);
  const [activeRun, setActiveRun] = usePersistentState<BacktestRun | null>(`${storagePrefix}.activeRun`, null);
  const [mode, setMode] = useState<'form' | 'processing' | 'results'>('form');
  const [historyOpen, setHistoryOpen] = usePersistentState(`${storagePrefix}.historyOpen`, true);
  const [activeHistoryKey, setActiveHistoryKey] = usePersistentState<string | null>(`${storagePrefix}.activeHistoryKey`, null);
  const [selectedFilterRunId, setSelectedFilterRunId] = usePersistentState<number | null>(`${storagePrefix}.selectedFilterRunId`, null);
  const [newRunKey, setNewRunKey] = useState(0);
  const [error, setError] = useState('');

  // Form state
  const [selectedAlgo, setSelectedAlgo] = usePersistentState(`${storagePrefix}.selectedAlgo`, '');
  const [selectedSymbol, setSelectedSymbol] = usePersistentState(`${storagePrefix}.selectedSymbol`, '');
  const [selectedStockName, setSelectedStockName] = usePersistentState(`${storagePrefix}.selectedStockName`, '');
  const [selectedStockSector, setSelectedStockSector] = usePersistentState(`${storagePrefix}.selectedStockSector`, '');
  const [startDate, setStartDate] = usePersistentState(`${storagePrefix}.startDate`, '');
  const [endDate, setEndDate] = usePersistentState(`${storagePrefix}.endDate`, '');
  const [capital, setCapital] = usePersistentState(`${storagePrefix}.capital`, '1000000');

  // Stock search
  const [stockQuery, setStockQuery] = useState('');
  const [stockResults, setStockResults] = useState<Stock[]>([]);
  const [showStockDropdown, setShowStockDropdown] = useState(false);
  const searchTimeout = useRef<NodeJS.Timeout | null>(null);

  // Chart ref
  const chartRef = useRef<HTMLDivElement>(null);

  // ── Load history ───────────────────────────────────────
  const loadHistory = useCallback(async (section: BacktestSection) => {
    try {
      const runs = await getBacktestHistory(section);
      setHistory(runs);
    } catch {
      /* silent */
    }
  }, []);

  // ── Load initial data ──────────────────────────────────
  useEffect(() => {
    getAlgorithms()
      .then((algos) => {
        // Only show algos that support backtesting (daily candle strategies)
        const backtestable = algos.filter((a) =>
          ['connors_rsi_pullback', 'rayner_dip_buyer', 'swing_volume_profile_pullback'].includes(a.name)
        );
        setAlgorithms(backtestable);
        if (backtestable.length > 0) setSelectedAlgo((current) => current || backtestable[0].name);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    let cancelled = false;
    getBacktestHistory(backtestSection)
      .then((runs) => {
        if (!cancelled) setHistory(runs);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [backtestSection]);

  // ── Stock search ───────────────────────────────────────
  const handleStockSearch = (q: string) => {
    setStockQuery(q);
    setSelectedSymbol('');
    setSelectedStockName('');
    setSelectedStockSector('');

    if (searchTimeout.current) clearTimeout(searchTimeout.current);
    if (q.length < 2) {
      setStockResults([]);
      setShowStockDropdown(false);
      return;
    }

    searchTimeout.current = setTimeout(async () => {
      try {
        const results = await searchStocks(q, 10);
        setStockResults(results);
        setShowStockDropdown(true);
      } catch {
        setStockResults([]);
      }
    }, 300);
  };

  const selectStock = (stock: Stock) => {
    setSelectedSymbol(stock.symbol);
    setSelectedStockName(getStockCompanyName(stock) || '');
    setSelectedStockSector(stock.sector || '');
    setStockQuery(formatStockSymbol(stock.symbol));
    setShowStockDropdown(false);
  };

  // ── Run backtest ───────────────────────────────────────
  const handleRunBacktest = async () => {
    if (!selectedAlgo || !selectedSymbol) return;

    setActiveRun(null);
    setMode('processing');
    setError('');

    try {
      const { run_id } = await runBacktest({
        algorithm: selectedAlgo,
        symbol: selectedSymbol,
        start_date: startDate || undefined,
        end_date: endDate || undefined,
        initial_capital: parseFloat(capital) || 1_000_000,
      });

      // Fetch full result
      const result = await getBacktestResult(run_id);
      setActiveRun(result);
      setActiveHistoryKey(`algorithm-${run_id}`);
      setMode('results');

      // Refresh history
      await loadHistory('algorithms');
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Backtest failed');
      setMode('form');
    }
  };

  // ── Load a historical run ──────────────────────────────
  const loadRun = async (run: BacktestHistoryItem) => {
    setActiveHistoryKey(`${run.backtest_type}-${run.id}`);
    if (run.backtest_type === 'filter') {
      setSelectedFilterRunId(run.id);
      setActiveRun(null);
      setMode('form');
      setError('');
      return;
    }

    setSelectedFilterRunId(null);
    setActiveRun(null);
    setMode('processing');
    setError('');
    try {
      const result = await getBacktestResult(run.id);
      setActiveRun(result);
      setSelectedAlgo(result.algorithm_name);
      setMode('results');
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load run');
      setMode('form');
    }
  };

  // ── Delete a run ───────────────────────────────────────
  const handleDelete = async (run: BacktestHistoryItem, e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      if (run.backtest_type === 'filter') {
        await deleteFilterBacktest(run.id);
      } else {
        await deleteBacktest(run.id);
      }
      const historyKey = `${run.backtest_type}-${run.id}`;
      setHistory((prev) => prev.filter((item) => `${item.backtest_type}-${item.id}` !== historyKey));
      if (activeHistoryKey === historyKey) {
        setActiveHistoryKey(null);
        setSelectedFilterRunId(null);
        setActiveRun(null);
        setMode('form');
      }
    } catch {
      /* silent */
    }
  };

  const startNewBacktest = () => {
    setActiveHistoryKey(null);
    setSelectedFilterRunId(null);
    setActiveRun(null);
    setMode('form');
    setError('');
    setNewRunKey((key) => key + 1);
  };

  const handleFilterRun = async (runId: number) => {
    setActiveHistoryKey(`filter-${runId}`);
    setSelectedFilterRunId(runId);
    await loadHistory('filters');
  };

  // ── Equity chart (simple canvas) ───────────────────────
  useEffect(() => {
    const displayMode = activeRun && mode === 'form' ? 'results' : mode;
    if (displayMode !== 'results' || !activeRun?.equity_curve?.length || !chartRef.current) return;

    const container = chartRef.current;
    // Clear previous
    container.innerHTML = '';

    const canvas = document.createElement('canvas');
    canvas.width = container.clientWidth;
    canvas.height = container.clientHeight;
    container.appendChild(canvas);

    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    const data = activeRun.equity_curve;
    const values = data.map((p) => p.value);
    const minVal = Math.min(...values) * 0.998;
    const maxVal = Math.max(...values) * 1.002;
    const range = maxVal - minVal || 1;
    const initialCap = activeRun.initial_capital;

    const w = canvas.width;
    const h = canvas.height;
    const pad = { top: 20, right: 16, bottom: 30, left: 72 };
    const plotW = w - pad.left - pad.right;
    const plotH = h - pad.top - pad.bottom;

    // Background
    ctx.fillStyle = '#0d0d15';
    ctx.fillRect(0, 0, w, h);

    // Grid lines
    ctx.strokeStyle = '#1a1a2e';
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const y = pad.top + (plotH * i) / 4;
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(w - pad.right, y);
      ctx.stroke();

      // Y-axis label
      const val = maxVal - (range * i) / 4;
      ctx.fillStyle = '#5c5c7a';
      ctx.font = '11px JetBrains Mono, monospace';
      ctx.textAlign = 'right';
      ctx.fillText('₹' + (val / 100000).toFixed(1) + 'L', pad.left - 8, y + 4);
    }

    // Initial capital line
    const capY = pad.top + plotH - ((initialCap - minVal) / range) * plotH;
    ctx.strokeStyle = 'rgba(99, 102, 241, 0.3)';
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(pad.left, capY);
    ctx.lineTo(w - pad.right, capY);
    ctx.stroke();
    ctx.setLineDash([]);

    // Equity line
    ctx.beginPath();
    for (let i = 0; i < data.length; i++) {
      const x = pad.left + (i / (data.length - 1)) * plotW;
      const y = pad.top + plotH - ((data[i].value - minVal) / range) * plotH;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }

    const finalVal = values[values.length - 1];
    const lineColor = finalVal >= initialCap ? '#00c853' : '#ff1744';
    ctx.strokeStyle = lineColor;
    ctx.lineWidth = 2;
    ctx.stroke();

    // Gradient fill
    const lastX = pad.left + plotW;

    ctx.lineTo(lastX, pad.top + plotH);
    ctx.lineTo(pad.left, pad.top + plotH);
    ctx.closePath();
    const grad = ctx.createLinearGradient(0, 0, 0, h);
    grad.addColorStop(0, lineColor === '#00c853' ? 'rgba(0,200,83,0.15)' : 'rgba(255,23,68,0.15)');
    grad.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.fillStyle = grad;
    ctx.fill();

    // X-axis dates
    const dateStep = Math.max(1, Math.floor(data.length / 5));
    ctx.fillStyle = '#5c5c7a';
    ctx.font = '10px JetBrains Mono, monospace';
    ctx.textAlign = 'center';
    for (let i = 0; i < data.length; i += dateStep) {
      const x = pad.left + (i / (data.length - 1)) * plotW;
      ctx.fillText(data[i].date.substring(0, 7), x, h - 8);
    }
  }, [mode, activeRun]);

  const displayMode = activeRun && mode === 'form' ? 'results' : mode;
  const hasSwingProfile = activeRun?.algorithm_name === 'swing_volume_profile_pullback';

  // ═════════════════════════════════════════════════════════
  // RENDER
  // ═════════════════════════════════════════════════════════

  return (
    <div className="bt-layout">
        {/* ── Main Panel ── */}
      <div className="bt-main">
        <header className="bt-section-header">
          <h1 className="bt-section-title">
            {backtestSection === 'algorithms' ? 'Algorithm backtesting' : 'Filter combination backtesting'}
          </h1>
        </header>
        {backtestSection === 'filters' ? (
          <FilterBacktestPanel
            key={`filter-panel-${newRunKey}`}
            onRun={handleFilterRun}
            onBatchRun={() => loadHistory('filters')}
            loadRunId={selectedFilterRunId}
          />
        ) : (
          <div className="bt-workspace-grid">
            <aside className="bt-algorithm-sidebar" aria-label="Algorithms">
              <div className="bt-filter-sidebar-header">
                <span className="bt-filter-sidebar-title">Algorithms</span>
                <span className="bt-filter-sidebar-count">{algorithms.length}</span>
              </div>
              <div className="bt-algorithm-list">
                {algorithms.map((algorithm) => (
                  <button
                    key={algorithm.name}
                    type="button"
                    className={`bt-algorithm-item ${selectedAlgo === algorithm.name ? 'is-selected' : ''}`}
                    onClick={() => {
                      setSelectedAlgo(algorithm.name);
                      setActiveRun(null);
                      setActiveHistoryKey(null);
                      setMode('form');
                      setError('');
                    }}
                  >
                    <strong>{algorithm.display_name || ALGO_LABELS[algorithm.name] || algorithm.name}</strong>
                    <span>{algorithm.strategy_type}</span>
                    <small>{algorithm.description}</small>
                  </button>
                ))}
              </div>
            </aside>
            <div className="bt-workspace-content">
        {error && <div className="bt-error">{error}</div>}

        {/* MODE: Form */}
        {displayMode === 'form' && (
          <div className="bt-form-container">
            <div className="bt-form">
              {/* Stock search */}
              <div className="bt-field">
                <label className="bt-field-label">Stock</label>
                <div className="bt-stock-search">
                  <input
                    id="bt-stock"
                    className="bt-field-input"
                    placeholder="Search stocks... (e.g. RELIANCE)"
                    value={stockQuery}
                    onChange={(e) => handleStockSearch(e.target.value)}
                    onFocus={() => stockResults.length > 0 && setShowStockDropdown(true)}
                    onBlur={() => setTimeout(() => setShowStockDropdown(false), 200)}
                    autoComplete="off"
                  />
                  {selectedSymbol && (
                    <div style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: '12px', color: 'var(--text-secondary)', marginTop: '4px' }}>
                      Selected: <StockIdentity symbol={selectedSymbol} name={selectedStockName} sector={selectedStockSector} />
                      {' · Sector: '}{selectedStockSector || '—'}
                    </div>
                  )}
                  {showStockDropdown && stockResults.length > 0 && (
                    <div className="bt-stock-results">
                      {stockResults.map((s) => (
                        <div
                          key={s.id}
                          className="bt-stock-option"
                        >
                          <div>
                            <StockIdentity className="bt-stock-option-identity" symbol={s.symbol} name={s.name} company_name={s.company_name} sector={s.sector} close={s.latest_price ?? s.last_price ?? null} market_cap_cr={s.market_cap_cr} as_of_date={s.price_date} />
                            <small className="bt-stock-option-name" style={{ display: 'block' }}>Sector: {s.sector || '—'}</small>
                          </div>
                          {s.latest_price ? (
                            <span style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: '12px', fontWeight: 600, color: 'var(--text-primary)' }}>
                              ₹{s.latest_price.toLocaleString('en-IN', { maximumFractionDigits: 2 })}
                            </span>
                          ) : (
                            <span style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>No data</span>
                          )}
                          <button type="button" className="bt-stock-option-select" onMouseDown={(event) => event.preventDefault()} onClick={() => selectStock(s)}>Use stock</button>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </div>

              {/* Date range */}
              <div className="bt-date-row">
                <div className="bt-field">
                  <label className="bt-field-label" htmlFor="bt-start">Start Date (optional)</label>
                  <input
                    id="bt-start"
                    type="date"
                    className="bt-field-input"
                    value={startDate}
                    onChange={(e) => setStartDate(e.target.value)}
                  />
                </div>
                <div className="bt-field">
                  <label className="bt-field-label" htmlFor="bt-end">End Date (optional)</label>
                  <input
                    id="bt-end"
                    type="date"
                    className="bt-field-input"
                    value={endDate}
                    onChange={(e) => setEndDate(e.target.value)}
                  />
                </div>
              </div>

              {/* Capital */}
              <div className="bt-field">
                <label className="bt-field-label" htmlFor="bt-capital">Initial Capital (₹)</label>
                <input
                  id="bt-capital"
                  type="number"
                  className="bt-field-input"
                  value={capital}
                  onChange={(e) => setCapital(e.target.value)}
                  placeholder="1000000"
                />
              </div>

              <button
                className="bt-run-btn"
                onClick={handleRunBacktest}
                disabled={!selectedAlgo || !selectedSymbol}
              >
                🚀 Run Backtest
              </button>
            </div>
          </div>
        )}

        {/* MODE: Processing */}
        {displayMode === 'processing' && (
          <div className="bt-processing">
            <div className="bt-processing-spinner" />
            <div className="bt-processing-text">Running Backtest...</div>
            <div className="bt-processing-sub">
              Computing indicators and simulating trades
            </div>
          </div>
        )}

        {/* MODE: Results */}
        {displayMode === 'results' && activeRun && (
          <div>
            {/* Header */}
            <div className="bt-results-header">
              <h1 className="bt-results-title">
                {ALGO_LABELS[activeRun.algorithm_name] || activeRun.algorithm_name}{' '}
                — <StockIdentity className="bt-results-stock-identity" symbol={activeRun.stock_symbol} name={activeRun.stock_name} company_name={activeRun.company_name} sector={activeRun.sector} />
              </h1>
              <p className="bt-results-meta-item">Symbol: {activeRun.stock_symbol.replace('-EQ', '')} · Sector: {activeRun.sector || '—'}</p>
              <div className="bt-results-meta">
                <span className="bt-results-meta-item">
                  📅 <strong>{fmtDate(activeRun.start_date)}</strong> → <strong>{fmtDate(activeRun.end_date)}</strong>
                </span>
                <span className="bt-results-meta-item">
                  💰 Capital: <strong>{fmtCurrency(activeRun.initial_capital)}</strong>
                </span>
                <span className="bt-results-meta-item">
                  📊 <strong>{activeRun.total_trades}</strong> trades
                </span>
              </div>
            </div>

            {/* Primary Stats */}
            <div className="bt-stats-grid">
              <div className="bt-stat-card">
                <div className="bt-stat-label">Total Return</div>
                <div
                  className="bt-stat-value"
                  style={{ color: (activeRun.total_return_pct ?? 0) >= 0 ? 'var(--color-profit)' : 'var(--color-loss)' }}
                >
                  {(activeRun.total_return_pct ?? 0) >= 0 ? '+' : ''}{fmt(activeRun.total_return_pct)}%
                </div>
                <div className="bt-stat-sub">
                  {fmtCurrency(activeRun.initial_capital)} → {fmtCurrency(activeRun.final_capital)}
                </div>
              </div>
              <div className="bt-stat-card">
                <div className="bt-stat-label">Win Rate</div>
                <div
                  className="bt-stat-value"
                  style={{ color: (activeRun.win_rate_pct ?? 0) >= 50 ? 'var(--color-profit)' : 'var(--color-warning)' }}
                >
                  {fmt(activeRun.win_rate_pct, 1)}%
                </div>
                <div className="bt-stat-sub">
                  {activeRun.winning_trades}W / {activeRun.losing_trades}L
                </div>
              </div>
              <div className="bt-stat-card">
                <div className="bt-stat-label">Max Drawdown</div>
                <div className="bt-stat-value" style={{ color: 'var(--color-loss)' }}>
                  -{fmt(activeRun.max_drawdown_pct)}%
                </div>
                <div className="bt-stat-sub">Worst peak-to-trough</div>
              </div>
              <div className="bt-stat-card">
                <div className="bt-stat-label">Sharpe Ratio</div>
                <div
                  className="bt-stat-value"
                  style={{ color: (activeRun.sharpe_ratio ?? 0) >= 1 ? 'var(--color-profit)' : 'var(--text-primary)' }}
                >
                  {fmt(activeRun.sharpe_ratio, 3)}
                </div>
                <div className="bt-stat-sub">Risk-adjusted return</div>
              </div>
            </div>

            {/* Secondary Stats */}
            <div className="bt-secondary-stats">
              <div className="bt-secondary-stat">
                <div className="bt-secondary-stat-label">CAGR</div>
                <div className="bt-secondary-stat-value">{fmt(activeRun.cagr_pct)}%</div>
              </div>
              <div className="bt-secondary-stat">
                <div className="bt-secondary-stat-label">Profit Factor</div>
                <div className="bt-secondary-stat-value">{fmt(activeRun.profit_factor, 2)}</div>
              </div>
              <div className="bt-secondary-stat">
                <div className="bt-secondary-stat-label">Avg Win</div>
                <div className="bt-secondary-stat-value" style={{ color: 'var(--color-profit)' }}>
                  +{fmt(activeRun.avg_win_pct)}%
                </div>
              </div>
              <div className="bt-secondary-stat">
                <div className="bt-secondary-stat-label">Avg Loss</div>
                <div className="bt-secondary-stat-value" style={{ color: 'var(--color-loss)' }}>
                  {fmt(activeRun.avg_loss_pct)}%
                </div>
              </div>
              <div className="bt-secondary-stat">
                <div className="bt-secondary-stat-label">Avg Hold</div>
                <div className="bt-secondary-stat-value">{fmt(activeRun.avg_hold_days, 1)}d</div>
              </div>
              <div className="bt-secondary-stat">
                <div className="bt-secondary-stat-label">Final Capital</div>
                <div className="bt-secondary-stat-value">{fmtCurrency(activeRun.final_capital)}</div>
              </div>
              <div className="bt-secondary-stat">
                <div className="bt-secondary-stat-label">Trading Charges</div>
                <div className="bt-secondary-stat-value">{fmtCurrency(activeRun.total_charges ?? 0)}</div>
              </div>
            </div>

            {/* Equity Curve */}
            <div className="bt-chart-section">
              <div className="section-header">
                <span className="section-title">Equity Curve</span>
              </div>
              <div className="bt-chart-container" ref={chartRef} />
            </div>

            {/* Trade Log */}
            {activeRun.trades && activeRun.trades.length > 0 && (
              <div className="bt-trades-section">
                <div className="bt-trades-header">
                  <span className="bt-trades-title">Trade Log</span>
                  <span className="bt-trades-count">{activeRun.trades.length} trades</span>
                </div>
                <div className="card" style={{ padding: 0, overflow: 'auto', maxHeight: '480px' }}>
                  <table className="data-table">
                    <thead>
                      <tr>
                        <th>#</th>
                        <th>Entry</th>
                        <th>Exit</th>
                        <th>Entry ₹</th>
                        <th>Exit ₹</th>
                        {hasSwingProfile && <><th>Swing low</th><th>Swing high</th><th>POC</th><th>Value-area low</th><th>Value-area high</th><th>Stop</th><th>Target</th></>}
                        <th>Qty</th>
                        <th>Charges</th>
                        <th>P&L</th>
                        <th>P&L %</th>
                        <th>Hold</th>
                        <th>Reason</th>
                      </tr>
                    </thead>
                    <tbody>
                      {activeRun.trades.map((t: BacktestTrade, i: number) => (
                        <tr
                          key={t.id || i}
                          className={t.pnl > 0 ? 'bt-trade-row-win' : 'bt-trade-row-loss'}
                        >
                          <td style={{ color: 'var(--text-tertiary)' }}>{i + 1}</td>
                          <td>{fmtDate(t.entry_date)}</td>
                          <td>{fmtDate(t.exit_date)}</td>
                          <td style={{ fontFamily: 'JetBrains Mono, monospace' }}>
                            {fmt(t.entry_price)}
                          </td>
                          <td style={{ fontFamily: 'JetBrains Mono, monospace' }}>
                            {fmt(t.exit_price)}
                          </td>
                          {hasSwingProfile && <><td>{fmt(Number(t.metadata?.swing_low))}</td><td>{fmt(Number(t.metadata?.swing_high))}</td><td>{fmt(Number(t.metadata?.poc))}</td><td>{fmt(Number(t.metadata?.value_area_low))}</td><td>{fmt(Number(t.metadata?.value_area_high))}</td><td>{fmt(Number(t.metadata?.stop_price))}</td><td>{fmt(Number(t.metadata?.target_price))}</td></>}
                          <td>{t.quantity}</td>
                          <td style={{ fontFamily: 'JetBrains Mono, monospace' }}>
                            {t.metadata?.total_charges != null ? fmtCurrency(Number(t.metadata.total_charges)) : '—'}
                          </td>
                          <td
                            style={{
                              fontFamily: 'JetBrains Mono, monospace',
                              fontWeight: 600,
                              color: t.pnl >= 0 ? 'var(--color-profit)' : 'var(--color-loss)',
                            }}
                          >
                            {t.pnl >= 0 ? '+' : ''}₹{Math.abs(t.pnl).toLocaleString('en-IN', { maximumFractionDigits: 0 })}
                          </td>
                          <td
                            style={{
                              fontFamily: 'JetBrains Mono, monospace',
                              fontWeight: 600,
                              color: t.pnl_pct >= 0 ? 'var(--color-profit)' : 'var(--color-loss)',
                            }}
                          >
                            {t.pnl_pct >= 0 ? '+' : ''}{fmt(t.pnl_pct)}%
                          </td>
                          <td>{t.hold_days}d</td>
                          <td>
                            <span className={`badge ${t.pnl >= 0 ? 'badge-buy' : 'badge-sell'}`}>
                              {exitReasonLabel(t.exit_reason)}
                            </span>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </div>
        )}
            </div>
          </div>
        )}
      </div>

      {/* ── History Sidebar ── */}
      <div className={`bt-history-shell ${historyOpen ? 'is-open' : 'is-collapsed'}`}>
        <aside id="backtest-history-drawer" className="bt-history">
          <div className="bt-history-header">
            <div>
              <span className="bt-history-title">History</span>
              <span className="bt-history-context">
                {backtestSection === 'algorithms' ? 'Algorithms' : 'Filter combinations'}
              </span>
            </div>
            <div className="bt-history-actions">
              <button className="bt-new-btn" onClick={startNewBacktest}>
                + New
              </button>
              <button
                className="bt-history-hide-btn"
                type="button"
                aria-label="Hide backtest history"
                title="Hide history"
                onClick={() => setHistoryOpen(false)}
              >
                →
              </button>
            </div>
          </div>
          <div className="bt-history-list">
            {history.length === 0 && (
              <div className="empty-state" style={{ padding: '32px 16px' }}>
                <div className="empty-state-icon">🧪</div>
                <div className="empty-state-text">No backtests yet</div>
                <div className="empty-state-sub">
                  Run your first {backtestSection === 'algorithms' ? 'algorithm' : 'filter'} backtest
                </div>
              </div>
            )}
            {history.map((run) => {
              const historyKey = `${run.backtest_type}-${run.id}`;
              const label = run.backtest_type === 'algorithm'
                ? ALGO_LABELS[run.display_name] || run.display_name
                : run.display_name;
              return (
                <div
                  key={historyKey}
                  className={`bt-history-item ${activeHistoryKey === historyKey ? 'active' : ''}`}
                  onClick={() => loadRun(run)}
                >
                  <div className="bt-history-item-algo">{label}</div>
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                    <div>
                      <StockIdentity className="bt-history-stock-identity" symbol={run.stock_symbol} name={run.stock_name} company_name={run.company_name} sector={run.sector} />
                      <small style={{ display: 'block', marginTop: 3, color: 'var(--text-tertiary)' }}>Sector: {run.sector || '—'}</small>
                    </div>
                    <button
                      className="bt-history-item-delete"
                      title="Delete"
                      onClick={(e) => handleDelete(run, e)}
                    >
                      ✕
                    </button>
                  </div>
                  <div className="bt-history-item-meta">
                    <span className="bt-history-item-date">
                      {run.backtest_type === 'algorithm' ? 'Algorithm' : 'Filter'} · {fmtDate(run.created_at)}
                    </span>
                    <span
                      className="bt-history-item-return"
                      style={{
                        color:
                          run.status === 'failed'
                            ? 'var(--text-tertiary)'
                            : (run.total_return_pct ?? 0) >= 0
                            ? 'var(--color-profit)'
                            : 'var(--color-loss)',
                      }}
                    >
                      {run.status === 'failed'
                        ? 'Failed'
                        : run.total_return_pct != null
                        ? `${run.total_return_pct >= 0 ? '+' : ''}${fmt(run.total_return_pct)}%`
                        : '—'}
                    </span>
                  </div>
                </div>
              );
            })}
          </div>
        </aside>
        {!historyOpen && (
          <button
            className="bt-history-toggle"
            type="button"
            aria-controls="backtest-history-drawer"
            aria-expanded={historyOpen}
            onClick={() => setHistoryOpen(true)}
          >
            ← <span>History</span>
          </button>
        )}
      </div>
    </div>
  );
}

function BacktestPageRoute() {
  const searchParams = useSearchParams();
  const requestedSection = searchParams.get('section');
  if (requestedSection === 'watchlist') return <WatchlistBacktestPanel />;
  if (requestedSection === 'fundamentals') return <FundamentalQueryBacktestPanel />;
  if (requestedSection === 'enhanced-momentum') return <EnhancedMomentumBacktestPanel />;
  const backtestSection: Exclude<BacktestSection, 'watchlist'> = requestedSection === 'filters' ? 'filters' : 'algorithms';

  return <BacktestPageContent key={backtestSection} backtestSection={backtestSection} />;
}

export default function BacktestPage() {
  return (
    <Suspense fallback={<div className="bt-page-loading" aria-label="Loading backtest workspace" />}>
      <BacktestPageRoute />
    </Suspense>
  );
}
