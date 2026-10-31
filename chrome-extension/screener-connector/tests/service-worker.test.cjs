const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const core = require('../screener-core.js');

function makeHarness() {
  const store = {};
  const sessionStore = {};
  const tabs = new Map();
  const appMessages = [];
  const alarms = new Map();
  const listeners = { messages: null, removed: null, startup: null, installed: null, alarm: null };
  let nextTabId = 20;

  const chrome = {
    storage: {
      local: {
        get: async (key) => ({ [key]: structuredClone(store[key]) }),
        set: async (values) => Object.assign(store, structuredClone(values)),
        remove: async (key) => { delete store[key]; },
        setAccessLevel: async () => {},
      },
      session: {
        get: async (key) => ({ [key]: structuredClone(sessionStore[key]) }),
        remove: async (key) => { delete sessionStore[key]; },
      },
    },
    runtime: {
      onMessage: { addListener: (listener) => { listeners.messages = listener; } },
      onStartup: { addListener: (listener) => { listeners.startup = listener; } },
      onInstalled: { addListener: (listener) => { listeners.installed = listener; } },
    },
    alarms: {
      create: (name, info) => alarms.set(name, info),
      clear: async (name) => alarms.delete(name),
      onAlarm: { addListener: (listener) => { listeners.alarm = listener; } },
    },
    tabs: {
      create: async (info) => {
        const tab = { id: nextTabId++, url: info.url, active: Boolean(info.active) };
        tabs.set(tab.id, tab);
        return structuredClone(tab);
      },
      get: async (id) => {
        if (!tabs.has(id)) throw new Error('No such tab');
        return structuredClone(tabs.get(id));
      },
      update: async (id, info) => {
        const tab = tabs.get(id);
        if (!tab) throw new Error('No such tab');
        Object.assign(tab, info);
        return structuredClone(tab);
      },
      remove: async (id) => {
        tabs.delete(id);
        if (listeners.removed) await listeners.removed(id);
      },
      sendMessage: async (id, message) => {
        appMessages.push({ id, message });
      },
      onRemoved: { addListener: (listener) => { listeners.removed = listener; } },
    },
  };

  const context = vm.createContext({
    chrome,
    ScreenerConnectorCore: core,
    URL,
    Set,
    Date,
    Math,
    Number,
    String,
    Array,
    Object,
    RegExp,
    Promise,
    structuredClone,
    importScripts() {},
  });
  context.globalThis = context;
  const source = fs.readFileSync(require.resolve('../service-worker.js'), 'utf8');
  vm.runInContext(source, context, { filename: 'service-worker.js' });

  function dispatch(message, sender) {
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error(`No response for ${message.type}`)), 1000);
      const keepOpen = listeners.messages(message, sender, (reply) => {
        clearTimeout(timeout);
        resolve(reply);
      });
      if (!keepOpen) {
        clearTimeout(timeout);
        resolve(undefined);
      }
    });
  }

  return { appMessages, alarms, dispatch, listeners, sessionStore, store, tabs };
}

const appSender = {
  tab: { id: 7 },
  frameId: 0,
  url: 'http://localhost:3000/master',
};

function screenerSender(id, url) {
  return { tab: { id }, frameId: 0, url };
}

test('collects all pages, preserves the completed payload until app acknowledgement', async () => {
  const harness = makeHarness();
  const jobId = 'a'.repeat(32);
  const query = 'Market capitalization > 500';
  assert.equal((await harness.dispatch({ type: 'start-refresh', jobId, query }, appSender)).ok, true);
  const [tabId, tab] = [...harness.tabs.entries()][0];
  const pageOne = new URL(tab.url);
  const pageTwo = new URL(tab.url);
  pageTwo.searchParams.set('page', '2');
  const firstRows = [{
    symbol: 'ACME', name: 'Acme Ltd', industry: 'Industrial Goods', close: 10, marketCapCr: 900, roePct: 18, profitGrowth1yPct: 22,
  }];
  const secondRows = [{
    symbol: 'BETA', name: 'Beta Ltd', industry: null, close: 20, marketCapCr: 1200, roePct: null, profitGrowth1yPct: null,
  }];

  const next = await harness.dispatch({
    type: 'screener-page',
    pageUrl: pageOne.href,
    hasTable: true,
    rawRowCount: 1,
    parsedRowCount: 1,
    rows: firstRows,
    nextUrl: pageTwo.href,
  }, screenerSender(tabId, tab.url));
  assert.equal(next.nextUrl, pageTwo.href);
  assert.equal(next.delayMs, 1200);

  await harness.dispatch({
    type: 'screener-page',
    pageUrl: pageTwo.href,
    hasTable: true,
    rawRowCount: 1,
    parsedRowCount: 1,
    rows: secondRows,
    nextUrl: null,
  }, screenerSender(tabId, pageTwo.href));

  const status = await harness.dispatch({ type: 'get-refresh-status' }, appSender);
  assert.equal(status.active.status, 'completed_pending_ack');
  assert.equal(status.active.page_count, 2);
  assert.equal(status.active.candidates_found, 2);
  assert.equal(status.active.rows.map((row) => row.symbol).join(','), 'ACME,BETA');
  assert.equal(harness.store.activeScreenerRefresh.rows.length, 0);
  assert.equal((await harness.dispatch({ type: 'ack-refresh', jobId }, appSender)).ok, true);
  assert.equal((await harness.dispatch({ type: 'get-refresh-status' }, appSender)).active, null);
});

test('rejects unparsed rows and does not convert a partial page into success', async () => {
  const harness = makeHarness();
  const jobId = 'b'.repeat(32);
  const query = 'Return on equity > 15';
  await harness.dispatch({ type: 'start-refresh', jobId, query }, appSender);
  const [tabId, tab] = [...harness.tabs.entries()][0];
  await harness.dispatch({
    type: 'screener-page',
    pageUrl: tab.url,
    hasTable: true,
    rawRowCount: 2,
    parsedRowCount: 1,
    rows: [{ symbol: 'ACME', name: 'Acme Ltd' }],
    nextUrl: null,
  }, screenerSender(tabId, tab.url));

  const status = await harness.dispatch({ type: 'get-refresh-status' }, appSender);
  assert.equal(status.active.status, 'failed_pending_ack');
  assert.match(status.active.error, /table layout changed/i);
});

test('rejects an empty result table instead of reporting an empty success', async () => {
  const harness = makeHarness();
  const jobId = 'f'.repeat(32);
  const query = 'Return on equity > 15';
  await harness.dispatch({ type: 'start-refresh', jobId, query }, appSender);
  const [tabId, tab] = [...harness.tabs.entries()][0];
  await harness.dispatch({
    type: 'screener-page',
    pageUrl: tab.url,
    hasTable: true,
    rawRowCount: 0,
    parsedRowCount: 0,
    rows: [],
    nextUrl: null,
  }, screenerSender(tabId, tab.url));

  const status = await harness.dispatch({ type: 'get-refresh-status' }, appSender);
  assert.equal(status.active.status, 'failed_pending_ack');
  assert.match(status.active.error, /table layout changed/i);
});

test('does not open tabs for untrusted app origins', async () => {
  const harness = makeHarness();
  const reply = await harness.dispatch({
    type: 'start-refresh',
    jobId: 'c'.repeat(32),
    query: 'Close > 10',
  }, { ...appSender, url: 'http://localhost.evil.example:3000/' });
  assert.equal(reply.ok, false);
  assert.equal(harness.tabs.size, 0);
});

test('recovers a stored refresh after Chrome restarts', async () => {
  const harness = makeHarness();
  const query = 'Return on equity > 15';
  const pendingUrl = core.initialPageUrl(query);
  harness.store.activeScreenerRefresh = {
    jobId: 'd'.repeat(32),
    query,
    pendingUrl,
    status: 'running',
    scrapeTabId: 999,
    appTabId: 7,
    startedAt: Date.now(),
    finishedAt: null,
    rows: [],
    seenSymbols: [],
    visitedUrls: [],
    pagesCompleted: 0,
    progress: { phase: 'Opening Screener', pages_completed: 0, candidates_found: 0, detail: '' },
  };

  harness.listeners.startup();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(harness.store.activeScreenerRefresh.needsTabReload, true);
  assert.ok(harness.alarms.has(`screener-refresh-${'d'.repeat(32)}`));

  const status = await harness.dispatch({ type: 'get-refresh-status' }, appSender);
  assert.equal(status.active.status, 'running');
  assert.equal(harness.store.activeScreenerRefresh.needsTabReload, false);
  assert.equal([...harness.tabs.values()][0].url, pendingUrl);
});

test('migrates legacy session state and safely fails an interrupted old-format job', async () => {
  const harness = makeHarness();
  const query = 'Close > 10';
  harness.sessionStore.activeScreenerRefresh = {
    jobId: 'e'.repeat(32),
    query,
    status: 'running',
    scrapeTabId: 10,
    appTabId: 7,
    startedAt: Date.now(),
    finishedAt: null,
    rows: [{ symbol: 'ACME', name: 'Acme Ltd' }],
    seenSymbols: ['ACME'],
    visitedUrls: [],
    pagesCompleted: 0,
    progress: { phase: 'Reading', pages_completed: 0, candidates_found: 1, detail: '' },
  };

  const status = await harness.dispatch({ type: 'get-refresh-status' }, appSender);
  assert.equal(status.active.status, 'failed_pending_ack');
  assert.match(status.active.error, /updated during this refresh/i);
  assert.equal(harness.sessionStore.activeScreenerRefresh, undefined);
  assert.equal(harness.store.activeScreenerRefresh.status, 'failed_pending_ack');
});
