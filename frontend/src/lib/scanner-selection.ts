export type ScannerSelectionSpec =
  | { mode: 'top' | 'bottom'; count: number }
  | { mode: 'all'; count: null };

/**
 * Reads the selection instruction from the end of a scanner-created watchlist
 * name. Examples: "Enhanced momentum top 44", "Momentum bottom-30", "Momentum all".
 */
export function parseScannerSelection(name: string): ScannerSelectionSpec | null {
  const normalized = name.trim();
  if (/(?:^|[\s_-])all\s*$/i.test(normalized)) {
    return { mode: 'all', count: null };
  }

  const match = normalized.match(/\b(top|bottom)[\s_-]*(\d+)\s*$/i);
  if (!match) return null;

  const count = Number(match[2]);
  if (!Number.isInteger(count) || count < 1) return null;

  return { mode: match[1].toLowerCase() as 'top' | 'bottom', count };
}

export function selectScannerStockIds<T extends { stock_id: number; rank: number }>(
  results: T[],
  selection: ScannerSelectionSpec,
): number[] {
  const ranked = [...results].sort((left, right) => left.rank - right.rank);
  if (selection.mode === 'all') return ranked.map((result) => result.stock_id);

  const candidates = selection.mode === 'top' ? ranked : [...ranked].reverse();
  return candidates.slice(0, selection.count).map((result) => result.stock_id);
}
