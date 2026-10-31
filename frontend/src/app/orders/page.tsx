'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { getOrders } from '@/lib/api';
import type { Order } from '@/lib/types';
import StockIdentity from '@/components/StockIdentity';

export default function OrdersPage() {
  const router = useRouter();
  const [orders, setOrders] = useState<Order[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    getOrders(100)
      .then(setOrders)
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <div>
        <h1 className="page-title">Orders</h1>
        <div className="skeleton" style={{ width: '100%', height: 400 }} />
      </div>
    );
  }

  // Stats
  const total = orders.length;
  const filled = orders.filter((o) => o.status === 'FILLED').length;
  const rejected = orders.filter((o) => o.status === 'REJECTED').length;
  const fillRate = total > 0 ? ((filled / total) * 100).toFixed(0) : '0';

  return (
    <div>
      <h1 className="page-title">Orders</h1>

      {/* Stats */}
      <div className="stats-grid">
        <div className="stat-card">
          <div className="stat-card-label">Total Orders</div>
          <div className="stat-card-value">{total}</div>
        </div>
        <div className="stat-card">
          <div className="stat-card-label">Filled</div>
          <div className="stat-card-value" style={{ color: 'var(--color-profit)' }}>{filled}</div>
        </div>
        <div className="stat-card">
          <div className="stat-card-label">Rejected</div>
          <div className="stat-card-value" style={{ color: 'var(--color-loss)' }}>{rejected}</div>
        </div>
        <div className="stat-card">
          <div className="stat-card-label">Fill Rate</div>
          <div className="stat-card-value">{fillRate}%</div>
        </div>
      </div>

      <div className="card">
        {orders.length > 0 ? (
          <table className="data-table">
            <thead>
              <tr>
                <th>Time</th>
                <th>Stock</th>
                <th>Sector</th>
                <th>Side</th>
                <th>Qty</th>
                <th>Price</th>
                <th>Fill Price</th>
                <th>Status</th>
                <th>Mode</th>
              </tr>
            </thead>
            <tbody>
              {orders.map((ord) => (
                <tr key={ord.id} onClick={() => router.push(`/stocks/${ord.symbol}`)}>
                  <td style={{ fontSize: 12, color: 'var(--text-tertiary)', fontFamily: "'JetBrains Mono', monospace" }}>
                    {new Date(ord.created_at).toLocaleString('en-IN', {
                      timeZone: 'Asia/Kolkata',
                      day: '2-digit',
                      month: 'short',
                      hour: '2-digit',
                      minute: '2-digit',
                    })}
                  </td>
                  <td><StockIdentity symbol={ord.symbol} name={ord.name} company_name={ord.company_name} /></td>
                  <td>{ord.sector || '—'}</td>
                  <td>
                    <span className={`badge ${ord.transaction_type === 'BUY' ? 'badge-buy' : 'badge-sell'}`}>
                      {ord.transaction_type}
                    </span>
                  </td>
                  <td>{ord.quantity}</td>
                  <td style={{ fontFamily: "'JetBrains Mono', monospace" }}>₹{ord.price?.toFixed(2)}</td>
                  <td style={{ fontFamily: "'JetBrains Mono', monospace" }}>
                    {ord.avg_fill_price > 0 ? `₹${ord.avg_fill_price.toFixed(2)}` : '-'}
                  </td>
                  <td>
                    <span
                      className={`badge ${
                        ord.status === 'FILLED' ? 'badge-buy' : ord.status === 'REJECTED' ? 'badge-sell' : 'badge-paper'
                      }`}
                    >
                      {ord.status}
                    </span>
                  </td>
                  <td>
                    <span className="badge badge-paper">{ord.is_paper_trade ? 'PAPER' : 'LIVE'}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="empty-state">
            <div className="empty-state-icon">📝</div>
            <div className="empty-state-text">No orders yet</div>
            <div className="empty-state-sub">Orders appear when signals are executed</div>
          </div>
        )}
      </div>
    </div>
  );
}
