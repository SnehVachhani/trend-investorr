'use client';

import { formatStockSymbol, getStockCompanyName } from '@/lib/stock-display';
import type { StockModalData } from '@/lib/types';
import { useStockDetails } from './StockDetailsContext';

export default function StockIdentity({ className, ...stock }: StockModalData & { className?: string }) {
  const { openStock } = useStockDetails();
  const companyName = getStockCompanyName(stock);
  return (
    <button
      type="button"
      className={`stock-identity-trigger${className ? ` ${className}` : ''}`}
      aria-label={`Open details, chart and fundamentals for ${companyName || formatStockSymbol(stock.symbol)}`}
      onClick={(event) => {
        event.stopPropagation();
        openStock(stock);
      }}
    >
      <span className="stock-identity">
        {companyName ? (
          <>
            <strong className="stock-identity-name" title={companyName}>{companyName}</strong>
            <small className="stock-identity-symbol">{formatStockSymbol(stock.symbol)}{stock.exchange ? ` · ${stock.exchange}` : ''}</small>
          </>
        ) : (
          <strong className="stock-identity-symbol">{formatStockSymbol(stock.symbol)}</strong>
        )}
      </span>
      <span className="stock-identity-open-icon" aria-hidden="true">↗</span>
    </button>
  );
}
