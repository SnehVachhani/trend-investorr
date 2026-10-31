'use client';

import { useEffect, useMemo, useState } from 'react';
import {
  getFundamentalScoreInputs,
  getSectorAnalysis,
  getSectorStocks,
  type SectorPeriod,
  type SectorStockMember,
  type SectorSummary,
} from '@/lib/api';
import { assessFundamentals } from '@/lib/fundamental-analysis';
import StockIdentity from '@/components/StockIdentity';

const PERIODS: { key: SectorPeriod; label: string }[] = [
  { key: '1w', label: '1W' },
  { key: '1m', label: '1M' },
  { key: '3m', label: '3M' },
  { key: '6m', label: '6M' },
  { key: '1y', label: '1Y' },
];

type SortMode = 'overall' | 'technical' | 'fundamental' | '1m';

interface RankedStock extends SectorStockMember {
  fundamentalScore: number | null;
  fundamentalCoverage: number;
  fundamentalSignalCoverage: number;
  technicalScore: number | null;
  overallScore: number | null;
}

function formatPercent(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '—';
  return `${value > 0 ? '+' : ''}${value.toFixed(1)}%`;
}

function formatScore(value: number | null): string {
  return value == null ? '—' : Math.round(value).toString();
}

function percentClass(value: number | null | undefined): string {
  if (value == null) return 'sector-neutral';
  return value > 0 ? 'sector-positive' : value < 0 ? 'sector-negative' : 'sector-neutral';
}

function technicalScore(stock: SectorStockMember): number | null {
  const checks: Array<{ available: boolean; passed: boolean; weight: number }> = [
    { available: stock.technical.sma_20 != null, passed: stock.technical.sma_20 != null && stock.latest_close > stock.technical.sma_20, weight: 10 },
    { available: stock.technical.sma_50 != null, passed: stock.technical.sma_50 != null && stock.latest_close > stock.technical.sma_50, weight: 20 },
    { available: stock.technical.sma_200 != null, passed: stock.technical.sma_200 != null && stock.latest_close > stock.technical.sma_200, weight: 25 },
    { available: stock.returns['1m'] != null, passed: (stock.returns['1m'] ?? 0) > 0, weight: 12 },
    { available: stock.returns['3m'] != null, passed: (stock.returns['3m'] ?? 0) > 0, weight: 13 },
    {
      available: stock.technical.macd_line != null && stock.technical.macd_signal != null,
      passed: stock.technical.macd_line != null && stock.technical.macd_signal != null && stock.technical.macd_line > stock.technical.macd_signal,
      weight: 12,
    },
    {
      available: stock.technical.rsi_14 != null,
      passed: stock.technical.rsi_14 != null && stock.technical.rsi_14 >= 45 && stock.technical.rsi_14 <= 70,
      weight: 8,
    },
  ];
  const available = checks.filter((check) => check.available);
  const weight = available.reduce((sum, check) => sum + check.weight, 0);
  if (weight === 0) return null;
  return available.reduce((sum, check) => sum + (check.passed ? check.weight : 0), 0) / weight * 100;
}

function buildRankedStock(stock: SectorStockMember, fundamentalScore: number | null, fundamentalCoverage: number, fundamentalSignalCoverage = 0): RankedStock {
  const techScore = technicalScore(stock);
  const overallScore = techScore != null && fundamentalScore != null
    ? fundamentalScore * 0.55 + techScore * 0.45
    : null;
  return {
    ...stock,
    fundamentalScore,
    fundamentalCoverage,
    fundamentalSignalCoverage,
    technicalScore: techScore,
    overallScore,
  };
}

function sectorRead(sector: SectorSummary): string {
  const breadth = sector.advance_pct_1m == null ? '—' : `${sector.advance_pct_1m.toFixed(0)}%`;
  return `${sector.outlook_detail} ${breadth} of stocks with one-month data advanced; ${sector.above_sma200_pct == null ? 'long-term trend data is unavailable' : `${sector.above_sma200_pct.toFixed(0)}% are above their 200-day average`}.`;
}

function ScoreBadge({ value }: { value: number | null }) {
  const rating = value == null ? 'No score' : value >= 75 ? 'Strong' : value >= 60 ? 'Good' : value >= 45 ? 'Mixed' : 'Weak';
  return <span className={`sector-score-badge ${value == null ? 'is-empty' : value >= 65 ? 'is-good' : value >= 45 ? 'is-mid' : 'is-low'}`}>
    {formatScore(value)} <small>{rating}</small>
  </span>;
}

export default function SectorsPage() {
  const [sectors, setSectors] = useState<SectorSummary[]>([]);
  const [asOfDate, setAsOfDate] = useState<string | null>(null);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [stocks, setStocks] = useState<RankedStock[]>([]);
  const [loadingSectors, setLoadingSectors] = useState(true);
  const [loadingStocks, setLoadingStocks] = useState(false);
  const [fundamentalsLoading, setFundamentalsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [stockError, setStockError] = useState<string | null>(null);
  const [sortMode, setSortMode] = useState<SortMode>('overall');
  const [search, setSearch] = useState('');
  const selectedSector = sectors.find((sector) => sector.key === selectedKey) ?? null;

  useEffect(() => {
    let active = true;
    getSectorAnalysis()
      .then((response) => {
        if (!active) return;
        setSectors(response.sectors);
        setAsOfDate(response.as_of_date);
        setSelectedKey((current) => current ?? response.sectors[0]?.key ?? null);
      })
      .catch(() => {
        if (active) setError('Sector history is unavailable. Check the market-data service and refresh the page.');
      })
      .finally(() => {
        if (active) setLoadingSectors(false);
      });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    if (!selectedKey) {
      return;
    }
    let active = true;
    setLoadingStocks(true);
    setFundamentalsLoading(false);
    setStockError(null);
    setStocks([]);
    getSectorStocks(selectedKey)
      .then(async (response) => {
        if (!active) return;
        const members = response.stocks;
        setStocks(members.map((stock) => buildRankedStock(stock, null, 0)));
        setLoadingStocks(false);
        setFundamentalsLoading(true);
        try {
          const batches = Array.from({ length: Math.ceil(members.length / 500) }, (_, index) => members.slice(index * 500, (index + 1) * 500));
          const inputs = await Promise.all(batches.map((batch) => getFundamentalScoreInputs(batch.map((stock) => stock.stock_id))));
          if (!active) return;
          const byId = Object.assign({}, ...inputs);
          setStocks(members.map((stock) => {
            const assessment = assessFundamentals(byId[String(stock.stock_id)] ?? null, selectedSector?.name);
            return buildRankedStock(stock, assessment.score, assessment.coverage, assessment.signalCoverage);
          }));
        } catch {
          if (active) setStockError('Fundamental scores are temporarily unavailable; technical scores are still shown.');
        } finally {
          if (active) setFundamentalsLoading(false);
        }
      })
      .catch(() => {
        if (active) {
          setStockError('Stocks in this sector are unavailable. Check the market-data service and try again.');
          setLoadingStocks(false);
        }
      });
    return () => { active = false; };
  }, [selectedKey, selectedSector?.name]);

  const sortedSectors = useMemo(() => [...sectors].sort((left, right) => {
    const a = left.periods['1m'].return_pct;
    const b = right.periods['1m'].return_pct;
    return (b ?? Number.NEGATIVE_INFINITY) - (a ?? Number.NEGATIVE_INFINITY);
  }), [sectors]);
  const visibleStocks = useMemo(() => {
    const query = search.trim().toLowerCase();
    return stocks
      .filter((stock) => !query || stock.symbol.toLowerCase().includes(query) || stock.name.toLowerCase().includes(query))
      .sort((left, right) => {
        const getValue = (stock: RankedStock) => sortMode === 'overall'
          ? stock.overallScore
          : sortMode === 'technical'
            ? stock.technicalScore
            : sortMode === 'fundamental'
              ? stock.fundamentalScore
              : stock.returns['1m'];
        const a = getValue(left);
        const b = getValue(right);
        if (a == null && b == null) return left.symbol.localeCompare(right.symbol);
        if (a == null) return 1;
        if (b == null) return -1;
        return b - a;
      });
  }, [stocks, search, sortMode]);
  const positiveSectors = sectors.filter((sector) => (sector.periods['1m'].return_pct ?? 0) > 0).length;
  const strongestSector = sortedSectors[0];
  const weakestSector = [...sortedSectors].reverse()[0];

  return (
    <div className="sectors-page">
      <div className="sectors-page-heading">
        <div>
          <div className="sectors-eyebrow"><span className="sectors-eyebrow-dot" /> MARKET MAP <span>/</span> NSE SECTORS</div>
          <h1 className="page-title">Sector pulse</h1>
          <p className="sectors-intro">Compare sector trends across time, then open any sector to rank its stocks by fundamentals and technical alignment.</p>
        </div>
        <div className="sectors-as-of"><span>DAILY CLOSE DATA</span><strong>{asOfDate || 'Waiting for data'}</strong></div>
      </div>

      {error && <div className="sector-error">{error}</div>}

      <section className="sector-stat-grid" aria-label="Sector market summary">
        <article className="sector-stat-card"><span>SECTORS TRACKED</span><strong>{loadingSectors ? '—' : sectors.length}</strong><small>With recent price history</small></article>
        <article className="sector-stat-card"><span>POSITIVE · 1M</span><strong>{loadingSectors ? '—' : `${positiveSectors}/${sectors.length}`}</strong><small>Sector averages above zero</small></article>
        <article className="sector-stat-card"><span>LEADING · 1M</span><strong className={percentClass(strongestSector?.periods['1m'].return_pct)}>{strongestSector ? formatPercent(strongestSector.periods['1m'].return_pct) : '—'}</strong><small>{strongestSector?.name ?? 'No sector data'}</small></article>
        <article className="sector-stat-card"><span>TRAILING · 1M</span><strong className={percentClass(weakestSector?.periods['1m'].return_pct)}>{weakestSector ? formatPercent(weakestSector.periods['1m'].return_pct) : '—'}</strong><small>{weakestSector?.name ?? 'No sector data'}</small></article>
      </section>

      <section className="sector-overview-section">
        <div className="sector-section-heading">
          <div><div className="sector-section-kicker">ROTATION & BREADTH</div><h2>Sector performance</h2></div>
          <span className="sector-sort-hint">Ranked by 1M return · select a row to inspect stocks</span>
        </div>
        <div className="card sector-table-card">
          {loadingSectors ? <div className="sector-loading"><span className="sector-spinner" />Calculating sector history…</div> : sortedSectors.length === 0 ? (
            <div className="sector-empty"><strong>No sector history is available yet.</strong><span>Daily candles and sector classifications are needed to calculate these returns.</span></div>
          ) : (
            <div className="sector-table-scroll">
              <table className="sector-table">
                <thead><tr><th>SECTOR</th><th>STOCKS</th>{PERIODS.map((period) => <th key={period.key}>{period.label}</th>)}<th>ADVANCING · 1M</th><th>READ</th></tr></thead>
                <tbody>{sortedSectors.map((sector, index) => (
                  <tr key={sector.key} className={selectedKey === sector.key ? 'is-selected' : ''} onClick={() => setSelectedKey(sector.key)} onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); setSelectedKey(sector.key); } }} role="button" tabIndex={0} aria-label={`Open ${sector.name} sector analysis`}>
                    <td><div className="sector-name-cell"><span className="sector-rank">{String(index + 1).padStart(2, '0')}</span><span><strong>{sector.name}</strong><small>{sector.outlook}</small></span></div></td>
                    <td><strong>{sector.priced_stock_count}</strong><small className="sector-coverage">/{sector.stock_count}</small></td>
                    {PERIODS.map((period) => <td key={period.key} className={`sector-return ${percentClass(sector.periods[period.key].return_pct)}`}>{formatPercent(sector.periods[period.key].return_pct)}</td>)}
                    <td><span className="sector-breadth-value">{sector.advance_pct_1m == null ? '—' : `${sector.advance_pct_1m.toFixed(0)}%`}</span><span className="sector-breadth-caption">{sector.advancing_1m} up · {sector.declining_1m} down</span></td>
                    <td><span className={`sector-outlook-tag ${sector.outlook === 'Early recovery' || sector.outlook === 'Broadening strength' ? 'is-positive' : sector.outlook === 'Weak trend' ? 'is-negative' : ''}`}>{sector.outlook}</span></td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          )}
        </div>
        <div className="sector-method-note"><span>ⓘ</span>Returns are equal-weight averages of constituent stock returns. Periods use 5, 21, 63, 126 and 252 trading sessions; sample counts can differ when history is incomplete.</div>
      </section>

      {selectedSector && (
        <section className="sector-detail" id="sector-detail">
          <div className="sector-detail-header">
            <div><div className="sector-section-kicker">SECTOR DEEP DIVE</div><h2>{selectedSector.name}<span className={`sector-outlook-tag ${selectedSector.outlook === 'Early recovery' || selectedSector.outlook === 'Broadening strength' ? 'is-positive' : selectedSector.outlook === 'Weak trend' ? 'is-negative' : ''}`}>{selectedSector.outlook}</span></h2><p>{sectorRead(selectedSector)}</p></div>
            <div className="sector-members-count"><strong>{selectedSector.priced_stock_count}</strong><span>priced stocks</span></div>
          </div>

          <div className="sector-detail-metrics">
            <div><span>ADVANCING · 1M</span><strong>{selectedSector.advance_pct_1m == null ? '—' : `${selectedSector.advance_pct_1m.toFixed(0)}%`}</strong></div>
            <div><span>ABOVE 200D AVG</span><strong>{selectedSector.above_sma200_pct == null ? '—' : `${selectedSector.above_sma200_pct.toFixed(0)}%`}</strong></div>
            <div><span>AVERAGE RSI</span><strong>{selectedSector.average_rsi_14 == null ? '—' : selectedSector.average_rsi_14.toFixed(1)}</strong></div>
            <div><span>1Y RETURN COVERAGE</span><strong>{selectedSector.periods['1y'].sample_count} / {selectedSector.priced_stock_count}</strong></div>
          </div>

          <div className="sector-stocks-heading">
            <div><div className="sector-section-kicker">CONSTITUENTS</div><h3>Stocks in {selectedSector.name}</h3><p>Overall rank blends fundamental quality (55%) and technical alignment (45%). Select a score column to sort.</p></div>
            <div className="sector-stock-controls">
              <label className="sector-search"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="11" cy="11" r="8" /><path d="m21 21-4.35-4.35" /></svg><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Filter stocks" aria-label="Filter sector stocks" /></label>
              <label className="sector-sort-select"><span>SORT</span><select value={sortMode} onChange={(event) => setSortMode(event.target.value as SortMode)}><option value="overall">Overall</option><option value="technical">Technical</option><option value="fundamental">Fundamental</option><option value="1m">1M return</option></select></label>
            </div>
          </div>

          {stockError && <div className="sector-stock-notice">{stockError}</div>}
          {fundamentalsLoading && <div className="sector-stock-notice">Refreshing fundamental scores…</div>}
          <div className="card sector-stock-table-card">
            {loadingStocks ? <div className="sector-loading"><span className="sector-spinner" />Loading stocks and analysis…</div> : visibleStocks.length === 0 ? (
              <div className="sector-empty"><strong>{stocks.length === 0 ? 'No priced stocks are available for this sector.' : 'No stocks match this filter.'}</strong><span>Try another search or check back after new daily data arrives.</span></div>
            ) : (
              <div className="sector-table-scroll">
                <table className="sector-table sector-stock-table">
                  <thead><tr><th>#</th><th>STOCK</th><th>PRICE</th><th>OVERALL</th><th>FUNDAMENTAL</th><th>TECHNICAL</th><th>1M</th><th>3M</th><th>RSI · 14</th><th>MARKET CAP</th></tr></thead>
                  <tbody>{visibleStocks.map((stock, index) => (
                    <tr key={stock.stock_id}>
                      <td className="sector-stock-rank">{String(index + 1).padStart(2, '0')}</td>
                      <td><StockIdentity className="sector-stock-link" symbol={stock.symbol} name={stock.name} sector={selectedSector.name} close={stock.latest_close} market_cap_cr={stock.market_cap_cr} as_of_date={stock.latest_at} /></td>
                      <td className="sector-price">₹{stock.latest_close.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</td>
                      <td><ScoreBadge value={stock.overallScore} /></td>
                      <td><ScoreBadge value={stock.fundamentalScore} /><small className="sector-score-coverage">{stock.fundamentalCoverage ? `${stock.fundamentalCoverage}% pillars · ${stock.fundamentalSignalCoverage}% inputs` : ''}</small></td>
                      <td><ScoreBadge value={stock.technicalScore} /></td>
                      <td className={`sector-return ${percentClass(stock.returns['1m'])}`}>{formatPercent(stock.returns['1m'])}</td>
                      <td className={`sector-return ${percentClass(stock.returns['3m'])}`}>{formatPercent(stock.returns['3m'])}</td>
                      <td className="sector-rsi">{stock.technical.rsi_14 == null ? '—' : stock.technical.rsi_14.toFixed(1)}</td>
                      <td className="sector-market-cap">{stock.market_cap_cr == null ? '—' : `₹${stock.market_cap_cr.toLocaleString('en-IN', { maximumFractionDigits: 0 })} Cr`}</td>
                    </tr>
                  ))}</tbody>
                </table>
              </div>
            )}
            {stocks.length > 0 && <div className="sector-table-footer">Showing {visibleStocks.length} of {stocks.length} priced stocks · Fundamentals can be unavailable for some companies.</div>}
          </div>
        </section>
      )}
    </div>
  );
}
