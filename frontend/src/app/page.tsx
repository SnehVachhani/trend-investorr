'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import {
  getPortfolioSummary,
  getSignals,
  getPositions,
  getSystemStatus,
} from '@/lib/api';
import type { PortfolioSummary, Signal, Position, SystemStatus } from '@/lib/types';
import StockIdentity from '@/components/StockIdentity';

const SYSTEM_STATUS_POLL_INTERVAL_MS = 5_000;

function formatCurrency(n: number): string {
  if (Math.abs(n) >= 10_000_000) return `₹${(n / 10_000_000).toFixed(2)} Cr`;
  if (Math.abs(n) >= 100_000) return `₹${(n / 100_000).toFixed(2)} L`;
  return `₹${n.toLocaleString('en-IN', { maximumFractionDigits: 0 })}`;
}

function formatPct(n: number): string {
  return `${n >= 0 ? '+' : ''}${n.toFixed(2)}%`;
}

export default function DashboardPage() {
  const router = useRouter();
  const [portfolio, setPortfolio] = useState<PortfolioSummary | null>(null);
  const [signals, setSignals] = useState<Signal[]>([]);
  const [positions, setPositions] = useState<Position[]>([]);
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      try {
        const [p, s, pos, st] = await Promise.all([
          getPortfolioSummary(),
          getSignals('today', 10),
          getPositions(),
          getSystemStatus(),
        ]);
        if (cancelled) return;
        setPortfolio(p);
        setSignals(s);
        setPositions(pos);
        setStatus(st);
      } catch {
        setError('Failed to connect to API. Make sure FastAPI is running on port 8000.');
      } finally {
        setLoading(false);
      }
    }
    load();

    const statusPoll = window.setInterval(() => {
      getSystemStatus()
        .then((nextStatus) => {
          if (!cancelled) setStatus(nextStatus);
        })
        .catch(() => {
          // Keep showing the last known status if a poll is transiently unavailable.
        });
    }, SYSTEM_STATUS_POLL_INTERVAL_MS);

    return () => {
      cancelled = true;
      window.clearInterval(statusPoll);
    };
  }, []);

  if (loading) {
    return (
      <div>
        <h1 className="page-title">Dashboard</h1>
        <div className="stats-grid">
          {[1, 2, 3, 4].map((i) => (
            <div key={i} className="stat-card">
              <div className="skeleton" style={{ width: 120, height: 14, marginBottom: 12 }} />
              <div className="skeleton" style={{ width: 160, height: 28 }} />
            </div>
          ))}
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div>
        <h1 className="page-title">Dashboard</h1>
        <div className="card">
          <div className="empty-state">
            <div className="empty-state-icon">⚠️</div>
            <div className="empty-state-text">{error}</div>
            <div className="empty-state-sub">
              Run: uvicorn api.server:app --reload --port 8000
            </div>
          </div>
        </div>
      </div>
    );
  }

  const todayPnl = portfolio?.today
    ? portfolio.today.realized + portfolio.today.unrealized
    : 0;
  const todayPnlPct = portfolio
    ? (todayPnl / portfolio.total_capital) * 100
    : 0;

  return (
    <div>
      <h1 className="page-title">Dashboard</h1>

      {/* Stats Grid */}
      <div className="stats-grid">
        <div className="stat-card">
          <div className="stat-card-icon" style={{ background: 'var(--accent-muted)', color: 'var(--accent-secondary)' }}>
            💰
          </div>
          <div className="stat-card-label">Total Capital</div>
          <div className="stat-card-value">{formatCurrency(portfolio?.total_capital || 0)}</div>
          <div className="stat-card-sub">Paper Trading Mode</div>
        </div>

        <div className="stat-card">
          <div
            className="stat-card-icon"
            style={{
              background: todayPnl >= 0 ? 'var(--color-profit-bg)' : 'var(--color-loss-bg)',
              color: todayPnl >= 0 ? 'var(--color-profit)' : 'var(--color-loss)',
            }}
          >
            📈
          </div>
          <div className="stat-card-label">Today&apos;s P&amp;L</div>
          <div
            className="stat-card-value"
            style={{ color: todayPnl >= 0 ? 'var(--color-profit)' : 'var(--color-loss)' }}
          >
            {formatCurrency(todayPnl)}
          </div>
          <div className="stat-card-sub">{formatPct(todayPnlPct)}</div>
        </div>

        <div className="stat-card">
          <div className="stat-card-icon" style={{ background: 'var(--color-info-bg)', color: 'var(--color-info)' }}>
            📊
          </div>
          <div className="stat-card-label">Open Positions</div>
          <div className="stat-card-value">{portfolio?.positions.count || 0}</div>
          <div className="stat-card-sub">
            {formatCurrency(portfolio?.positions.total_value || 0)} deployed
          </div>
        </div>

        <div className="stat-card">
          <div className="stat-card-icon" style={{ background: 'var(--color-warning-bg)', color: 'var(--color-warning)' }}>
            ⚡
          </div>
          <div className="stat-card-label">Total P&amp;L</div>
          <div
            className="stat-card-value"
            style={{
              color:
                (portfolio?.total_pnl || 0) >= 0
                  ? 'var(--color-profit)'
                  : 'var(--color-loss)',
            }}
          >
            {formatCurrency(portfolio?.total_pnl || 0)}
          </div>
          <div className="stat-card-sub">Since inception</div>
        </div>
      </div>

      {/* Two-column layout */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
        {/* System Status */}
        <div className="card">
          <div className="card-header">
            <span className="card-title">System Status</span>
            <span
              className={`badge ${status?.status === 'running' ? 'badge-buy' : 'badge-sell'}`}
            >
              {status?.status}
            </span>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 13 }}>
              <span style={{ color: 'var(--text-secondary)' }}>Database</span>
              <span style={{ color: status?.database === 'connected' ? 'var(--color-profit)' : 'var(--color-loss)' }}>
                {status?.database}
              </span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 13 }}>
              <span style={{ color: 'var(--text-secondary)' }}>Active Stocks</span>
              <span>{status?.stocks_active?.toLocaleString()}</span>
            </div>
            {status?.coverage?.map((c) => (
              <div
                key={c.interval}
                style={{ display: 'flex', justifyContent: 'space-between', fontSize: 13 }}
              >
                <span style={{ color: 'var(--text-secondary)' }}>{c.interval}</span>
                <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 12 }}>
                  {c.stocks} stocks / {Number(c.candles || 0).toLocaleString()} candles
                </span>
              </div>
            ))}
          </div>
        </div>

        {/* Today's Signals */}
        <div className="card">
          <div className="card-header">
            <span className="card-title">Today&apos;s Signals</span>
            <span style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>{signals.length} signals</span>
          </div>
          {signals.length > 0 ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              {signals.slice(0, 6).map((sig) => (
                <div
                  key={sig.id}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    padding: '8px 12px',
                    background: 'var(--bg-surface)',
                    borderRadius: 'var(--radius-md)',
                    cursor: 'pointer',
                  }}
                  onClick={() => router.push(`/stocks/${sig.symbol}`)}
                >
                  <div>
                    <StockIdentity symbol={sig.symbol} name={sig.name} company_name={sig.company_name} />
                    <small style={{ display: 'block', marginTop: 3, color: 'var(--text-tertiary)' }}>Sector: {sig.sector || '—'}</small>
                    <span
                      className={`badge ${sig.signal_type === 'BUY' ? 'badge-buy' : 'badge-sell'}`}
                      style={{ marginLeft: 8 }}
                    >
                      {sig.signal_type}
                    </span>
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <span className={`badge badge-${sig.strength?.toLowerCase()}`}>
                      {sig.strength}
                    </span>
                    <span style={{ fontSize: 12, color: 'var(--text-secondary)', fontFamily: "'JetBrains Mono', monospace" }}>
                      ₹{sig.price?.toFixed(2)}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="empty-state">
              <div className="empty-state-icon">⚡</div>
              <div className="empty-state-text">No signals today</div>
              <div className="empty-state-sub">Signals appear during market hours</div>
            </div>
          )}
        </div>
      </div>

      {/* Active Positions */}
      <div className="section" style={{ marginTop: 16 }}>
        <div className="card">
          <div className="card-header">
            <span className="card-title">Active Positions</span>
          </div>
          {positions.length > 0 ? (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Stock</th>
                  <th>Sector</th>
                  <th>Qty</th>
                  <th>Avg Price</th>
                  <th>Current</th>
                  <th>P&amp;L</th>
                  <th>Type</th>
                </tr>
              </thead>
              <tbody>
                {positions.map((pos) => {
                  const pnlPct =
                    pos.avg_buy_price > 0
                      ? ((pos.current_price - pos.avg_buy_price) / pos.avg_buy_price) * 100
                      : 0;
                  return (
                    <tr key={pos.id} onClick={() => router.push(`/stocks/${pos.symbol}`)}>
                      <td><StockIdentity symbol={pos.symbol} name={pos.name} company_name={pos.company_name} /></td>
                      <td>{pos.sector || '—'}</td>
                      <td>{pos.quantity}</td>
                      <td style={{ fontFamily: "'JetBrains Mono', monospace" }}>
                        ₹{pos.avg_buy_price?.toFixed(2)}
                      </td>
                      <td style={{ fontFamily: "'JetBrains Mono', monospace" }}>
                        ₹{pos.current_price?.toFixed(2)}
                      </td>
                      <td
                        style={{
                          color: pos.unrealized_pnl >= 0 ? 'var(--color-profit)' : 'var(--color-loss)',
                          fontWeight: 600,
                          fontFamily: "'JetBrains Mono', monospace",
                        }}
                      >
                        {formatCurrency(pos.unrealized_pnl)} ({formatPct(pnlPct)})
                      </td>
                      <td>
                        <span className="badge badge-paper">{pos.product_type}</span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          ) : (
            <div className="empty-state">
              <div className="empty-state-icon">📭</div>
              <div className="empty-state-text">No active positions</div>
              <div className="empty-state-sub">Positions appear when trades are executed</div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
