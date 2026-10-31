import type { FilterBacktestResult } from '@/lib/filter-types';
import StockIdentity from './StockIdentity';

type Props = {
  result: FilterBacktestResult | null;
  loading?: boolean;
  error?: string;
};

function metric(value: number | undefined, suffix = '') {
  return value == null || Number.isNaN(value) ? '—' : `${value.toFixed(2)}${suffix}`;
}

function tradeValue(trade: Record<string, unknown>, key: string) {
  const value = trade[key];
  return value == null ? '—' : String(value);
}

export default function FilterBacktestResultDetails({ result, loading = false, error }: Props) {
  if (!result) {
    return (
      <div className="bt-result-details">
        <div className="card" style={{ padding: 24, textAlign: 'center' }}>
          {loading ? 'Loading trade details…' : error || 'Trade details are unavailable for this run.'}
        </div>
      </div>
    );
  }

  const hasSwingProfile = result.trades.some((trade) => trade.poc != null);

  return (
    <div className="bt-result-details">
      <div className="bt-stats-grid">
        <div className="bt-stat-card">
          <div className="bt-stat-label">Total return</div>
          <div className="bt-stat-value" style={{ color: result.total_return_pct >= 0 ? 'var(--color-profit)' : 'var(--color-loss)' }}>{metric(result.total_return_pct, '%')}</div>
          <div className="bt-stat-sub"><StockIdentity symbol={result.symbol} name={result.company_name} company_name={result.company_name} sector={result.sector} /></div>
          <div className="bt-stat-sub">Sector: {result.sector || '—'}</div>
        </div>
        <div className="bt-stat-card">
          <div className="bt-stat-label">Final capital</div>
          <div className="bt-stat-value">₹{result.final_capital.toLocaleString('en-IN', { maximumFractionDigits: 0 })}</div>
          <div className="bt-stat-sub">from ₹{result.initial_capital.toLocaleString('en-IN', { maximumFractionDigits: 0 })}</div>
        </div>
        <div className="bt-stat-card">
          <div className="bt-stat-label">Max drawdown</div>
          <div className="bt-stat-value" style={{ color: 'var(--color-loss)' }}>{metric(result.max_drawdown_pct, '%')}</div>
          <div className="bt-stat-sub">peak to trough</div>
        </div>
        <div className="bt-stat-card">
          <div className="bt-stat-label">Win rate</div>
          <div className="bt-stat-value">{metric(result.win_rate_pct, '%')}</div>
          <div className="bt-stat-sub">{result.winning_trades}W / {result.losing_trades}L</div>
        </div>
        <div className="bt-stat-card">
          <div className="bt-stat-label">Trading charges</div>
          <div className="bt-stat-value">₹{(result.total_charges ?? 0).toLocaleString('en-IN', { maximumFractionDigits: 0 })}</div>
          <div className="bt-stat-sub">Already deducted from P&amp;L</div>
        </div>
      </div>
      <div className="bt-trades-section">
        <div className="bt-trades-header">
          <span className="bt-trades-title">Trade log</span>
          <span className="bt-trades-count">{result.total_trades} trades</span>
        </div>
        <div className="card" style={{ padding: 0, overflow: 'auto', maxHeight: 480 }}>
          <table className="data-table">
            <thead><tr><th>Entry</th><th>Exit</th><th>Entry price</th>{hasSwingProfile && <><th>Swing low</th><th>Swing high</th><th>Volume POC</th><th>Value-area low</th><th>Value-area high</th><th>Stop</th><th>Target</th></>}<th>Exit price</th><th>Charges</th><th>P&amp;L</th><th>Reason</th></tr></thead>
            <tbody>
              {loading ? (
                <tr><td colSpan={hasSwingProfile ? 14 : 7} style={{ textAlign: 'center', padding: 24 }}>Loading trade details…</td></tr>
              ) : result.trades.length === 0 ? (
                <tr><td colSpan={hasSwingProfile ? 14 : 7} style={{ textAlign: 'center', padding: 24 }}>No trades were taken for this filter combination.</td></tr>
              ) : result.trades.map((trade, index) => (
                <tr key={`${tradeValue(trade, 'entry_date')}-${index}`}>
                  <td>{tradeValue(trade, 'entry_date')}</td>
                  <td>{tradeValue(trade, 'exit_date')}</td>
                  <td>{tradeValue(trade, 'entry_price')}</td>
                  {hasSwingProfile && <><td>{tradeValue(trade, 'swing_low')}</td><td>{tradeValue(trade, 'swing_high')}</td><td>{tradeValue(trade, 'poc')}</td><td>{tradeValue(trade, 'value_area_low')}</td><td>{tradeValue(trade, 'value_area_high')}</td><td>{tradeValue(trade, 'stop_price')}</td><td>{tradeValue(trade, 'target_price')}</td></>}
                  <td>{tradeValue(trade, 'exit_price')}</td>
                  <td>{tradeValue(trade, 'total_charges')}</td>
                  <td>{tradeValue(trade, 'pnl')}</td>
                  <td>{tradeValue(trade, 'exit_reason')}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
