'use client';

import { useState, useEffect, useRef, useCallback } from 'react';
import { searchStocks } from '@/lib/api';
import type { Stock } from '@/lib/types';
import { formatStockSymbol, getStockCompanyName } from '@/lib/stock-display';
import { useStockDetails } from './StockDetailsContext';

export default function TopBar() {
  const { openStock } = useStockDetails();
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<Stock[]>([]);
  const [isOpen, setIsOpen] = useState(false);
  const [focusedIdx, setFocusedIdx] = useState(-1);
  const [time, setTime] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout>>(undefined);

  // Clock
  useEffect(() => {
    const tick = () => {
      const now = new Date();
      setTime(
        now.toLocaleTimeString('en-IN', {
          hour: '2-digit',
          minute: '2-digit',
          second: '2-digit',
          hour12: false,
          timeZone: 'Asia/Kolkata',
        })
      );
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, []);

  // Market status
  const getMarketStatus = useCallback(() => {
    const now = new Date();
    const ist = new Date(now.toLocaleString('en-US', { timeZone: 'Asia/Kolkata' }));
    const h = ist.getHours();
    const m = ist.getMinutes();
    const day = ist.getDay();
    const mins = h * 60 + m;
    if (day === 0 || day === 6) return 'closed';
    if (mins >= 555 && mins < 930) return 'open'; // 9:15 - 15:30
    if (mins >= 540 && mins < 555) return 'pre';
    return 'closed';
  }, []);

  const [marketStatus, setMarketStatus] = useState('closed');
  useEffect(() => {
    setMarketStatus(getMarketStatus());
    const id = setInterval(() => setMarketStatus(getMarketStatus()), 30000);
    return () => clearInterval(id);
  }, [getMarketStatus]);

  // Search with debounce
  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    if (!query.trim()) {
      setResults([]);
      setIsOpen(false);
      return;
    }
    debounceRef.current = setTimeout(async () => {
      try {
        const stocks = await searchStocks(query, 10);
        setResults(stocks);
        setIsOpen(true);
        setFocusedIdx(-1);
      } catch {
        setResults([]);
      }
    }, 250);
  }, [query]);

  // Keyboard navigation
  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setFocusedIdx((prev) => Math.min(prev + 1, results.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setFocusedIdx((prev) => Math.max(prev - 1, 0));
    } else if (e.key === 'Enter' && focusedIdx >= 0) {
      e.preventDefault();
      openStockResult(results[focusedIdx]);
    } else if (e.key === 'Escape') {
      setIsOpen(false);
      inputRef.current?.blur();
    }
  };

  const openStockResult = (stock: Stock) => {
    setQuery('');
    setIsOpen(false);
    openStock({
      symbol: stock.symbol,
      name: stock.name,
      company_name: stock.company_name,
      sector: stock.sector,
      industry: stock.industry,
      market_cap_cr: stock.market_cap_cr,
      close: stock.latest_price ?? stock.last_price ?? null,
      as_of_date: stock.price_date ?? null,
    });
  };

  // Global keyboard shortcut
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault();
        inputRef.current?.focus();
      }
    };
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, []);

  // Click outside
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setIsOpen(false);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  const statusLabel = marketStatus === 'open' ? 'MARKET OPEN' : marketStatus === 'pre' ? 'PRE-MARKET' : 'MARKET CLOSED';
  const statusClass = marketStatus === 'open' ? 'open' : 'closed';

  return (
    <header className="topbar">
      <div className="search-container" ref={containerRef}>
        <svg className="search-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" />
        </svg>
        <input
          ref={inputRef}
          type="text"
          className="search-input"
          placeholder="Search stocks..."
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={handleKeyDown}
          onFocus={() => results.length > 0 && setIsOpen(true)}
        />
        <span className="search-kbd">Ctrl+K</span>

        {isOpen && results.length > 0 && (
          <div className="search-results" role="listbox" aria-label="Stock search results">
            {results.map((stock, idx) => {
              const displaySymbol = formatStockSymbol(stock.symbol);
              const companyName = getStockCompanyName(stock);
              return (
              <button
                type="button"
                key={stock.id}
                className={`search-result-item ${idx === focusedIdx ? 'focused' : ''}`}
                role="option"
                aria-selected={idx === focusedIdx}
                onClick={() => openStockResult(stock)}
                onMouseEnter={() => setFocusedIdx(idx)}
                onFocus={() => setFocusedIdx(idx)}
              >
                <div style={{ display: 'flex', alignItems: 'center' }}>
                  <span className="search-result-symbol">{displaySymbol}</span>
                  {companyName && <span className="search-result-name">{companyName}</span>}
                  <span className="search-result-sector">Sector: {stock.sector || '—'}</span>
                </div>
                <span className="search-result-exchange">{stock.exchange}</span>
              </button>
              );
            })}
          </div>
        )}
      </div>

      <div className="topbar-right">
        <span className={`topbar-badge ${statusClass}`}>{statusLabel}</span>
        <span className="topbar-time">{time} IST</span>
      </div>
    </header>
  );
}
