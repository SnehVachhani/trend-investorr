export interface ScreenerQueryCsvRow {
  symbol: string;
  name: string;
  industry: string | null;
  close: number | null;
  marketCapCr: number | null;
  roePct: number | null;
  profitGrowth1yPct: number | null;
}

function readCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = '';
  let quoted = false;
  const input = text.replace(/^\uFEFF/, '');

  for (let index = 0; index < input.length; index += 1) {
    const character = input[index];
    if (quoted) {
      if (character === '"' && input[index + 1] === '"') {
        cell += '"';
        index += 1;
      } else if (character === '"') {
        quoted = false;
      } else {
        cell += character;
      }
      continue;
    }
    if (character === '"' && cell.length === 0) {
      quoted = true;
    } else if (character === ',') {
      row.push(cell.trim());
      cell = '';
    } else if (character === '\n' || character === '\r') {
      if (character === '\r' && input[index + 1] === '\n') index += 1;
      row.push(cell.trim());
      if (row.some((value) => value !== '')) rows.push(row);
      row = [];
      cell = '';
    } else {
      cell += character;
    }
  }
  row.push(cell.trim());
  if (row.some((value) => value !== '')) rows.push(row);
  return rows;
}

function headerKey(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]/g, '');
}

function columnIndex(headers: string[], names: string[]): number {
  const keys = headers.map(headerKey);
  const candidates = names.map(headerKey);
  return keys.findIndex((key) => candidates.some((candidate) => key === candidate || key.startsWith(candidate)));
}

function cellAt(row: string[], index: number): string {
  return index < 0 ? '' : (row[index] ?? '').trim();
}

function numericCell(value: string): number | null {
  const cleaned = value.replace(/[₹,%\s]/g, '').replace(/,/g, '').replace(/−/g, '-');
  if (!cleaned || cleaned === '-' || cleaned === '—' || cleaned.toLowerCase() === 'na') return null;
  const number = Number(cleaned);
  return Number.isFinite(number) ? number : null;
}

export function parseScreenerQueryCsv(text: string): ScreenerQueryCsvRow[] {
  if (text.length > 12_000_000) throw new Error('The Screener CSV is larger than 12 MB.');
  const records = readCsv(text);
  const headerRowIndex = records.findIndex((row) => {
    const keys = row.map(headerKey);
    return keys.includes('name') && (keys.includes('nsecode') || keys.includes('bsecode') || keys.includes('symbol') || keys.includes('ticker'));
  });
  if (headerRowIndex < 0) {
    throw new Error('Could not find Screener CSV columns for Name and an NSE/BSE code. Export the full results from Screener.');
  }

  const headers = records[headerRowIndex];
  const nseCode = columnIndex(headers, ['nsecode', 'nse symbol']);
  const bseCode = columnIndex(headers, ['bsecode', 'bse symbol']);
  const symbolColumn = columnIndex(headers, ['symbol', 'ticker']);
  const nameColumn = columnIndex(headers, ['name', 'company name', 'company']);
  const industryColumn = columnIndex(headers, ['industry', 'sector']);
  const closeColumn = columnIndex(headers, ['cmprs', 'current price', 'price']);
  const marketCapColumn = columnIndex(headers, ['marcaprscr', 'market capitalization', 'market cap']);
  const roeColumn = columnIndex(headers, ['roe', 'return on equity']);
  const profitGrowthColumn = columnIndex(headers, [
    'net profit growth 1y', 'net profit growth 1 year', 'profit growth 1y', 'profit growth 1 year',
  ]);

  const parsed: ScreenerQueryCsvRow[] = [];
  const seen = new Set<string>();
  for (const row of records.slice(headerRowIndex + 1)) {
    const symbol = cellAt(row, nseCode) || cellAt(row, symbolColumn) || cellAt(row, bseCode);
    const name = cellAt(row, nameColumn);
    if (!symbol || !name) continue;
    const key = symbol.toUpperCase();
    if (seen.has(key)) continue;
    seen.add(key);
    parsed.push({
      symbol: key,
      name,
      industry: cellAt(row, industryColumn) || null,
      close: numericCell(cellAt(row, closeColumn)),
      marketCapCr: numericCell(cellAt(row, marketCapColumn)),
      roePct: numericCell(cellAt(row, roeColumn)),
      profitGrowth1yPct: numericCell(cellAt(row, profitGrowthColumn)),
    });
    if (parsed.length > 5000) throw new Error('The Screener CSV contains more than 5,000 candidates.');
  }
  if (parsed.length === 0) throw new Error('The Screener CSV has no company rows with exchange codes.');
  return parsed;
}
