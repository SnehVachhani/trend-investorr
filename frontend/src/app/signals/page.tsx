'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { getSignals, getScannerSignals } from '@/lib/api';
import type { Signal, ScannerSignal } from '@/lib/types';

export default function SignalsPage() {
  const router = useRouter();
  const [tab, setTab] = useState<'trading' | 'scanner'>('trading');
  const [signals, setSignals] = useState<Signal[]>([]);
  const [scannerSignals, setScannerSignals] = useState<ScannerSignal[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([getSignals(undefined, 100), getScannerSignals(undefined, 100)])
      .then(([s, ss]) => {
        setSignals(s);
        setScannerSignals(ss);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <div>
        <h1 className="page-title">Signals</h1>
        <div className="skeleton" style={{ width: '100%', height: 400 }} />
      </div>
    );
  }

  return (
    <div>
      <h1 className="page-title">Signals</h1>

      {/* Tab switcher */}
      <div className="interval-tabs" style={{ marginBottom: 20, display: 'inline-flex' }}>
        <button
          className={`interval-tab ${tab === 'trading' ? 'active' : ''}`}
          onClick={() => setTab('trading')}
          style={{ fontFamily: 'Inter, sans-serif' }}
        >
          Trading Signals ({signals.length})
        </button>
        <button
          className={`interval-tab ${tab === 'scanner' ? 'active' : ''}`}
          onClick={() => setTab('scanner')}
          style={{ fontFamily: 'Inter, sans-serif' }}
        >
          Scanner Signals ({scannerSignals.length})
        </button>
      </div>

      <div className="card">
        {tab === 'trading' ? (
          signals.length > 0 ? (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Symbol</th>
                  <th>Type</th>
                  <th>Strength</th>
                  <th>Score</th>
                  <th>Price</th>
                  <th>Vol Ratio</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {signals.map((sig) => (
                  <tr key={sig.id} onClick={() => router.push(`/stocks/${sig.symbol}`)}>
                    <td style={{ fontSize: 12, color: 'var(--text-tertiary)', fontFamily: "'JetBrains Mono', monospace" }}>
                      {new Date(sig.created_at).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', day: '2-digit', month: 'short' })}
                    </td>
                    <td style={{ fontWeight: 600 }}>{sig.symbol}</td>
                    <td><span className={`badge ${sig.signal_type === 'BUY' ? 'badge-buy' : 'badge-sell'}`}>{sig.signal_type}</span></td>
                    <td><span className={`badge badge-${sig.strength?.toLowerCase()}`}>{sig.strength}</span></td>
                    <td style={{ fontFamily: "'JetBrains Mono', monospace" }}>{sig.score?.toFixed(3)}</td>
                    <td style={{ fontFamily: "'JetBrains Mono', monospace" }}>₹{sig.price?.toFixed(2)}</td>
                    <td style={{ fontFamily: "'JetBrains Mono', monospace" }}>{sig.volume_ratio?.toFixed(1)}x</td>
                    <td><span className="badge badge-paper">{sig.status}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div className="empty-state">
              <div className="empty-state-icon">⚡</div>
              <div className="empty-state-text">No trading signals yet</div>
            </div>
          )
        ) : scannerSignals.length > 0 ? (
          <table className="data-table">
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Scanner</th>
                <th>Pattern</th>
                <th>Direction</th>
                <th>Confidence</th>
                <th>Price</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {scannerSignals.map((sig) => (
                <tr key={sig.id} onClick={() => router.push(`/stocks/${sig.symbol}`)}>
                  <td style={{ fontWeight: 600 }}>{sig.symbol}</td>
                  <td style={{ fontSize: 12, color: 'var(--text-secondary)' }}>{sig.scanner_name}</td>
                  <td>{sig.pattern}</td>
                  <td>
                    <span className={`badge ${sig.direction === 'bullish' ? 'badge-buy' : 'badge-sell'}`}>
                      {sig.direction}
                    </span>
                  </td>
                  <td style={{ fontFamily: "'JetBrains Mono', monospace" }}>{(sig.confidence * 100).toFixed(0)}%</td>
                  <td style={{ fontFamily: "'JetBrains Mono', monospace" }}>₹{sig.price?.toFixed(2)}</td>
                  <td><span className="badge badge-paper">{sig.status}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="empty-state">
            <div className="empty-state-icon">🔍</div>
            <div className="empty-state-text">No scanner signals yet</div>
          </div>
        )}
      </div>
    </div>
  );
}
