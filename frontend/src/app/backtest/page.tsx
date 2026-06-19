'use client';

import { useState, useEffect, useRef, useCallback } from 'react';
import type { Algorithm, BacktestRun, BacktestTrade, Stock } from '@/lib/types';
import {
  getAlgorithms,
  runBacktest,
  getBacktestHistory,
  getBacktestResult,
  deleteBacktest,
  searchStocks,
} from '@/lib/api';

// ── Algo display names ──────────────────────────────────────
const ALGO_LABELS: Record<string, string> = {
  connors_rsi_pullback: 'Connors RSI Pullback',
  rayner_dip_buyer: 'Rayner Dip Buyer',
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
    stop_loss: 'Stop Loss',
    time_stop: 'Time Stop',
    backtest_end: 'End',
  };
  return map[reason] || reason;
}

// ═══════════════════════════════════════════════════════════════
// COMPONENT
// ═══════════════════════════════════════════════════════════════

export default function BacktestPage() {
  // ── State ──────────────────────────────────────────────
  const [algorithms, setAlgorithms] = useState<Algorithm[]>([]);
  const [history, setHistory] = useState<BacktestRun[]>([]);
  const [activeRun, setActiveRun] = useState<BacktestRun | null>(null);
  const [mode, setMode] = useState<'form' | 'processing' | 'results'>('form');
  const [error, setError] = useState('');

  // Form state
  const [selectedAlgo, setSelectedAlgo] = useState('');
  const [selectedSymbol, setSelectedSymbol] = useState('');
  const [selectedStockName, setSelectedStockName] = useState('');
  const [startDate, setStartDate] = useState('');
  const [endDate, setEndDate] = useState('');
  const [capital, setCapital] = useState('1000000');

  // Stock search
  const [stockQuery, setStockQuery] = useState('');
  const [stockResults, setStockResults] = useState<Stock[]>([]);
  const [showStockDropdown, setShowStockDropdown] = useState(false);
  const searchTimeout = useRef<NodeJS.Timeout | null>(null);

  // Chart ref
  const chartRef = useRef<HTMLDivElement>(null);

  // ── Load initial data ──────────────────────────────────
  useEffect(() => {
    getAlgorithms()
      .then((algos) => {
        // Only show algos that support backtesting (daily candle strategies)
        const backtestable = algos.filter((a) =>
          ['connors_rsi_pullback', 'rayner_dip_buyer'].includes(a.name)
        );
        setAlgorithms(backtestable);
        if (backtestable.length > 0) setSelectedAlgo(backtestable[0].name);
      })
      .catch(() => {});
    loadHistory();
  }, []);

  // ── Load history ───────────────────────────────────────
  const loadHistory = useCallback(async () => {
    try {
      const runs = await getBacktestHistory();
      setHistory(runs);
    } catch {
      /* silent */
    }
  }, []);

  // ── Stock search ───────────────────────────────────────
  const handleStockSearch = (q: string) => {
    setStockQuery(q);
    setSelectedSymbol('');
    setSelectedStockName('');

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
    setSelectedStockName(stock.name);
    setStockQuery(stock.symbol.replace('-EQ', ''));
    setShowStockDropdown(false);
  };

  // ── Run backtest ───────────────────────────────────────
  const handleRunBacktest = async () => {
    if (!selectedAlgo || !selectedSymbol) return;

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
      setMode('results');

      // Refresh history
      await loadHistory();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Backtest failed');
      setMode('form');
    }
  };

  // ── Load a historical run ──────────────────────────────
  const loadRun = async (runId: number) => {
    setMode('processing');
    setError('');
    try {
      const result = await getBacktestResult(runId);
      setActiveRun(result);
      setMode('results');
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load run');
      setMode('form');
    }
  };

  // ── Delete a run ───────────────────────────────────────
  const handleDelete = async (runId: number, e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      await deleteBacktest(runId);
      setHistory((prev) => prev.filter((r) => r.id !== runId));
      if (activeRun?.id === runId) {
        setActiveRun(null);
        setMode('form');
      }
    } catch {
      /* silent */
    }
  };

  // ── Equity chart (simple canvas) ───────────────────────
  useEffect(() => {
    if (mode !== 'results' || !activeRun?.equity_curve?.length || !chartRef.current) return;

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
    const lastY = pad.top + plotH - ((values[values.length - 1] - minVal) / range) * plotH;
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

  // ═════════════════════════════════════════════════════════
  // RENDER
  // ═════════════════════════════════════════════════════════

  return (
    <div className="bt-layout">
      {/* ── History Sidebar ── */}
      <aside className="bt-history">
        <div className="bt-history-header">
          <span className="bt-history-title">History</span>
          <button
            className="bt-new-btn"
            onClick={() => {
              setActiveRun(null);
              setMode('form');
              setError('');
            }}
          >
            + New
          </button>
        </div>
        <div className="bt-history-list">
          {history.length === 0 && (
            <div className="empty-state" style={{ padding: '32px 16px' }}>
              <div className="empty-state-icon">🧪</div>
              <div className="empty-state-text">No backtests yet</div>
              <div className="empty-state-sub">Run your first backtest</div>
            </div>
          )}
          {history.map((run) => (
            <div
              key={run.id}
              className={`bt-history-item ${activeRun?.id === run.id ? 'active' : ''}`}
              onClick={() => loadRun(run.id)}
            >
              <div className="bt-history-item-algo">
                {ALGO_LABELS[run.algorithm_name] || run.algorithm_name}
              </div>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                <span className="bt-history-item-symbol">
                  {run.stock_symbol.replace('-EQ', '')}
                </span>
                <button
                  className="bt-history-item-delete"
                  title="Delete"
                  onClick={(e) => handleDelete(run.id, e)}
                >
                  ✕
                </button>
              </div>
              <div className="bt-history-item-meta">
                <span className="bt-history-item-date">{fmtDate(run.created_at)}</span>
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
          ))}
        </div>
      </aside>

      {/* ── Main Panel ── */}
      <div className="bt-main">
        {error && <div className="bt-error">{error}</div>}

        {/* MODE: Form */}
        {mode === 'form' && (
          <div className="bt-form-container">
            <h1 className="bt-form-title">Run Backtest</h1>
            <p className="bt-form-subtitle">
              Test trading algorithms against historical data
            </p>

            <div className="bt-form">
              {/* Algorithm */}
              <div className="bt-field">
                <label className="bt-field-label" htmlFor="bt-algo">Algorithm</label>
                <select
                  id="bt-algo"
                  className="bt-field-select"
                  value={selectedAlgo}
                  onChange={(e) => setSelectedAlgo(e.target.value)}
                >
                  {algorithms.map((a) => (
                    <option key={a.name} value={a.name}>
                      {a.display_name}
                    </option>
                  ))}
                </select>
              </div>

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
                    <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: '4px' }}>
                      Selected: <strong style={{ color: 'var(--accent-secondary)' }}>{selectedSymbol.replace('-EQ', '')}</strong>
                      {selectedStockName && ` — ${selectedStockName}`}
                    </div>
                  )}
                  {showStockDropdown && stockResults.length > 0 && (
                    <div className="bt-stock-results">
                      {stockResults.map((s) => (
                        <div
                          key={s.id}
                          className="bt-stock-option"
                          onMouseDown={() => selectStock(s)}
                        >
                          <div>
                            <span className="bt-stock-option-sym">{s.symbol.replace('-EQ', '')}</span>
                            <span className="bt-stock-option-name" style={{ marginLeft: '8px' }}>{s.name}</span>
                          </div>
                          {s.latest_price ? (
                            <span style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: '12px', fontWeight: 600, color: 'var(--text-primary)' }}>
                              ₹{s.latest_price.toLocaleString('en-IN', { maximumFractionDigits: 2 })}
                            </span>
                          ) : (
                            <span style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>No data</span>
                          )}
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
        {mode === 'processing' && (
          <div className="bt-processing">
            <div className="bt-processing-spinner" />
            <div className="bt-processing-text">Running Backtest...</div>
            <div className="bt-processing-sub">
              Computing indicators and simulating trades
            </div>
          </div>
        )}

        {/* MODE: Results */}
        {mode === 'results' && activeRun && (
          <div>
            {/* Header */}
            <div className="bt-results-header">
              <h1 className="bt-results-title">
                {ALGO_LABELS[activeRun.algorithm_name] || activeRun.algorithm_name}{' '}
                — {activeRun.stock_symbol.replace('-EQ', '')}
              </h1>
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
                        <th>Qty</th>
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
                          <td>{t.quantity}</td>
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
  );
}
