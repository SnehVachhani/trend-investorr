'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { getPortfolioSummary, getPnL, getPositions } from '@/lib/api';
import type { PortfolioSummary, DailyPnL, Position } from '@/lib/types';
import StockIdentity from '@/components/StockIdentity';

function fmt(n: number): string {
  if (Math.abs(n) >= 1e7) return `₹${(n / 1e7).toFixed(2)} Cr`;
  if (Math.abs(n) >= 1e5) return `₹${(n / 1e5).toFixed(2)} L`;
  return `₹${n.toLocaleString('en-IN', { maximumFractionDigits: 0 })}`;
}

export default function PortfolioPage() {
  const router = useRouter();
  const [portfolio, setPortfolio] = useState<PortfolioSummary | null>(null);
  const [pnl, setPnl] = useState<DailyPnL[]>([]);
  const [positions, setPositions] = useState<Position[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([getPortfolioSummary(), getPnL(90), getPositions()])
      .then(([p, pl, pos]) => { setPortfolio(p); setPnl(pl); setPositions(pos); })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <div><h1 className="page-title">Portfolio</h1><div className="skeleton" style={{width:'100%',height:400}}/></div>;

  const tp = portfolio?.total_pnl || 0;
  return (
    <div>
      <h1 className="page-title">Portfolio</h1>
      <div className="stats-grid">
        <div className="stat-card"><div className="stat-card-label">Total P&amp;L</div><div className="stat-card-value" style={{color:tp>=0?'var(--color-profit)':'var(--color-loss)'}}>{fmt(tp)}</div></div>
        <div className="stat-card"><div className="stat-card-label">Capital</div><div className="stat-card-value">{fmt(portfolio?.total_capital||0)}</div></div>
        <div className="stat-card"><div className="stat-card-label">Positions</div><div className="stat-card-value">{positions.length}</div></div>
        <div className="stat-card"><div className="stat-card-label">Deployed</div><div className="stat-card-value">{fmt(portfolio?.positions.total_value||0)}</div></div>
      </div>
      <div className="card" style={{marginBottom:20}}>
        <div className="card-header"><span className="card-title">Positions</span></div>
        {positions.length>0?(
          <table className="data-table"><thead><tr><th>Stock</th><th>Sector</th><th>Qty</th><th>Avg</th><th>Current</th><th>P&amp;L</th></tr></thead>
          <tbody>{positions.map(p=>(
            <tr key={p.id} onClick={()=>router.push(`/stocks/${p.symbol}`)}>
              <td><StockIdentity symbol={p.symbol} name={p.name} company_name={p.company_name} /></td>
              <td>{p.sector || '—'}</td><td>{p.quantity}</td>
              <td style={{fontFamily:"'JetBrains Mono',monospace"}}>₹{p.avg_buy_price?.toFixed(2)}</td>
              <td style={{fontFamily:"'JetBrains Mono',monospace"}}>₹{p.current_price?.toFixed(2)}</td>
              <td style={{fontFamily:"'JetBrains Mono',monospace",fontWeight:600,color:p.unrealized_pnl>=0?'var(--color-profit)':'var(--color-loss)'}}>{fmt(p.unrealized_pnl)}</td>
            </tr>))}</tbody></table>
        ):(<div className="empty-state"><div className="empty-state-icon">📭</div><div className="empty-state-text">No positions</div></div>)}
      </div>
      <div className="card">
        <div className="card-header"><span className="card-title">Daily P&amp;L</span></div>
        {pnl.length>0?(
          <table className="data-table"><thead><tr><th>Date</th><th>Realized</th><th>Trades</th><th>Win Rate</th></tr></thead>
          <tbody>{pnl.map(d=>{const wr=d.total_trades>0?((d.winning_trades/d.total_trades)*100).toFixed(0)+"%":"-";return(
            <tr key={d.date} style={{cursor:'default'}}>
              <td style={{fontFamily:"'JetBrains Mono',monospace",fontSize:12}}>{d.date}</td>
              <td style={{fontFamily:"'JetBrains Mono',monospace",color:d.realized_pnl>=0?'var(--color-profit)':'var(--color-loss)'}}>{fmt(d.realized_pnl)}</td>
              <td>{d.total_trades}</td><td>{wr}</td>
            </tr>);})}</tbody></table>
        ):(<div className="empty-state"><div className="empty-state-icon">📊</div><div className="empty-state-text">No P&amp;L data</div></div>)}
      </div>
    </div>
  );
}
