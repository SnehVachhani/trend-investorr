'use client';

import { useEffect, useRef, useState } from 'react';
import type { Stock } from '@/lib/types';
import { searchStocks } from '@/lib/api';
import { getFilterBacktest, getFilterBacktestPresets, runFilterBacktestBatch, runFilterBacktestPreset } from '@/lib/filter-api';
import { usePersistentState } from '@/lib/persistence';
import type { FilterBacktestPreset, FilterBacktestResult } from '@/lib/filter-types';
import FilterBacktestResultDetails from './FilterBacktestResultDetails';
import StockIdentity from './StockIdentity';

type Props = {
  onRun?: (runId: number) => void | Promise<void>;
  onBatchRun?: () => void | Promise<void>;
  loadRunId?: number | null;
};

const ALL_FILTER_PRESETS = '__all__';

type ComparisonRow = {
  preset: FilterBacktestPreset;
  result: FilterBacktestResult | null;
  runId?: number;
  error?: string;
};

function metric(value: number | undefined, suffix = '') {
  return value == null || Number.isNaN(value) ? '—' : `${value.toFixed(2)}${suffix}`;
}

function formatDateInput(date: Date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

function getDefaultDateRange() {
  const end = new Date();
  const start = new Date(end);
  start.setFullYear(start.getFullYear() - 1);
  return {
    start: formatDateInput(start),
    end: formatDateInput(end),
  };
}

function StockSuggestion({ stock, onSelect }: { stock: Stock; onSelect: (stock: Stock) => void }) {
  return (
    <div className="bt-stock-option" role="listitem">
      <div className="bt-stock-option-copy">
        <StockIdentity className="bt-stock-option-identity" symbol={stock.symbol} name={stock.name} company_name={stock.company_name} sector={stock.sector} close={stock.latest_price ?? stock.last_price ?? null} market_cap_cr={stock.market_cap_cr} as_of_date={stock.price_date} />
        <span className="bt-stock-option-name">Sector: {stock.sector || '—'}</span>
      </div>
      {stock.latest_price != null ? <span style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 12, fontWeight: 600, color: 'var(--text-primary)' }}>₹{stock.latest_price.toLocaleString('en-IN', { maximumFractionDigits: 2 })}</span> : <span style={{ fontSize: 11, color: 'var(--text-tertiary)' }}>No data</span>}
      <button type="button" className="bt-stock-option-select" onMouseDown={(event) => event.preventDefault()} onClick={() => onSelect(stock)}>Use stock</button>
    </div>
  );
}

export default function FilterBacktestPanel({ onRun, onBatchRun, loadRunId }: Props) {
  const [presets, setPresets] = useState<FilterBacktestPreset[]>([]);
  const [selectedPreset, setSelectedPreset] = usePersistentState('workspace.filterBacktest.selectedPreset', '');
  const [symbol, setSymbol] = usePersistentState('workspace.filterBacktest.symbol', 'RELIANCE');
  const [selectedStock, setSelectedStock] = useState<Stock | null>(null);
  const [stockResults, setStockResults] = useState<Stock[]>([]);
  const [showStockSuggestions, setShowStockSuggestions] = useState(false);
  const [searchingStocks, setSearchingStocks] = useState(false);
  const defaultDateRange = getDefaultDateRange();
  const [startDate, setStartDate] = usePersistentState('workspace.filterBacktest.startDate', defaultDateRange.start);
  const [endDate, setEndDate] = usePersistentState('workspace.filterBacktest.endDate', defaultDateRange.end);
  const [capital, setCapital] = usePersistentState('workspace.filterBacktest.capital', '100000');
  const [result, setResult] = usePersistentState<FilterBacktestResult | null>('workspace.filterBacktest.result', null);
  const [comparisonRows, setComparisonRows] = usePersistentState<ComparisonRow[]>('workspace.filterBacktest.comparisonRows', []);
  const [selectedComparisonRow, setSelectedComparisonRow] = useState<ComparisonRow | null>(null);
  const [tradeDetailsLoading, setTradeDetailsLoading] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadedRunId, setLoadedRunId] = useState<number | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState('');
  const searchTimeout = useRef<ReturnType<typeof setTimeout> | null>(null);
  const searchRequest = useRef(0);
  const tradeDetailsRequest = useRef(0);

  useEffect(() => {
    getFilterBacktestPresets()
      .then((items) => {
        setPresets(items);
        if (items.length > 0) {
          setSelectedPreset((current) => current || items[0].preset_id);
        }
      })
      .catch((err: Error) => setError(err.message))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => () => {
    if (searchTimeout.current) clearTimeout(searchTimeout.current);
  }, []);

  useEffect(() => {
    if (loadRunId == null) return;
    let cancelled = false;
    getFilterBacktest(loadRunId)
      .then(({ run }) => {
        if (cancelled) return;
        setResult(run.result);
        setComparisonRows([]);
        setSelectedComparisonRow(null);
        setSelectedPreset(run.preset_id || '');
        setSymbol(run.symbol);
        setSelectedStock(null);
        setStartDate(run.start_date.slice(0, 10));
        setEndDate(run.end_date.slice(0, 10));
        const config = run.config;
        if (typeof config.initial_capital === 'number') setCapital(String(config.initial_capital));
        setLoadedRunId(loadRunId);
      })
      .catch((err: Error) => {
        if (!cancelled) {
          setError(err.message);
          setLoadedRunId(loadRunId);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [loadRunId]);


  const selected = presets.find((preset) => preset.preset_id === selectedPreset);
  const isAllSelected = selectedPreset === ALL_FILTER_PRESETS;
  const sortedComparisonRows = [...comparisonRows].sort((left, right) => {
    if (!left.result && !right.result) return 0;
    if (!left.result) return 1;
    if (!right.result) return -1;
    return right.result.total_return_pct - left.result.total_return_pct;
  });

  const openComparisonRow = async (row: ComparisonRow) => {
    if (!row.result) return;
    const requestId = ++tradeDetailsRequest.current;
    setSelectedComparisonRow(row);
    setResult(row.result);
    setTradeDetailsLoading(Boolean(row.runId));
    if (!row.runId) return;
    try {
      const response = await getFilterBacktest(row.runId);
      if (requestId === tradeDetailsRequest.current) setResult(response.run.result);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to load trade details');
    } finally {
      if (requestId === tradeDetailsRequest.current) setTradeDetailsLoading(false);
    }
  };

  const isComparisonModalOpen = isAllSelected && selectedComparisonRow != null && result != null;

  const closeComparisonModal = () => {
    tradeDetailsRequest.current += 1;
    setSelectedComparisonRow(null);
    setResult(null);
    setTradeDetailsLoading(false);
  };

  useEffect(() => {
    if (!isComparisonModalOpen) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') closeComparisonModal();
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [isComparisonModalOpen]);

  const selectPreset = (preset: FilterBacktestPreset) => {
    setSelectedPreset(preset.preset_id);
    setComparisonRows([]);
    setSelectedComparisonRow(null);
    setResult(null);
  };

  const selectAllPresets = () => {
    setSelectedPreset(ALL_FILTER_PRESETS);
    setComparisonRows([]);
    setSelectedComparisonRow(null);
    setResult(null);
  };

  const handleStockSearch = (value: string) => {
    searchRequest.current += 1;
    setSymbol(value);
    setSelectedStock(null);
    setStockResults([]);
    if (searchTimeout.current) clearTimeout(searchTimeout.current);

    const query = value.trim();
    if (query.length < 2) {
      setSearchingStocks(false);
      setShowStockSuggestions(false);
      return;
    }

    const requestId = searchRequest.current;
    setSearchingStocks(true);
    setShowStockSuggestions(true);
    searchTimeout.current = setTimeout(async () => {
      try {
        const results = await searchStocks(query, 8);
        if (requestId === searchRequest.current) setStockResults(results);
      } catch {
        if (requestId === searchRequest.current) setStockResults([]);
      } finally {
        if (requestId === searchRequest.current) setSearchingStocks(false);
      }
    }, 250);
  };

  const selectStock = (stock: Stock) => {
    searchRequest.current += 1;
    if (searchTimeout.current) clearTimeout(searchTimeout.current);
    setSearchingStocks(false);
    setSymbol(stock.symbol.replace(/-EQ$/, ''));
    setSelectedStock(stock);
    setShowStockSuggestions(false);
  };

  const run = async () => {
    if (!selectedPreset || !symbol.trim()) return;
    const targetPresets = isAllSelected
      ? presets
      : presets.filter((preset) => preset.preset_id === selectedPreset);
    if (targetPresets.length === 0) {
      setError('No filter combinations are available to run.');
      return;
    }
    const initialCapital = Number(capital);
    if (!Number.isFinite(initialCapital) || initialCapital <= 0) {
      setError('Initial capital must be greater than 0.');
      return;
    }
    setRunning(true);
    setError('');
    setResult(null);
    setComparisonRows([]);
    setSelectedComparisonRow(null);
    try {
      if (isAllSelected) {
        const response = await runFilterBacktestBatch(
          targetPresets.map((preset) => preset.preset_id),
          {
            symbol: symbol.trim(),
            start_date: startDate,
            end_date: endDate,
            initial_capital: initialCapital,
          },
        );
        const rows: ComparisonRow[] = response.results.flatMap((item) => {
          const preset = targetPresets.find((candidate) => candidate.preset_id === item.preset_id);
          return preset ? [{ preset, result: item.result, runId: item.run_id ?? undefined, error: item.error }] : [];
        });
        setComparisonRows(rows);
        setSelectedComparisonRow(null);
        const failedCount = rows.filter((row) => !row.result).length;
        if (failedCount > 0) {
          setError(`${targetPresets.length - failedCount} of ${targetPresets.length} combinations completed. Failed rows are marked in the table.`);
        }
        await onBatchRun?.();
      } else {
        const response = await runFilterBacktestPreset(selectedPreset, {
          symbol: symbol.trim(),
          start_date: startDate,
          end_date: endDate,
          initial_capital: initialCapital,
        });
        setResult(response.result);
        await onRun?.(response.run_id);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Filter backtest failed');
    } finally {
      setRunning(false);
    }
  };

  if (loading) return <div className="bt-processing"><div className="bt-processing-spinner" /><div className="bt-processing-text">Loading filter combinations…</div></div>;
  if (loadRunId != null && loadedRunId !== loadRunId) return <div className="bt-processing"><div className="bt-processing-spinner" /><div className="bt-processing-text">Loading saved filter backtest…</div></div>;

  return (
    <div className="bt-filter-panel">
      <aside className="bt-filter-sidebar" aria-label="Filter combinations">
        <div className="bt-filter-sidebar-header">
          <span className="bt-filter-sidebar-title">Filter combinations</span>
          <span className="bt-filter-sidebar-count">{presets.length}</span>
        </div>
        <div className="bt-filter-preset-list">
          <button
            type="button"
            className={`bt-filter-preset bt-filter-preset-all ${isAllSelected ? 'is-selected' : ''}`}
            onClick={selectAllPresets}
            aria-pressed={isAllSelected}
          >
            <div className="bt-filter-preset-heading">
              <strong>All filter combinations</strong>
              <span className="badge badge-paper">{presets.length} setups</span>
            </div>
            <p>Run every setup with the same stock, date range, and risk settings.</p>
            <small>Results are ranked together in a comparison table.</small>
          </button>
          {presets.map((preset) => (
            <button
              key={preset.preset_id}
              type="button"
              className={`bt-filter-preset ${selectedPreset === preset.preset_id ? 'is-selected' : ''}`}
              onClick={() => selectPreset(preset)}
              aria-pressed={selectedPreset === preset.preset_id}
            >
              <div className="bt-filter-preset-heading">
                <strong>{preset.name}</strong>
                <span className="badge">{preset.category}</span>
              </div>
              <p>{preset.description}</p>
              <small>{preset.timeframe} · Typical holding period {preset.holding_period}</small>
            </button>
          ))}
        </div>
      </aside>

      <div className="bt-filter-content">
      <div className="bt-form-container" style={{ maxWidth: 900, margin: '0 auto' }}>
        {error && <div className="bt-error">{error}</div>}

        {isAllSelected ? (
          <div className="card bt-selection-card">
            <h2 className="section-title">All filter combinations</h2>
            <p>{presets.length} setups will run with the same inputs. The comparison table will rank each combination by total return.</p>
          </div>
        ) : selected ? (
          <div className="card bt-selection-card">
            <h2 className="section-title">Selected combination: {selected.name}</h2>
            <p>{selected.description}</p>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 12 }}>{selected.expression.children.map((child, index) => child.type === 'condition' ? <span className="badge" key={`${child.filter_id}-${index}`}>{child.filter_id}</span> : <span className="badge" key={index}>{child.operator}</span>)}</div>
          </div>
        ) : null}

        <div className="bt-form card">
          <div className="bt-field"><label className="bt-field-label" htmlFor="filter-bt-symbol">Stock symbol or name</label><div className="bt-stock-search"><input id="filter-bt-symbol" className="bt-field-input" value={symbol} onChange={(event) => handleStockSearch(event.target.value)} onFocus={() => stockResults.length > 0 && setShowStockSuggestions(true)} onBlur={() => setTimeout(() => setShowStockSuggestions(false), 200)} placeholder="Search stocks... (e.g. RELIANCE)" autoComplete="off" role="combobox" aria-autocomplete="list" aria-expanded={showStockSuggestions} aria-controls="filter-bt-stock-suggestions" />{selectedStock && <small style={{ display: 'flex', alignItems: 'center', gap: 5, marginTop: 4, color: 'var(--text-secondary)' }}>Selected: <StockIdentity symbol={selectedStock.symbol} name={selectedStock.name} company_name={selectedStock.company_name} sector={selectedStock.sector} close={selectedStock.latest_price ?? selectedStock.last_price ?? null} market_cap_cr={selectedStock.market_cap_cr} as_of_date={selectedStock.price_date} /> · Sector: {selectedStock.sector || '—'}</small>}{showStockSuggestions && <div id="filter-bt-stock-suggestions" className="bt-stock-results" role="list">{searchingStocks ? <div className="bt-stock-empty">Searching stocks…</div> : stockResults.length > 0 ? stockResults.map((stock) => <StockSuggestion key={stock.id} stock={stock} onSelect={selectStock} />) : <div className="bt-stock-empty">No matching stocks found</div>}</div>}</div></div>
          <div className="bt-date-row"><div className="bt-field"><label className="bt-field-label" htmlFor="filter-bt-start">Start date</label><input id="filter-bt-start" type="date" className="bt-field-input" value={startDate} onChange={(event) => setStartDate(event.target.value)} /></div><div className="bt-field"><label className="bt-field-label" htmlFor="filter-bt-end">End date</label><input id="filter-bt-end" type="date" className="bt-field-input" value={endDate} onChange={(event) => setEndDate(event.target.value)} /></div></div>
          <div className="bt-field"><label className="bt-field-label" htmlFor="filter-bt-capital">Initial capital (₹)</label><input id="filter-bt-capital" type="number" className="bt-field-input" value={capital} onChange={(event) => setCapital(event.target.value)} /></div>
          <button className="bt-run-btn" onClick={run} disabled={running || !selectedPreset || !symbol.trim()}>{running ? (isAllSelected ? `Running all ${presets.length}…` : 'Running…') : isAllSelected ? `Run all ${presets.length} combinations` : 'Run filter backtest'}</button>
        </div>

        {isAllSelected && comparisonRows.length > 0 && (
          <section className="bt-comparison-section" aria-labelledby="filter-comparison-results">
            <div className="bt-comparison-header">
            <div>
              <h2 id="filter-comparison-results" className="bt-comparison-title">Filter combination results</h2>
                <p className="bt-comparison-subtitle">Sorted by total return for {symbol.trim().toUpperCase()}. Click a completed row to view its trades.</p>
              </div>
              <span className="bt-filter-sidebar-count">{comparisonRows.filter((row) => row.result).length}/{comparisonRows.length} complete</span>
            </div>
            <div className="bt-comparison-table-wrap">
              <table className="data-table bt-comparison-table">
                <thead>
                  <tr>
                    <th>Combination</th>
                    <th>Category</th>
                    <th>Total return</th>
                    <th>Final capital</th>
                    <th>Charges</th>
                    <th>Win rate</th>
                    <th>Trades</th>
                    <th>Drawdown</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {sortedComparisonRows.map((row) => {
                    const returnValue = row.result?.total_return_pct;
                    const isOpen = selectedComparisonRow?.preset.preset_id === row.preset.preset_id;
                    const canOpen = Boolean(row.result);
                    return (
                      <tr
                        key={row.preset.preset_id}
                        className={canOpen ? `bt-comparison-row${isOpen ? ' is-open' : ''}` : undefined}
                        role={canOpen ? 'button' : undefined}
                        tabIndex={canOpen ? 0 : -1}
                        onClick={() => openComparisonRow(row)}
                        onKeyDown={(event) => {
                          if (canOpen && (event.key === 'Enter' || event.key === ' ')) {
                            event.preventDefault();
                            openComparisonRow(row);
                          }
                        }}
                      >
                        <td className="bt-comparison-name"><span>{row.preset.name}</span>{canOpen && <small className="bt-comparison-open-hint">View trades</small>}</td>
                        <td><span className="badge">{row.preset.category}</span></td>
                        <td className={returnValue != null && returnValue >= 0 ? 'bt-comparison-positive' : 'bt-comparison-negative'}>{metric(returnValue, '%')}</td>
                        <td>{row.result ? `₹${row.result.final_capital.toLocaleString('en-IN', { maximumFractionDigits: 0 })}` : '—'}</td>
                        <td>{row.result ? `₹${(row.result.total_charges ?? 0).toLocaleString('en-IN', { maximumFractionDigits: 0 })}` : '—'}</td>
                        <td>{metric(row.result?.win_rate_pct, '%')}</td>
                        <td>{row.result?.total_trades ?? '—'}</td>
                        <td>{metric(row.result?.max_drawdown_pct, '%')}</td>
                        <td><span className={`badge ${row.result ? 'badge-buy' : 'badge-sell'}`}>{row.result ? 'Complete' : 'Failed'}</span></td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </section>
        )}

        {result && (
          <div
            className={isComparisonModalOpen ? 'bt-comparison-modal-backdrop' : undefined}
            role={isComparisonModalOpen ? 'presentation' : undefined}
            onClick={isComparisonModalOpen ? closeComparisonModal : undefined}
          >
            <div
              className={isComparisonModalOpen ? 'bt-comparison-modal' : undefined}
              role={isComparisonModalOpen ? 'dialog' : undefined}
              aria-modal={isComparisonModalOpen ? 'true' : undefined}
              aria-labelledby={isComparisonModalOpen ? 'comparison-modal-title' : undefined}
              onClick={(event) => event.stopPropagation()}
            >
              {isComparisonModalOpen && selectedComparisonRow && (
                <div className="bt-comparison-modal-header">
                  <div>
                    <span className="bt-comparison-detail-label">Selected combination</span>
                    <h2 id="comparison-modal-title" className="bt-comparison-detail-title">{selectedComparisonRow.preset.name}</h2>
                    <p className="bt-comparison-subtitle">Trade details for {result.symbol}.</p>
                  </div>
                  <button type="button" className="btn btn-secondary bt-comparison-close" onClick={closeComparisonModal}>Close</button>
                </div>
              )}
              <FilterBacktestResultDetails result={result} loading={tradeDetailsLoading} />
            </div>
          </div>
        )}
      </div>
      </div>
    </div>
  );
}
