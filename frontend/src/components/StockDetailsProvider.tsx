'use client';

import { useCallback, useMemo, useState, type ReactNode } from 'react';
import type { StockModalData } from '@/lib/types';
import EnhancedMomentumStockModal from './EnhancedMomentumStockModal';
import SortableTables from './SortableTables';
import { StockDetailsContext } from './StockDetailsContext';

export default function StockDetailsProvider({ children }: { children: ReactNode }) {
  const [activeStock, setActiveStock] = useState<StockModalData | null>(null);
  const openStock = useCallback((stock: StockModalData) => setActiveStock({ ...stock }), []);
  const closeStock = useCallback(() => setActiveStock(null), []);
  const contextValue = useMemo(() => ({ openStock }), [openStock]);

  return (
    <StockDetailsContext.Provider value={contextValue}>
      <SortableTables />
      {children}
      {activeStock && (
        <EnhancedMomentumStockModal key={activeStock.symbol} stock={activeStock} onClose={closeStock} />
      )}
    </StockDetailsContext.Provider>
  );
}
