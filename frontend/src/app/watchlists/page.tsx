'use client';

import { useEffect, useState, useRef, useCallback } from 'react';
import { useRouter } from 'next/navigation';
import {
  getWatchlists,
  getWatchlistStocks,
  createWatchlist,
  deleteWatchlist,
  addStockToWatchlist,
  removeStockFromWatchlist,
  populateWatchlist,
  getWatchlistPrices,
  reorderWatchlistStocks,
  getWatchlistChanges,
  searchStocks,
} from '@/lib/api';
import type { LivePrice, StockChange } from '@/lib/api';
import type { Watchlist, Stock } from '@/lib/types';

// ── Debounce utility ────────────────────────────────────────
function useDebounce(value: string, delay: number) {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(t);
  }, [value, delay]);
  return debounced;
}

// ── Create Watchlist Modal ──────────────────────────────────
function CreateModal({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: () => void;
}) {
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [listType, setListType] = useState('custom');
  const [maxStocks, setMaxStocks] = useState(500);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) {
      setName('');
      setDescription('');
      setListType('custom');
      setMaxStocks(500);
      setError('');
      setTimeout(() => inputRef.current?.focus(), 100);
    }
  }, [open]);

  if (!open) return null;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = name.trim();
    if (!trimmed) {
      setError('Watchlist name is required');
      return;
    }
    setLoading(true);
    setError('');
    try {
      await createWatchlist({
        name: trimmed,
        description: description.trim(),
        list_type: listType,
        max_stocks: maxStocks,
      });
      onCreated();
      onClose();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to create watchlist';
      setError(msg);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="wl-modal-overlay" onClick={onClose}>
      <div className="wl-modal" onClick={(e) => e.stopPropagation()}>
        <div className="wl-modal-header">
          <h2 className="wl-modal-title">Create Watchlist</h2>
          <button className="wl-modal-close" onClick={onClose} aria-label="Close">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>

        <form onSubmit={handleSubmit} className="wl-form">
          <div className="wl-field">
            <label className="wl-field-label">Name *</label>
            <input
              ref={inputRef}
              className="wl-field-input"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Momentum Picks"
              maxLength={64}
              id="wl-create-name"
            />
          </div>

          <div className="wl-field">
            <label className="wl-field-label">Description</label>
            <input
              className="wl-field-input"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Short description (optional)"
              maxLength={200}
              id="wl-create-desc"
            />
          </div>

          <div className="wl-field-row">
            <div className="wl-field">
              <label className="wl-field-label">Type</label>
              <select
                className="wl-field-select"
                value={listType}
                onChange={(e) => setListType(e.target.value)}
                id="wl-create-type"
              >
                <option value="custom">Custom</option>
                <option value="index">Index</option>
                <option value="sector">Sector</option>
                <option value="scanner">Scanner</option>
              </select>
            </div>
            <div className="wl-field">
              <label className="wl-field-label">Max Stocks</label>
              <input
                className="wl-field-input"
                type="number"
                min={1}
                max={5000}
                value={maxStocks}
                onChange={(e) => setMaxStocks(Number(e.target.value) || 500)}
                id="wl-create-max"
              />
            </div>
          </div>

          {error && <div className="wl-error">{error}</div>}

          <button type="submit" className="wl-submit-btn" disabled={loading} id="wl-create-submit">
            {loading ? (
              <span className="wl-btn-loading">
                <span className="wl-spinner" />
                Creating…
              </span>
            ) : (
              <>
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                  <line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" />
                </svg>
                Create Watchlist
              </>
            )}
          </button>
        </form>
      </div>
    </div>
  );
}

// ── Delete Confirmation Modal ───────────────────────────────
function DeleteModal({
  watchlist,
  onClose,
  onDeleted,
}: {
  watchlist: Watchlist;
  onClose: () => void;
  onDeleted: () => void;
}) {
  const [loading, setLoading] = useState(false);

  const handleDelete = async () => {
    setLoading(true);
    try {
      await deleteWatchlist(watchlist.id);
      onDeleted();
      onClose();
    } catch {
      // silently ignore — the watchlist may already be deleted
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="wl-modal-overlay" onClick={onClose}>
      <div className="wl-modal wl-modal-sm" onClick={(e) => e.stopPropagation()}>
        <div className="wl-modal-header">
          <h2 className="wl-modal-title">Delete Watchlist</h2>
          <button className="wl-modal-close" onClick={onClose} aria-label="Close">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>
        <div className="wl-delete-body">
          <div className="wl-delete-icon">🗑️</div>
          <p className="wl-delete-text">
            Are you sure you want to delete <strong>{watchlist.name}</strong>?
          </p>
          <p className="wl-delete-sub">
            This will remove the watchlist and all {watchlist.stock_count} stock(s) in it.
            This action cannot be undone.
          </p>
        </div>
        <div className="wl-delete-actions">
          <button className="wl-cancel-btn" onClick={onClose} disabled={loading}>
            Cancel
          </button>
          <button
            className="wl-danger-btn"
            onClick={handleDelete}
            disabled={loading}
            id="wl-delete-confirm"
          >
            {loading ? 'Deleting…' : 'Delete Watchlist'}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Add Stock Search Panel ──────────────────────────────────
function AddStockPanel({
  watchlistId,
  existingStockIds,
  onAdded,
}: {
  watchlistId: number;
  existingStockIds: Set<number>;
  onAdded: () => void;
}) {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<Stock[]>([]);
  const [searching, setSearching] = useState(false);
  const [addingId, setAddingId] = useState<number | null>(null);
  const debouncedQuery = useDebounce(query, 300);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!debouncedQuery.trim()) {
      setResults([]);
      return;
    }
    setSearching(true);
    searchStocks(debouncedQuery, 15)
      .then(setResults)
      .catch(() => setResults([]))
      .finally(() => setSearching(false));
  }, [debouncedQuery]);

  const handleAdd = async (stock: Stock) => {
    setAddingId(stock.id);
    try {
      await addStockToWatchlist(watchlistId, stock.id);
      onAdded();
      // Remove from results
      setResults((prev) => prev.filter((s) => s.id !== stock.id));
    } catch {
      // ignore duplicate errors
    } finally {
      setAddingId(null);
    }
  };

  return (
    <div className="wl-add-stock-panel">
      <div className="wl-add-stock-search">
        <svg className="wl-add-stock-search-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" />
        </svg>
        <input
          ref={inputRef}
          className="wl-add-stock-input"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search stocks to add… (e.g. RELIANCE, TCS)"
          autoFocus
          id="wl-add-stock-search"
        />
        {searching && <span className="wl-spinner wl-spinner-sm" />}
      </div>

      {results.length > 0 && (
        <div className="wl-add-stock-results">
          {results.map((stock) => {
            const alreadyAdded = existingStockIds.has(stock.id);
            return (
              <div key={stock.id} className="wl-add-stock-item">
                <div className="wl-add-stock-info">
                  <span className="wl-add-stock-sym">{stock.symbol.replace(/-EQ$/, '')}</span>
                  <span className="wl-add-stock-name">{stock.name}</span>
                </div>
                {alreadyAdded ? (
                  <span className="wl-added-badge">Added</span>
                ) : (
                  <button
                    className="wl-add-stock-btn"
                    onClick={() => handleAdd(stock)}
                    disabled={addingId === stock.id}
                    aria-label={`Add ${stock.symbol}`}
                  >
                    {addingId === stock.id ? (
                      <span className="wl-spinner wl-spinner-xs" />
                    ) : (
                      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                        <line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" />
                      </svg>
                    )}
                  </button>
                )}
              </div>
            );
          })}
        </div>
      )}

      {debouncedQuery.trim() && !searching && results.length === 0 && (
        <div className="wl-add-stock-empty">
          No stocks found for &ldquo;{debouncedQuery}&rdquo;
        </div>
      )}
    </div>
  );
}

// ── Price Changes Modal ─────────────────────────────────────
const CHANGE_PERIODS = ['1d', '5d', '10d', '20d', '30d', '60d', '90d', '180d', '1y', '3y'] as const;
type Period = typeof CHANGE_PERIODS[number];

function ChangesModal({
  open,
  onClose,
  watchlistId,
  watchlistName,
}: {
  open: boolean;
  onClose: () => void;
  watchlistId: number;
  watchlistName: string;
}) {
  const [data, setData] = useState<StockChange[]>([]);
  const [loading, setLoading] = useState(false);
  const [sortCol, setSortCol] = useState<Period>('10d');
  const [sortAsc, setSortAsc] = useState(false);

  useEffect(() => {
    if (!open) return;
    setLoading(true);
    getWatchlistChanges(watchlistId)
      .then(setData)
      .catch(() => setData([]))
      .finally(() => setLoading(false));
  }, [open, watchlistId]);

  if (!open) return null;

  const handleSort = (col: Period) => {
    if (sortCol === col) {
      setSortAsc(!sortAsc);
    } else {
      setSortCol(col);
      setSortAsc(false);
    }
  };

  const sorted = [...data].sort((a, b) => {
    const av = a[sortCol] ?? -9999;
    const bv = b[sortCol] ?? -9999;
    return sortAsc ? av - bv : bv - av;
  });

  const fmtPct = (v: number | null) => {
    if (v == null) return '-';
    const sign = v >= 0 ? '+' : '';
    return `${sign}${v.toFixed(2)}%`;
  };

  const pctColor = (v: number | null) => {
    if (v == null) return 'var(--text-tertiary)';
    return v >= 0 ? '#10b981' : '#ef4444';
  };

  return (
    <div className="wl-modal-overlay" onClick={onClose}>
      <div
        className="wl-modal changes-modal"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="wl-modal-header">
          <h3 className="wl-modal-title">{watchlistName} — Price Changes</h3>
          <button className="wl-modal-close" onClick={onClose}>
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>
        <div className="changes-body">
          {loading ? (
            <div className="skeleton" style={{ width: '100%', height: 300, borderRadius: 10 }} />
          ) : data.length === 0 ? (
            <div className="empty-state">
              <div className="empty-state-text">No candle data available for this watchlist</div>
            </div>
          ) : (
            <div className="changes-table-wrap">
              <table className="data-table changes-table" id="wl-changes-table">
                <thead>
                  <tr>
                    <th className="changes-th-name">Stock</th>
                    <th className="changes-th-price">Close</th>
                    {CHANGE_PERIODS.map((p) => (
                      <th
                        key={p}
                        className={`changes-th-sort ${sortCol === p ? 'active' : ''}`}
                        onClick={() => handleSort(p)}
                      >
                        <span>{p.toUpperCase()}</span>
                        {sortCol === p && (
                          <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
                            {sortAsc
                              ? <polyline points="18 15 12 9 6 15" />
                              : <polyline points="6 9 12 15 18 9" />
                            }
                          </svg>
                        )}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {sorted.map((s) => (
                    <tr key={s.stock_id}>
                      <td className="changes-td-name">
                        {s.name || s.symbol.replace(/-EQ$|-SM$|-BE$|-BZ$|-ST$/, '')}
                      </td>
                      <td className="changes-td-price">
                        {s.close ? `₹${s.close.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : '-'}
                      </td>
                      {CHANGE_PERIODS.map((p) => (
                        <td
                          key={p}
                          className="changes-td-pct"
                          style={{ color: pctColor(s[p]) }}
                        >
                          {fmtPct(s[p])}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ══════════════════════════════════════════════════════════════
// MAIN PAGE
// ══════════════════════════════════════════════════════════════

export default function WatchlistsPage() {
  const router = useRouter();
  const [watchlists, setWatchlists] = useState<Watchlist[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [stocks, setStocks] = useState<Stock[]>([]);
  const [loading, setLoading] = useState(true);
  const [stocksLoading, setStocksLoading] = useState(false);

  // Modals
  const [showCreate, setShowCreate] = useState(false);
  const [deletingWl, setDeletingWl] = useState<Watchlist | null>(null);
  const [showAddStock, setShowAddStock] = useState(false);
  const [removingStockId, setRemovingStockId] = useState<number | null>(null);
  const [populating, setPopulating] = useState(false);
  const [editMode, setEditMode] = useState(false);
  const [reordering, setReordering] = useState(false);
  const [showChanges, setShowChanges] = useState(false);

  // Live prices
  const [prices, setPrices] = useState<Record<string, LivePrice>>({});

  // ── Load watchlists ───────────────────────────────────
  const loadWatchlists = useCallback(async () => {
    try {
      const data = await getWatchlists();
      setWatchlists(data);
    } catch {
      // ignore
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadWatchlists();
  }, [loadWatchlists]);

  // ── Load stocks for selected watchlist ────────────────
  const loadStocks = useCallback(async (wlId: number) => {
    setStocksLoading(true);
    try {
      const data = await getWatchlistStocks(wlId);
      setStocks(data);
    } catch {
      setStocks([]);
    } finally {
      setStocksLoading(false);
    }
  }, []);

  useEffect(() => {
    if (selectedId != null) {
      loadStocks(selectedId);
    }
  }, [selectedId, loadStocks]);

  // ── Live price polling ────────────────────────────────
  useEffect(() => {
    if (selectedId == null || stocks.length === 0) return;

    let cancelled = false;

    const fetchPrices = async () => {
      try {
        const data = await getWatchlistPrices(selectedId);
        if (!cancelled) setPrices(data);
      } catch {
        // silent
      }
    };

    // Fetch immediately
    fetchPrices();

    // Check if market is open (Mon-Fri, 9:15-15:30 IST)
    const isMarketOpen = () => {
      const now = new Date();
      // Convert to IST
      const istOffset = 5.5 * 60;
      const utcMs = now.getTime() + now.getTimezoneOffset() * 60000;
      const ist = new Date(utcMs + istOffset * 60000);
      const day = ist.getDay();
      if (day === 0 || day === 6) return false;
      const hhmm = ist.getHours() * 100 + ist.getMinutes();
      return hhmm >= 915 && hhmm <= 1530;
    };

    // Poll every 3s during market hours, 30s otherwise
    const interval = setInterval(
      fetchPrices,
      isMarketOpen() ? 3000 : 30000,
    );

    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [selectedId, stocks.length]);

  // ── Auto-select first watchlist ───────────────────────
  useEffect(() => {
    if (!loading && watchlists.length > 0 && selectedId == null) {
      setSelectedId(watchlists[0].id);
    }
  }, [loading, watchlists, selectedId]);

  // ── Handlers ──────────────────────────────────────────
  const handleCreated = () => {
    loadWatchlists();
  };

  const handleDeleted = () => {
    if (selectedId === deletingWl?.id) {
      setSelectedId(null);
      setStocks([]);
    }
    setDeletingWl(null);
    loadWatchlists();
  };

  const handleStockAdded = () => {
    if (selectedId != null) loadStocks(selectedId);
    loadWatchlists(); // refresh counts
  };

  const handleRemoveStock = async (stockId: number) => {
    if (selectedId == null) return;
    setRemovingStockId(stockId);
    try {
      await removeStockFromWatchlist(selectedId, stockId);
      setStocks((prev) => prev.filter((s) => s.id !== stockId));
      loadWatchlists(); // refresh counts
    } catch {
      // ignore
    } finally {
      setRemovingStockId(null);
    }
  };

  const handleMoveStock = async (idx: number, direction: 'up' | 'down') => {
    if (selectedId == null) return;
    const newStocks = [...stocks];
    const swapIdx = direction === 'up' ? idx - 1 : idx + 1;
    if (swapIdx < 0 || swapIdx >= newStocks.length) return;
    [newStocks[idx], newStocks[swapIdx]] = [newStocks[swapIdx], newStocks[idx]];
    setStocks(newStocks);
    setReordering(true);
    try {
      await reorderWatchlistStocks(selectedId, newStocks.map((s) => s.id));
    } catch {
      // revert
      setStocks(stocks);
    } finally {
      setReordering(false);
    }
  };

  const handlePopulate = async (method: 'volume' | 'all_active') => {
    if (selectedId == null) return;
    setPopulating(true);
    try {
      await populateWatchlist(selectedId, method, 500);
      loadStocks(selectedId);
      loadWatchlists();
    } catch {
      // ignore
    } finally {
      setPopulating(false);
    }
  };

  const selectedWl = watchlists.find((wl) => wl.id === selectedId) ?? null;
  const existingStockIds = new Set(stocks.map((s) => s.id));

  // ── Loading state ─────────────────────────────────────
  if (loading) {
    return (
      <div>
        <h1 className="page-title">Watchlists</h1>
        <div className="skeleton" style={{ width: '100%', height: 400, borderRadius: 14 }} />
      </div>
    );
  }

  return (
    <div>
      {/* ── Page Header ──────────────────────────────────── */}
      <div className="wl-page-header">
        <div>
          <h1 className="page-title" style={{ marginBottom: 4 }}>Watchlists</h1>
          <p className="wl-page-subtitle">
            {watchlists.length} watchlist{watchlists.length !== 1 ? 's' : ''} ·{' '}
            {watchlists.reduce((acc, wl) => acc + wl.stock_count, 0)} total stocks
          </p>
        </div>
        <button className="wl-create-btn" onClick={() => setShowCreate(true)} id="wl-create-btn">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" />
          </svg>
          New Watchlist
        </button>
      </div>

      {/* ── Main Grid ────────────────────────────────────── */}
      <div className="wl-layout">
        {/* ── Sidebar: Watchlist List ── */}
        <div className="wl-sidebar">
          <div className="wl-sidebar-header">
            <span className="wl-sidebar-title">Your Lists</span>
            <span className="wl-sidebar-count">{watchlists.length}</span>
          </div>

          <div className="wl-sidebar-list">
            {watchlists.length === 0 ? (
              <div className="wl-sidebar-empty">
                <div style={{ fontSize: 28, opacity: 0.2, marginBottom: 8 }}>📋</div>
                <div style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>No watchlists yet</div>
              </div>
            ) : (
              watchlists.map((wl) => (
                <div
                  key={wl.id}
                  className={`wl-sidebar-item ${selectedId === wl.id ? 'active' : ''}`}
                  onClick={() => {
                    setSelectedId(wl.id);
                    setShowAddStock(false);
                    setEditMode(false);
                  }}
                >
                  <div className="wl-sidebar-item-main">
                    <div className="wl-sidebar-item-name">{wl.name}</div>
                    <div className="wl-sidebar-item-meta">
                      <span className="wl-sidebar-item-count">{wl.stock_count} stocks</span>
                      <span className="wl-sidebar-item-type">{wl.list_type}</span>
                    </div>
                  </div>
                </div>
              ))
            )}
          </div>
        </div>

        {/* ── Main Panel ── */}
        <div className="wl-main">
          {selectedId == null || selectedWl == null ? (
            <div className="empty-state">
              <div className="empty-state-icon">📋</div>
              <div className="empty-state-text">
                {watchlists.length === 0
                  ? 'Create your first watchlist to get started'
                  : 'Select a watchlist to view its stocks'}
              </div>
              {watchlists.length === 0 && (
                <button
                  className="wl-create-btn"
                  style={{ marginTop: 16 }}
                  onClick={() => setShowCreate(true)}
                >
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                    <line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" />
                  </svg>
                  New Watchlist
                </button>
              )}
            </div>
          ) : (
            <>
              {/* ── Watchlist Header ── */}
              <div className="wl-main-header">
                <div>
                  <h2 className="wl-main-title">{selectedWl.name}</h2>
                  {selectedWl.description && (
                    <p className="wl-main-desc">{selectedWl.description}</p>
                  )}
                  <div className="wl-main-badges">
                    <span className="badge badge-paper">{selectedWl.list_type}</span>
                    <span className="wl-stock-count-badge">
                      {selectedWl.stock_count} / {selectedWl.max_stocks} stocks
                    </span>
                  </div>
                </div>
                <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                  {editMode && (
                    <button
                      className="wl-edit-action-btn wl-delete-wl-btn"
                      onClick={() => setDeletingWl(selectedWl)}
                      title="Delete this watchlist"
                    >
                      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                        <polyline points="3 6 5 6 21 6" />
                        <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                      </svg>
                    </button>
                  )}
                  <button
                    className={`wl-edit-toggle ${editMode ? 'active' : ''}`}
                    onClick={() => { setEditMode(!editMode); setShowAddStock(false); }}
                    id="wl-edit-toggle"
                  >
                    {editMode ? (
                      <>
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                          <polyline points="20 6 9 17 4 12" />
                        </svg>
                        Done
                      </>
                    ) : (
                      <>
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                          <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" />
                          <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" />
                        </svg>
                        Edit
                      </>
                    )}
                  </button>
                </div>
              </div>

              {/* ── Add Stock Panel (only in edit mode) ── */}
              {editMode && showAddStock && (
                <AddStockPanel
                  watchlistId={selectedId}
                  existingStockIds={existingStockIds}
                  onAdded={handleStockAdded}
                />
              )}

              {/* ── Stocks Table ── */}
              {stocksLoading ? (
                <div className="skeleton" style={{ width: '100%', height: 300, borderRadius: 10 }} />
              ) : stocks.length > 0 ? (
                <>
                  <div style={{ display: 'flex', gap: 8, marginBottom: 12 }}>
                    {editMode && (
                      <button
                        className={`wl-add-toggle ${showAddStock ? 'active' : ''}`}
                        onClick={() => setShowAddStock(!showAddStock)}
                      >
                        {showAddStock ? (
                          <>
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                              <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
                            </svg>
                            Close
                          </>
                        ) : (
                          <>
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                              <line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" />
                            </svg>
                            Add Stocks
                          </>
                        )}
                      </button>
                    )}
                    <button
                      className="wl-changes-btn"
                      id="wl-view-changes-btn"
                      onClick={() => {
                        console.log('View Change clicked, selectedId=', selectedId);
                        setShowChanges(true);
                      }}
                    >
                      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                        <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
                      </svg>
                      View Change
                    </button>
                  </div>
                  <div className="wl-table-wrap">
                    <table className="data-table" id="wl-stocks-table">
                      <thead>
                        <tr>
                          {editMode && <th style={{ width: 60 }}>Order</th>}
                          <th>Name</th>
                          <th style={{ textAlign: 'right' }}>Mkt price / 1D</th>
                          <th style={{ textAlign: 'right' }}>Down from ATH</th>
                          {editMode && <th style={{ width: 48 }}></th>}
                        </tr>
                      </thead>
                      <tbody>
                        {stocks.map((s, idx) => {
                          const p = prices[String(s.id)];
                          const ltp = p?.ltp ?? s.last_price;
                          const change = p?.change ?? null;
                          const changePct = p?.change_pct ?? null;
                          const isUp = change != null && change >= 0;
                          const changeColor = change == null ? 'var(--text-tertiary)' : isUp ? '#10b981' : '#ef4444';

                          return (
                            <tr key={s.id}>
                              {editMode && (
                                <td style={{ textAlign: 'center' }}>
                                  <div style={{ display: 'flex', gap: 2, justifyContent: 'center' }}>
                                    <button
                                      className="wl-reorder-btn"
                                      onClick={() => handleMoveStock(idx, 'up')}
                                      disabled={idx === 0 || reordering}
                                      aria-label="Move up"
                                    >
                                      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
                                        <polyline points="18 15 12 9 6 15" />
                                      </svg>
                                    </button>
                                    <button
                                      className="wl-reorder-btn"
                                      onClick={() => handleMoveStock(idx, 'down')}
                                      disabled={idx === stocks.length - 1 || reordering}
                                      aria-label="Move down"
                                    >
                                      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
                                        <polyline points="6 9 12 15 18 9" />
                                      </svg>
                                    </button>
                                  </div>
                                </td>
                              )}
                              <td
                                style={{ cursor: 'pointer' }}
                                onClick={() => router.push(`/stocks/${s.symbol}`)}
                              >
                                <div style={{ fontWeight: 600, fontSize: 14 }}>
                                  {s.name || s.symbol.replace(/-EQ$|-SM$/, '')}
                                </div>
                              </td>
                              <td style={{ textAlign: 'right', minWidth: 130 }}>
                                <div style={{
                                  fontFamily: "'JetBrains Mono', monospace",
                                  fontSize: 14,
                                  fontWeight: 700,
                                }}>
                                  {ltp
                                    ? `₹${Number(ltp).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
                                    : '-'}
                                </div>
                                {change != null && (
                                  <div style={{
                                    fontSize: 12,
                                    fontFamily: "'JetBrains Mono', monospace",
                                    color: changeColor,
                                    marginTop: 2,
                                  }}>
                                    {isUp ? '+' : ''}{change.toFixed(2)} ({isUp ? '+' : ''}{changePct?.toFixed(2)}%)
                                  </div>
                                )}
                              </td>
                              <td style={{ textAlign: 'right', minWidth: 120 }}>
                                {(() => {
                                  const currentPrice = ltp ? Number(ltp) : null;
                                  const athPrice = s.ath_price ? Number(s.ath_price) : null;
                                  if (!athPrice || !currentPrice) return <span style={{ color: 'var(--text-tertiary)' }}>—</span>;
                                  const downPct = ((currentPrice - athPrice) / athPrice) * 100;
                                  const isAtAth = Math.abs(downPct) < 0.5;
                                  return (
                                    <>
                                      <div style={{
                                        fontFamily: "'JetBrains Mono', monospace",
                                        fontSize: 13,
                                        fontWeight: 600,
                                        color: 'var(--text-secondary)',
                                      }}>
                                        ₹{athPrice.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                                      </div>
                                      <div style={{
                                        fontSize: 12,
                                        fontFamily: "'JetBrains Mono', monospace",
                                        color: isAtAth ? '#10b981' : '#ef4444',
                                        marginTop: 2,
                                      }}>
                                        {isAtAth ? 'AT ATH' : `${downPct.toFixed(2)}%`}
                                      </div>
                                    </>
                                  );
                                })()}
                              </td>
                              {editMode && (
                                <td>
                                  <button
                                    className="wl-remove-btn"
                                    onClick={() => handleRemoveStock(s.id)}
                                    disabled={removingStockId === s.id}
                                    aria-label={`Remove ${s.symbol}`}
                                    title="Remove from watchlist"
                                  >
                                    {removingStockId === s.id ? (
                                      <span className="wl-spinner wl-spinner-xs" />
                                    ) : (
                                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                        <line x1="5" y1="12" x2="19" y2="12" />
                                      </svg>
                                    )}
                                  </button>
                                </td>
                              )}
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                </>
              ) : (
                <div className="empty-state">
                  <div className="empty-state-icon">📭</div>
                  <div className="empty-state-text">No stocks in this watchlist</div>
                  <div className="empty-state-sub">
                    {selectedWl.list_type === 'system'
                      ? 'This is a system watchlist. Auto-populate it with top stocks by volume.'
                      : 'Click \u201cAdd Stocks\u201d above to search and add stocks'}
                  </div>
                  <div style={{ display: 'flex', gap: 10, marginTop: 16, flexWrap: 'wrap', justifyContent: 'center' }}>
                    {/* Populate button — shown for any empty watchlist */}
                    <button
                      className="wl-populate-btn"
                      onClick={() => handlePopulate('volume')}
                      disabled={populating}
                      id="wl-populate-volume"
                    >
                      {populating ? (
                        <span className="wl-btn-loading">
                          <span className="wl-spinner wl-spinner-sm" />
                          Populating…
                        </span>
                      ) : (
                        <>
                          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                            <polyline points="7 10 12 15 17 10" />
                            <line x1="12" y1="15" x2="12" y2="3" />
                          </svg>
                          Populate — Top 500 by Volume
                        </>
                      )}
                    </button>
                    {!showAddStock && (
                      <button
                        className="wl-create-btn"
                        onClick={() => setShowAddStock(true)}
                      >
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                          <line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" />
                        </svg>
                        Add Manually
                      </button>
                    )}
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      </div>

      {/* ── Modals ────────────────────────────────────────── */}
      <CreateModal
        open={showCreate}
        onClose={() => setShowCreate(false)}
        onCreated={handleCreated}
      />

      {deletingWl && (
        <DeleteModal
          watchlist={deletingWl}
          onClose={() => setDeletingWl(null)}
          onDeleted={handleDeleted}
        />
      )}

      <ChangesModal
        open={showChanges && selectedId != null}
        onClose={() => setShowChanges(false)}
        watchlistId={selectedId ?? 0}
        watchlistName={selectedWl?.name ?? ''}
      />
    </div>
  );
}
