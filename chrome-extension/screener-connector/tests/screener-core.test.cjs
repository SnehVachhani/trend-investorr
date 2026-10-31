const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const core = require('../screener-core.js');

function node(textContent) {
  return { textContent, innerText: textContent };
}

function anchor(text, href, attributes = {}) {
  return {
    ...node(text),
    href,
    getAttribute(name) {
      return attributes[name] || null;
    },
  };
}

function row(values, companyHref) {
  return {
    querySelectorAll(selector) {
      if (selector === 'td') return values.map(node);
      if (selector === 'a[href]') return [anchor(values[0], companyHref)];
      return [];
    },
  };
}

function resultsTable(headers, rows) {
  return {
    querySelectorAll(selector) {
      if (selector === 'thead th') return headers.map(node);
      if (selector === 'tbody tr') return rows;
      return [];
    },
  };
}

test('parses Screener formatted numbers and missing values', () => {
  assert.equal(core.parseNumber('₹ 2,340.50'), 2340.5);
  assert.equal(core.parseNumber('(1,204.2)'), -1204.2);
  assert.equal(core.parseNumber('−4.5%'), -4.5);
  assert.equal(core.parseNumber('—'), null);
  assert.equal(core.parseNumber('n/a'), null);
});

test('builds query URLs and rejects pagination outside the requested screen', () => {
  const query = 'Market capitalization > 500 AND Return on equity > 15%';
  const initial = core.initialPageUrl(query);
  assert.equal(new URL(initial).searchParams.get('query'), query);
  assert.equal(core.canonicalPageUrl(initial, query), initial);
  assert.equal(core.pageNumber(initial), 1);

  const pageTwo = new URL(initial);
  pageTwo.searchParams.set('page', '2');
  assert.equal(core.pageNumber(pageTwo.href), 2);
  assert.equal(core.canonicalPageUrl(pageTwo.href, query), pageTwo.href);
  assert.equal(core.canonicalPageUrl('https://attacker.example/screen/raw/?query=x', query), null);
  assert.equal(core.canonicalPageUrl('https://www.screener.in/company/ABC/', query), null);
  assert.equal(core.canonicalPageUrl(`${initial}&query=other`, query), null);
});

test('parses every visible row and extracts the selected fundamental columns', () => {
  const table = resultsTable(
    ['Name', 'NSE Code', 'Industry', 'CMP Rs.', 'Mar Cap Rs.Cr.', 'ROE %', 'Profit growth 1Y %'],
    [row(
      ['Acme Industries Ltd', 'ACME', 'Capital Goods', '₹ 245.60', '1,250.4', '18.2', '22.1'],
      'https://www.screener.in/company/ACME/consolidated/',
    )],
  );

  assert.deepEqual(core.parseRows(table), [{
    symbol: 'ACME',
    name: 'Acme Industries Ltd',
    industry: 'Capital Goods',
    close: 245.6,
    marketCapCr: 1250.4,
    roePct: 18.2,
    profitGrowth1yPct: 22.1,
  }]);
  const document = {
    querySelectorAll() { return [table]; },
  };
  assert.equal(core.findResultsTable(document), table);
});

test('uses company links when an NSE code column is absent and finds the next page', () => {
  const href = 'https://www.screener.in/screen/raw/?query=roe%20%3E%2015&page=2';
  const table = resultsTable(
    ['Name', 'CMP Rs.'],
    [row(['Acme Industries Ltd', '245.60'], 'https://www.screener.in/company/ACME/consolidated/')],
  );
  const document = {
    querySelectorAll(selector) {
      if (selector.includes('table')) return [table];
      if (selector === 'a[href]') return [anchor('2', href)];
      return [];
    },
  };

  assert.equal(core.parseRows(table)[0].symbol, 'ACME');
  assert.equal(core.nextPageUrl(document, 1), href);
});

test('cleans candidate values into a bounded field whitelist', () => {
  assert.deepEqual(core.cleanCandidate({
    symbol: ' acme ',
    name: ' Acme Ltd ',
    industry: ' Industrial Goods ',
    close: '₹ 10.50',
    marketCapCr: '1,000',
    roePct: '18%',
    profitGrowth1yPct: '—',
    unexpected: 'discard me',
  }), {
    symbol: 'ACME',
    name: 'Acme Ltd',
    industry: 'Industrial Goods',
    close: 10.5,
    marketCapCr: 1000,
    roePct: 18,
    profitGrowth1yPct: null,
  });
  assert.equal(core.cleanCandidate({ symbol: 'bad symbol', name: 'Invalid' }), null);
  assert.equal(core.cleanCandidate({ symbol: 'ACME', name: '' }), null);
});

test('keeps the extension permissions and local app hosts narrowly scoped', () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'manifest.json'), 'utf8'));
  assert.equal(manifest.manifest_version, 3);
  assert.equal(Number(manifest.minimum_chrome_version) >= 114, true);
  assert.deepEqual(manifest.permissions, ['storage', 'alarms']);
  assert.deepEqual(manifest.host_permissions, [
    'https://www.screener.in/*',
    'https://screener.in/*',
    'http://localhost:3000/*',
    'http://127.0.0.1:3000/*',
  ]);
  assert.equal(JSON.stringify(manifest).includes('cookies'), false);
  assert.equal(JSON.stringify(manifest).includes('debugger'), false);
});
