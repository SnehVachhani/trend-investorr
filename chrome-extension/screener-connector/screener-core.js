((root, factory) => {
  const core = factory();
  if (typeof module === 'object' && module.exports) module.exports = core;
  root.ScreenerConnectorCore = core;
})(globalThis, () => {
  const SYMBOL_FROM_COMPANY_URL = /\/company\/([^/?#]+)/i;
  const KNOWN_HEADER_KEYS = new Set([
    'name', 'company', 'companyname', 'nsecode', 'bsecode', 'symbol', 'ticker',
    'industry', 'sector', 'cmprs', 'currentprice', 'price', 'marcaprscr',
    'marketcapitalization', 'marketcap', 'roe', 'returnonequity',
    'netprofitgrowth1y', 'netprofitgrowth1year', 'profitgrowth1y',
    'profitgrowth1year', 'qtrprofitvar', 'quarterlyprofitgrowth',
    'yoyquarterlyprofitgrowth',
  ]);

  function cleanText(node) {
    return (node?.innerText || node?.textContent || '').replace(/\s+/g, ' ').trim();
  }

  function headerKey(value) {
    return value.toLowerCase().replace(/[^a-z0-9]/g, '');
  }

  function column(headers, aliases) {
    const normalized = headers.map(headerKey);
    for (const alias of aliases) {
      const key = headerKey(alias);
      const found = normalized.findIndex((header) => header === key || header.startsWith(key));
      if (found >= 0) return found;
    }
    return -1;
  }

  function companySymbol(href) {
    try {
      const url = new URL(href, 'https://www.screener.in');
      if (!['https://www.screener.in', 'https://screener.in'].includes(url.origin)) return '';
      const match = url.pathname.match(SYMBOL_FROM_COMPANY_URL);
      if (!match) return '';
      return decodeURIComponent(match[1]).trim().toUpperCase();
    } catch {
      return '';
    }
  }

  function tableRows(table) {
    const rows = [...table.querySelectorAll('tr')];
    return rows.length ? rows : [...table.querySelectorAll('tbody tr')];
  }

  function rowCells(row) {
    const directCells = [...(row.children || [])]
      .filter((child) => ['TD', 'TH'].includes(child.tagName?.toUpperCase()));
    if (directCells.length) return directCells;
    const dataCells = [...row.querySelectorAll('td')];
    if (dataCells.length) return dataCells;
    const headerCells = [...row.querySelectorAll('th')];
    if (headerCells.length) return headerCells;
    return [...(row.children || [])].filter((child) => ['TD', 'TH'].includes(child.tagName?.toUpperCase()));
  }

  function headerLabel(node) {
    return cleanText(node)
      || node?.getAttribute?.('aria-label')
      || node?.getAttribute?.('data-sort')
      || node?.getAttribute?.('data-column')
      || node?.getAttribute?.('title')
      || '';
  }

  function rowCompanyAnchor(row) {
    return [...row.querySelectorAll('a[href]')].find((anchor) => companySymbol(anchor.href)) || null;
  }

  function cellLabel(node) {
    return node?.getAttribute?.('data-label')
      || node?.getAttribute?.('aria-label')
      || node?.getAttribute?.('data-column')
      || node?.getAttribute?.('title')
      || '';
  }

  function directHeaderCells(row) {
    return [...(row.children || [])].filter((child) => child.tagName?.toUpperCase() === 'TH');
  }

  function headerCells(table) {
    const standardHeaders = [...table.querySelectorAll('thead th')];
    if (standardHeaders.length > 0) return standardHeaders;

    const accessibleHeaders = [...table.querySelectorAll('thead [role="columnheader"], thead td')];
    if (accessibleHeaders.length > 0) return accessibleHeaders;

    // Some table layouts keep the heading row in TBODY and use TDs instead of THs.
    const rows = tableRows(table);
    const headerRow = rows.find((row) => {
      if (rowCompanyAnchor(row)) return false;
      if (directHeaderCells(row).length > 0) return true;
      if (row.querySelectorAll('th').length > 0) return true;
      const labels = rowCells(row).map((cell) => headerKey(headerLabel(cell)));
      return labels.filter((label) => KNOWN_HEADER_KEYS.has(label)).length >= 2;
    });
    return headerRow ? rowCells(headerRow) : [];
  }

  function isHeaderRow(row) {
    if (rowCompanyAnchor(row)) return false;
    if (directHeaderCells(row).length > 0 || row.querySelectorAll('th').length > 0) return true;
    const labels = rowCells(row).map((cell) => headerKey(headerLabel(cell)));
    return labels.filter((label) => KNOWN_HEADER_KEYS.has(label)).length >= 2;
  }

  function parseNumber(value) {
    const cleaned = String(value || '').replace(/,/g, '').replace(/−/g, '-').trim();
    if (!cleaned || /^[-—–]$/.test(cleaned) || /^(n\/a|na)$/i.test(cleaned)) return null;
    const negative = cleaned.startsWith('(') && cleaned.endsWith(')');
    const token = cleaned.match(/-?\d+(?:\.\d+)?/);
    if (!token) return null;
    const parsed = Number(token[0]) * (negative ? -1 : 1);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function findResultsTable(document) {
    const tables = [...document.querySelectorAll('#screener-table, table.data-table, table')];
    return tables.map((table) => {
      const headers = headerCells(table).map((header) => headerKey(headerLabel(header)));
      const hasNameHeader = column(headers, ['name', 'company name', 'company']) >= 0;
      const linkedRows = tableRows(table).filter((row) => rowCompanyAnchor(row)).length;
      const id = table.getAttribute?.('id') || table.id || '';
      const className = table.getAttribute?.('class') || table.className || '';
      const score = (hasNameHeader ? 100 : 0)
        + Math.min(linkedRows, 50)
        + (id === 'screener-table' ? 20 : 0)
        + (String(className).split(/\s+/).includes('data-table') ? 10 : 0);
      return { table, score };
    }).filter((entry) => entry.score > 0).sort((left, right) => right.score - left.score)[0]?.table || null;
  }

  function parseTable(table) {
    const headerNodes = headerCells(table);
    const headers = headerNodes.map((header) => headerLabel(header));
    const nameIndex = column(headers, ['name', 'company name', 'company']);
    const symbolIndex = column(headers, ['nse code', 'symbol', 'ticker']);
    const bseIndex = column(headers, ['bse code']);
    const industryIndex = column(headers, ['industry', 'sector']);
    const closeIndex = column(headers, ['cmp rs', 'current price', 'price']);
    const marketCapIndex = column(headers, ['mar cap rs cr', 'market capitalization', 'market cap']);
    const roeIndex = column(headers, ['roe', 'return on equity']);
    const growthIndex = column(headers, [
      'net profit growth 1y', 'net profit growth 1 year', 'profit growth 1y', 'profit growth 1 year',
      'qtr profit var', 'quarterly profit growth', 'yoy quarterly profit growth',
    ]);

    const aliases = {
      name: ['name', 'company name', 'company'],
      symbol: ['nse code', 'symbol', 'ticker'],
      bse: ['bse code'],
      industry: ['industry', 'sector'],
      close: ['cmp rs', 'current price', 'price'],
      marketCap: ['mar cap rs cr', 'market capitalization', 'market cap'],
      roe: ['roe', 'return on equity'],
      growth: [
        'net profit growth 1y', 'net profit growth 1 year', 'profit growth 1y', 'profit growth 1 year',
        'qtr profit var', 'quarterly profit growth', 'yoy quarterly profit growth',
      ],
    };
    const valuesFor = (row, indices, key) => {
      const cells = rowCells(row);
      const index = indices[key];
      const positional = index >= 0 ? cleanText(cells[index]) : '';
      if (positional) return positional;
      const aliasesForKey = aliases[key] || [];
      const labelled = cells.find((candidate) => column([cellLabel(candidate)], aliasesForKey) === 0);
      return cleanText(labelled);
    };
    const indices = {
      name: nameIndex,
      symbol: symbolIndex,
      bse: bseIndex,
      industry: industryIndex,
      close: closeIndex,
      marketCap: marketCapIndex,
      roe: roeIndex,
      growth: growthIndex,
    };
    const candidates = tableRows(table).filter((row) => {
      if (isHeaderRow(row)) return false;
      if (rowCompanyAnchor(row)) return true;
      const hasName = nameIndex >= 0 && Boolean(valuesFor(row, indices, 'name'));
      const hasSymbol = Boolean(valuesFor(row, indices, 'symbol') || valuesFor(row, indices, 'bse'));
      return hasName && hasSymbol;
    });

    const rows = candidates.flatMap((row) => {
      const companyAnchor = rowCompanyAnchor(row);
      const name = valuesFor(row, indices, 'name') || cleanText(companyAnchor);
      const symbol = valuesFor(row, indices, 'symbol').toUpperCase()
        || valuesFor(row, indices, 'bse').toUpperCase()
        || companySymbol(companyAnchor?.href);
      if (!name || !symbol) return [];
      return [{
        symbol,
        name,
        industry: valuesFor(row, indices, 'industry') || null,
        close: parseNumber(valuesFor(row, indices, 'close')),
        marketCapCr: parseNumber(valuesFor(row, indices, 'marketCap')),
        roePct: parseNumber(valuesFor(row, indices, 'roe')),
        profitGrowth1yPct: parseNumber(valuesFor(row, indices, 'growth')),
      }];
    });

    return { rows, companyRowCount: candidates.length };
  }

  function parseRows(table) {
    return parseTable(table).rows;
  }

  function isLoginPage(location, document) {
    return /^\/(login|register|accounts\/login|user\/login)(\/|$)/i.test(location.pathname)
      || Boolean(document.querySelector('form input[type="password"]'));
  }

  function canonicalPageUrl(value, expectedQuery) {
    try {
      const url = new URL(value);
      if (url.origin !== 'https://www.screener.in' && url.origin !== 'https://screener.in') return null;
      if (url.pathname.replace(/\/$/, '') !== '/screen/raw') return null;
      const queries = url.searchParams.getAll('query');
      if (queries.length !== 1 || queries[0] !== expectedQuery) return null;
      const pages = url.searchParams.getAll('page');
      if (pages.length > 1) return null;
      if (pages.length === 1) {
        const page = Number(pages[0]);
        if (!/^\d+$/.test(pages[0]) || !Number.isSafeInteger(page) || page < 1) return null;
      }
      url.hash = '';
      if (url.hostname === 'screener.in') url.hostname = 'www.screener.in';
      return url.href;
    } catch {
      return null;
    }
  }

  function initialPageUrl(query) {
    const url = new URL('https://www.screener.in/screen/raw/');
    url.searchParams.set('order', '');
    url.searchParams.set('query', query);
    url.searchParams.set('sort', '');
    url.searchParams.set('source', '');
    return url.href;
  }

  function pageNumber(value) {
    const raw = new URL(value).searchParams.get('page');
    const parsed = Number(raw || '1');
    return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : 1;
  }

  function nextPageUrl(document, currentPage) {
    const links = [...document.querySelectorAll('a[href]')];
    const next = links.find((link) => {
      const text = cleanText(link).toLowerCase();
      const label = (link.getAttribute('aria-label') || '').toLowerCase();
      const rel = (link.getAttribute('rel') || '').toLowerCase().split(/\s+/);
      return rel.includes('next') || ['next', 'next ›', '› next', '→'].includes(text) || label.includes('next');
    });
    if (next) return next.href;

    const laterPages = links.flatMap((link) => {
      try {
        const url = new URL(link.href);
        const page = Number(url.searchParams.get('page'));
        return Number.isSafeInteger(page) && page > currentPage ? [{ page, href: link.href }] : [];
      } catch {
        return [];
      }
    }).sort((left, right) => left.page - right.page);
    return laterPages[0]?.href || null;
  }

  function cleanCandidate(candidate) {
    if (!candidate || typeof candidate !== 'object' || Array.isArray(candidate)) return null;
    const symbol = String(candidate.symbol || '').trim().toUpperCase();
    const name = String(candidate.name || '').trim();
    if (!/^[A-Z0-9&._-]{1,40}$/.test(symbol) || !name || name.length > 200) return null;
    const optionalText = (value, maxLength) => {
      const text = String(value || '').trim();
      return text ? text.slice(0, maxLength) : null;
    };
    return {
      symbol,
      name,
      industry: optionalText(candidate.industry, 160),
      close: parseNumber(candidate.close),
      marketCapCr: parseNumber(candidate.marketCapCr),
      roePct: parseNumber(candidate.roePct),
      profitGrowth1yPct: parseNumber(candidate.profitGrowth1yPct),
    };
  }

  return {
    canonicalPageUrl,
    cleanCandidate,
    findResultsTable,
    initialPageUrl,
    isLoginPage,
    nextPageUrl,
    pageNumber,
    parseNumber,
    parseTable,
    parseRows,
  };
});
