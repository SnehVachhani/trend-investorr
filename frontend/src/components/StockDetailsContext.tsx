'use client';

import { createContext, useContext } from 'react';
import type { StockModalData } from '@/lib/types';

export interface StockDetailsContextValue {
  openStock: (stock: StockModalData) => void;
}

export const StockDetailsContext = createContext<StockDetailsContextValue | null>(null);

export function useStockDetails() {
  const context = useContext(StockDetailsContext);
  if (!context) throw new Error('useStockDetails must be used within StockDetailsProvider');
  return context;
}
