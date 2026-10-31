export interface StockDisplayFields {
  symbol: string;
  name?: string | null;
  company_name?: string | null;
  stock_name?: string | null;
}

const SERIES_SUFFIX = /-(?:EQ|SM|BE|BZ|ST)$/i;

export function formatStockSymbol(symbol: string): string {
  return symbol.trim().replace(SERIES_SUFFIX, '');
}

function comparableLabel(value: string): string {
  return formatStockSymbol(value).toLowerCase().replace(/[^a-z0-9]/g, '');
}

export function getStockCompanyName(stock: StockDisplayFields): string | null {
  const symbolKey = comparableLabel(stock.symbol);
  for (const value of [stock.company_name, stock.stock_name, stock.name]) {
    const candidate = value?.trim();
    if (
      !candidate
      || comparableLabel(candidate) === symbolKey
      || (candidate === candidate.toUpperCase() && !/\s/.test(candidate))
    ) continue;
    return candidate;
  }
  return null;
}
