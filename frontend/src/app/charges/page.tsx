'use client';

import { useEffect, useState } from 'react';
import {
  getTradingCharges,
  updateTradingCharges,
  type TradingChargesConfig,
} from '@/lib/api';

type NumericField = Exclude<keyof TradingChargesConfig, 'enabled' | 'product_type' | 'exchange'>;

const NUMERIC_FIELDS: Array<{ key: NumericField; label: string; suffix?: string; help: string }> = [
  { key: 'brokerage_rate_pct', label: 'Brokerage rate', suffix: '%', help: 'Zero for equity delivery under this schedule.' },
  { key: 'brokerage_cap_rupees', label: 'Brokerage cap', suffix: '₹', help: 'Maximum brokerage for one executed order.' },
  { key: 'brokerage_min_rupees', label: 'Brokerage minimum', suffix: '₹', help: 'Minimum brokerage for one executed order.' },
  { key: 'stt_buy_pct', label: 'STT on buy', suffix: '%', help: 'Delivery STT charged to the buyer.' },
  { key: 'stt_sell_pct', label: 'STT on sell', suffix: '%', help: 'Delivery STT charged to the seller.' },
  { key: 'transaction_charge_pct', label: 'NSE transaction charge', suffix: '%', help: 'Exchange turnover charge on each side.' },
  { key: 'ipft_charge_pct', label: 'IPFT charge', suffix: '%', help: 'Investor Protection Fund Trust charge on turnover.' },
  { key: 'stamp_duty_buy_pct', label: 'Stamp duty on buy', suffix: '%', help: 'Buy-side stamp duty for delivery orders.' },
  { key: 'sebi_fee_per_crore', label: 'SEBI fee', suffix: '₹ / crore', help: 'SEBI turnover fee applied to buy and sell turnover.' },
  { key: 'dp_charge_per_sell_rupees', label: 'DP charge on sell', suffix: '₹', help: 'Per sell instruction and ISIN before GST.' },
  { key: 'gst_pct', label: 'GST', suffix: '%', help: 'Applied to brokerage, exchange/regulatory services, and DP charges.' },
];

export default function BrokerageChargesPage() {
  const [config, setConfig] = useState<TradingChargesConfig | null>(null);
  const [formula, setFormula] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');

  useEffect(() => {
    getTradingCharges()
      .then((response) => {
        setConfig(response.config);
        setFormula(response.formula);
      })
      .catch((requestError) => setError(requestError instanceof Error ? requestError.message : 'Unable to load charge settings'))
      .finally(() => setLoading(false));
  }, []);

  const updateNumber = (key: NumericField, value: string) => {
    const parsed = Number(value);
    setConfig((current) => current ? { ...current, [key]: Number.isFinite(parsed) ? parsed : 0 } : current);
  };

  const save = async () => {
    if (!config) return;
    setSaving(true);
    setError('');
    setMessage('');
    try {
      const response = await updateTradingCharges(config);
      setConfig(response.config);
      setFormula(response.formula);
      setMessage('Trading cost settings saved. New backtests will use this schedule.');
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Unable to save charge settings');
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <div><h1 className="page-title">Brokerage charges</h1><div className="skeleton" style={{ width: '100%', height: 420 }} /></div>;
  if (!config) return <div><h1 className="page-title">Brokerage charges</h1><div className="bt-error">{error || 'Charge settings are unavailable.'}</div></div>;

  return (
    <div>
      <div className="section-header" style={{ alignItems: 'flex-start', gap: 20 }}>
        <div>
          <h1 className="page-title" style={{ marginBottom: 6 }}>Brokerage charges</h1>
          <p style={{ color: 'var(--text-secondary)', maxWidth: 760, margin: 0 }}>
            Master equity-delivery transaction-cost settings used by algorithm backtests, filter combinations, and watchlist analysis. These are trading charges—not capital-gains tax.
          </p>
        </div>
        <span className={`badge ${config.enabled ? 'badge-buy' : 'badge-sell'}`}>{config.enabled ? 'Included in backtests' : 'Disabled'}</span>
      </div>

      {error && <div className="bt-error" style={{ marginBottom: 16 }}>{error}</div>}
      {message && <div className="bt-success" style={{ marginBottom: 16 }}>{message}</div>}

      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1.35fr) minmax(300px, .65fr)', gap: 18, alignItems: 'start' }}>
        <section className="card" style={{ padding: 22 }}>
          <div className="section-header" style={{ marginBottom: 18 }}>
            <div>
              <h2 className="section-title">Equity delivery charges</h2>
              <span style={{ color: 'var(--text-tertiary)', fontSize: 12 }}>{config.product_type} · {config.exchange}</span>
            </div>
            <label style={{ display: 'flex', alignItems: 'center', gap: 8, color: 'var(--text-secondary)', fontSize: 13 }}>
              <input type="checkbox" checked={config.enabled} onChange={(event) => setConfig({ ...config, enabled: event.target.checked })} />
              Include costs
            </label>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 14 }}>
            {NUMERIC_FIELDS.map(({ key, label, suffix, help }) => (
              <label className="bt-field" key={key}>
                <span className="bt-field-label">{label} {suffix && <span style={{ color: 'var(--text-tertiary)' }}>({suffix})</span>}</span>
                <input className="bt-field-input" type="number" min="0" step="any" value={config[key]} onChange={(event) => updateNumber(key, event.target.value)} />
                <small style={{ color: 'var(--text-tertiary)', lineHeight: 1.4 }}>{help}</small>
              </label>
            ))}
          </div>

          <button className="bt-run-btn" type="button" onClick={save} disabled={saving} style={{ marginTop: 20 }}>
            {saving ? 'Saving…' : 'Save master configuration'}
          </button>
        </section>

        <aside className="card" style={{ padding: 22 }}>
          <h2 className="section-title" style={{ marginBottom: 8 }}>What gets charged</h2>
          <p style={{ color: 'var(--text-secondary)', fontSize: 13, lineHeight: 1.6, marginTop: 0 }}>
            A buy reduces the stock wallet by the share value plus buy-side charges. A sell returns the share value minus sell-side charges. The next trade is sized from that updated wallet, so profits and costs compound together.
          </p>
          <div style={{ borderTop: '1px solid var(--border-primary)', paddingTop: 14, marginTop: 16, display: 'grid', gap: 12 }}>
            <div><strong>Brokerage</strong><div style={{ color: 'var(--text-tertiary)', fontSize: 12 }}>{formula.brokerage}</div></div>
            <div><strong>GST</strong><div style={{ color: 'var(--text-tertiary)', fontSize: 12 }}>{formula.gst}</div></div>
            <div><strong>Buy total</strong><div style={{ color: 'var(--text-tertiary)', fontSize: 12 }}>{formula.buy_total}</div></div>
            <div><strong>Sell total</strong><div style={{ color: 'var(--text-tertiary)', fontSize: 12 }}>{formula.sell_total}</div></div>
            <div><strong>Rounding</strong><div style={{ color: 'var(--text-tertiary)', fontSize: 12 }}>{formula.rounding}</div></div>
          </div>
          <div style={{ borderTop: '1px solid var(--border-primary)', marginTop: 18, paddingTop: 14, color: 'var(--text-tertiary)', fontSize: 12, lineHeight: 1.5 }}>
            DP charges are applied once per simulated sell order. Backtest P&amp;L is net of all configured charges.
          </div>
        </aside>
      </div>
    </div>
  );
}
