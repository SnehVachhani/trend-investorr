importScripts('screener-core.js');

const core = globalThis.ScreenerConnectorCore;
const STATE_KEY = 'activeScreenerRefresh';
const REFRESH_ALARM_PREFIX = 'screener-refresh-';
const ACK_ALARM_PREFIX = 'screener-ack-';
const MAX_ROWS = 5000;
const MAX_PAGES = 250;
const REFRESH_TIMEOUT_MS = 10 * 60_000;
const ACK_TIMEOUT_MS = 20 * 60_000;
const STORAGE = chrome.storage.local;
const ALLOWED_APP_HOSTS = new Set(['localhost', '127.0.0.1']);
const ALLOWED_APP_PORT = '3000';
const ALLOWED_SCREENER_ORIGINS = new Set(['https://www.screener.in', 'https://screener.in']);

STORAGE.setAccessLevel({ accessLevel: 'TRUSTED_CONTEXTS' }).catch(() => {});

async function getState() {
  const stored = await STORAGE.get(STATE_KEY);
  if (stored[STATE_KEY]) return stored[STATE_KEY];
  const legacy = await chrome.storage.session.get(STATE_KEY).catch(() => ({}));
  const state = legacy[STATE_KEY];
  if (!state) return null;
  if (['running', 'waiting_for_login'].includes(state.status) && !state.pendingUrl) {
    state.status = 'failed_pending_ack';
    state.finishedAt = Date.now();
    state.error = 'The connector was updated during this refresh. Start a new refresh.';
    state.progress = {
      ...state.progress,
      phase: 'Failed',
      detail: state.error,
    };
    state.rows = [];
    state.seenSymbols = [];
    chrome.alarms.create(`${ACK_ALARM_PREFIX}${state.jobId}`, { delayInMinutes: ACK_TIMEOUT_MS / 60_000 });
  }
  state.pendingUrl = state.pendingUrl || core.initialPageUrl(state.query);
  await STORAGE.set({ [STATE_KEY]: state });
  await chrome.storage.session.remove(STATE_KEY).catch(() => {});
  return state;
}

async function saveState(state) {
  await STORAGE.set({ [STATE_KEY]: state });
}

async function clearState(state) {
  await STORAGE.remove(STATE_KEY);
  if (state?.jobId) {
    await chrome.alarms.clear(`${REFRESH_ALARM_PREFIX}${state.jobId}`);
    await chrome.alarms.clear(`${ACK_ALARM_PREFIX}${state.jobId}`);
  }
}

async function discardStoredRefresh(state) {
  await clearState(state);
  if (state?.scrapeTabId != null) {
    try {
      await chrome.tabs.remove(state.scrapeTabId);
    } catch {}
  }
}

function isAllowedAppSender(sender) {
  if (!sender.tab || sender.frameId !== 0) return false;
  try {
    const url = new URL(sender.url);
    return url.protocol === 'http:'
      && ALLOWED_APP_HOSTS.has(url.hostname)
      && url.port === ALLOWED_APP_PORT;
  } catch {
    return false;
  }
}

function isAllowedScreenerSender(sender) {
  if (!sender.tab || sender.frameId !== 0) return false;
  try {
    const url = new URL(sender.url);
    return ALLOWED_SCREENER_ORIGINS.has(url.origin)
      && /^\/(screen\/raw|login|register|accounts\/login|user\/login)(\/|$)/i.test(url.pathname);
  } catch {
    return false;
  }
}

function sameQueryPage(left, right, query) {
  const leftUrl = core.canonicalPageUrl(left, query);
  const rightUrl = core.canonicalPageUrl(right, query);
  return Boolean(leftUrl && rightUrl && core.pageNumber(leftUrl) === core.pageNumber(rightUrl));
}

function hasVisitedPage(state, value) {
  const target = core.canonicalPageUrl(value, state.query);
  if (!target) return false;
  const page = core.pageNumber(target);
  return state.visitedUrls.some((visited) => core.pageNumber(visited) === page);
}

function hasVisitedEveryPageBefore(state, targetPage) {
  const visitedPages = new Set(state.visitedUrls.map((visited) => core.pageNumber(visited)));
  for (let page = 1; page < targetPage; page += 1) {
    if (!visitedPages.has(page)) return false;
  }
  return true;
}

async function notifyApp(state, type, payload = {}) {
  if (state?.appTabId == null) return false;
  try {
    await chrome.tabs.sendMessage(state.appTabId, { type: `screener-${type}`, payload });
    return true;
  } catch {
    return false;
  }
}

function progressPayload(state) {
  return {
    jobId: state.jobId,
    status: state.status,
    phase: state.progress?.phase || 'Opening Screener',
    pages_completed: state.pagesCompleted,
    candidates_found: state.progress?.candidates_found ?? state.rows.length,
    detail: state.progress?.detail || '',
  };
}

async function reportProgress(state, status, phase, detail) {
  state.status = status;
  state.progress = {
    phase,
    detail,
    pages_completed: state.pagesCompleted,
    candidates_found: state.rows.length,
  };
  await saveState(state);
  await notifyApp(state, 'progress', progressPayload(state));
}

async function finishRefresh(state, error = null) {
  if (state.status === 'completed_pending_ack' || state.status === 'failed_pending_ack') return;
  state.finishedAt = Date.now();
  await chrome.alarms.clear(`${REFRESH_ALARM_PREFIX}${state.jobId}`);
  chrome.alarms.create(`${ACK_ALARM_PREFIX}${state.jobId}`, { delayInMinutes: ACK_TIMEOUT_MS / 60_000 });

  if (error) {
    state.status = 'failed_pending_ack';
    state.error = String(error).slice(0, 500);
    state.progress = {
      ...state.progress,
      phase: 'Failed',
      detail: state.error,
      pages_completed: state.pagesCompleted,
      candidates_found: state.rows.length,
    };
    state.rows = [];
    state.seenSymbols = [];
    await saveState(state);
    await notifyApp(state, 'error', { jobId: state.jobId, error: state.error });
  } else {
    state.status = 'completed_pending_ack';
    state.result = { page_count: state.pagesCompleted, rows: state.rows };
    state.progress = {
      phase: 'Complete',
      detail: `Collected ${state.rows.length} companies from ${state.pagesCompleted} pages.`,
      pages_completed: state.pagesCompleted,
      candidates_found: state.rows.length,
    };
    state.rows = [];
    state.seenSymbols = [];
    await saveState(state);
    await notifyApp(state, 'complete', {
      jobId: state.jobId,
      page_count: state.result.page_count,
      rows: state.result.rows,
    });
  }

  if (state.scrapeTabId != null) {
    try {
      await chrome.tabs.remove(state.scrapeTabId);
    } catch {}
  }
}

async function clearExpiredTerminalState(state) {
  if (!['completed_pending_ack', 'failed_pending_ack'].includes(state?.status)) return false;
  if (Date.now() - state.finishedAt < ACK_TIMEOUT_MS) return false;
  await discardStoredRefresh(state);
  return true;
}

async function startRefresh(message, sender) {
  if (!isAllowedAppSender(sender)) {
    return { ok: false, error: 'Start the refresh from Trend Investorr at localhost:3000.' };
  }
  if (typeof message.jobId !== 'string' || !/^[a-f0-9]{32}$/.test(message.jobId)) {
    return { ok: false, error: 'The refresh job ID is invalid. Reload the app and try again.' };
  }
  if (typeof message.query !== 'string' || !message.query.trim() || message.query.length > 5000) {
    return { ok: false, error: 'Enter a Screener query with at most 5,000 characters.' };
  }

  let active = await getState();
  if (active && await clearExpiredTerminalState(active)) active = null;

  // A new refresh supersedes a terminal result that the app never acknowledged.
  // The app has already requested a fresh result, so this temporary connector
  // payload must not block the new scan until the acknowledgement alarm fires.
  if (active && ['completed_pending_ack', 'failed_pending_ack'].includes(active.status)) {
    await discardStoredRefresh(active);
    active = null;
  }

  // Alarms normally expire these jobs, but recover here too in case Chrome
  // suspended or dropped the alarm while the service worker was inactive.
  if (active && ['running', 'waiting_for_login'].includes(active.status)) {
    const startedAt = Number(active.startedAt);
    if (!Number.isFinite(startedAt) || Date.now() - startedAt >= REFRESH_TIMEOUT_MS) {
      await discardStoredRefresh(active);
      active = null;
    }
  }

  // Clear malformed or obsolete persisted states instead of turning them into
  // a permanent refresh lock.
  if (active && !['running', 'waiting_for_login'].includes(active.status)) {
    await discardStoredRefresh(active);
    active = null;
  }
  if (active) return { ok: false, error: 'A Screener refresh is already active or waiting for the app to save its result.' };

  const query = message.query.trim();
  const state = {
    jobId: message.jobId,
    query,
    appTabId: sender.tab.id,
    scrapeTabId: null,
    status: 'running',
    rows: [],
    seenSymbols: [],
    visitedUrls: [],
    pendingUrl: core.initialPageUrl(query),
    needsTabReload: false,
    pagesCompleted: 0,
    startedAt: Date.now(),
    finishedAt: null,
    progress: {
      phase: 'Opening Screener',
      detail: 'Opening the query in your regular Chrome profile.',
      pages_completed: 0,
      candidates_found: 0,
    },
  };
  await saveState(state);
  chrome.alarms.create(`${REFRESH_ALARM_PREFIX}${state.jobId}`, { delayInMinutes: REFRESH_TIMEOUT_MS / 60_000 });
  try {
    const tab = await chrome.tabs.create({ url: state.pendingUrl, active: true });
    state.scrapeTabId = tab.id;
    await saveState(state);
    await notifyApp(state, 'progress', progressPayload(state));
    return { ok: true };
  } catch {
    await clearState(state);
    return { ok: false, error: 'Could not open a Screener tab in regular Chrome.' };
  }
}

async function handlePage(message, sender) {
  const state = await getState();
  if (!state || !['running', 'waiting_for_login'].includes(state.status)) return;
  if (sender.tab.id !== state.scrapeTabId) return;
  if (Date.now() - state.startedAt > REFRESH_TIMEOUT_MS) {
    await finishRefresh(state, 'Screener refresh timed out after ten minutes. Check the Chrome tab and try again.');
    return;
  }

  if (message.isLogin) {
    try {
      await chrome.tabs.update(state.scrapeTabId, { active: true });
    } catch {}
    await reportProgress(
      state,
      'waiting_for_login',
      'Screener sign-in',
      'Sign in to Screener in the highlighted Chrome tab. The refresh will continue when the results appear.',
    );
    return;
  }
  if (!message.hasTable) {
    await finishRefresh(state, message.error || 'Screener did not show a results table. Check the query and account access, then retry.');
    return;
  }

  const pageUrl = core.canonicalPageUrl(message.pageUrl, state.query);
  const senderPageUrl = core.canonicalPageUrl(sender.url, state.query);
  const expectedPageUrl = core.canonicalPageUrl(state.pendingUrl, state.query);
  let pageError = null;
  let recoveredStalePendingPage = false;
  if (!pageUrl || !senderPageUrl || !expectedPageUrl) {
    const expectedPage = expectedPageUrl ? core.pageNumber(expectedPageUrl) : 'unknown';
    const invalidUrls = [
      !pageUrl ? 'page-reported URL' : null,
      !senderPageUrl ? 'Chrome tab URL' : null,
      !expectedPageUrl ? 'expected URL' : null,
    ].filter(Boolean).join(', ');
    pageError = `Screener ${invalidUrls} did not match the active query/results route (expected page ${expectedPage}). No results were saved.`;
  } else if (!sameQueryPage(pageUrl, senderPageUrl, state.query)) {
    pageError = `The Screener page changed while it was being read (content page ${core.pageNumber(pageUrl)}, Chrome tab page ${core.pageNumber(senderPageUrl)}). No results were saved.`;
  } else if (!sameQueryPage(pageUrl, expectedPageUrl, state.query)) {
    const currentPage = core.pageNumber(pageUrl);
    const expectedPage = core.pageNumber(expectedPageUrl);
    recoveredStalePendingPage = currentPage === expectedPage + 1
      && hasVisitedPage(state, expectedPageUrl)
      && hasVisitedEveryPageBefore(state, currentPage);
    if (!recoveredStalePendingPage) {
      pageError = `Screener opened page ${currentPage}, but page ${expectedPage} was expected. No results were saved.`;
    }
  }
  if (pageError) {
    await finishRefresh(state, pageError);
    return;
  }
  if (hasVisitedPage(state, pageUrl)) {
    await finishRefresh(state, 'Screener repeated a results page. The refresh stopped without saving an incomplete list.');
    return;
  }

  const companyRowCount = Number(message.companyRowCount ?? message.rawRowCount);
  const parsedRowCount = Number(message.parsedRowCount);
  const receivedRows = Array.isArray(message.rows) ? message.rows : [];
  if (
    !Number.isSafeInteger(companyRowCount)
    || !Number.isSafeInteger(parsedRowCount)
    || companyRowCount <= 0
    || companyRowCount !== parsedRowCount
    || companyRowCount !== receivedRows.length
  ) {
    await finishRefresh(state, 'The Screener table layout changed, so not every displayed company could be read. No partial list was saved.');
    return;
  }
  const rows = receivedRows.map((row) => core.cleanCandidate(row));
  if (rows.some((row) => row === null)) {
    await finishRefresh(state, 'A Screener result row had an unreadable company name or symbol. No partial list was saved.');
    return;
  }

  if (recoveredStalePendingPage) state.pendingUrl = pageUrl;
  state.visitedUrls.push(pageUrl);
  const seen = new Set(state.seenSymbols);
  for (const candidate of rows) {
    if (seen.has(candidate.symbol)) continue;
    seen.add(candidate.symbol);
    state.rows.push({ ...candidate, rank: state.rows.length + 1 });
    if (state.rows.length > MAX_ROWS) {
      await finishRefresh(state, 'This query returned over 5,000 companies. Narrow the query and try again.');
      return;
    }
  }
  state.seenSymbols = [...seen];

  const currentPage = core.pageNumber(pageUrl);
  if (rows.length > 0) state.pagesCompleted += 1;
  state.progress = {
    phase: 'Reading Screener results',
    detail: `Read page ${currentPage}; found ${state.rows.length} companies so far.`,
    pages_completed: state.pagesCompleted,
    candidates_found: state.rows.length,
  };
  await saveState(state);
  if (rows.length > 0) await notifyApp(state, 'progress', progressPayload(state));

  const rawNextUrl = message.nextUrl;
  let nextUrl = rawNextUrl ? core.canonicalPageUrl(rawNextUrl, state.query) : null;
  if (rawNextUrl && !nextUrl) {
    await finishRefresh(state, 'Screener returned an unsafe or mismatched pagination link. No partial list was saved.');
    return;
  }
  if (!nextUrl) {
    await finishRefresh(state);
    return;
  }
  if (state.pagesCompleted >= MAX_PAGES) {
    await finishRefresh(state, `The query exceeded the ${MAX_PAGES}-page limit. Narrow the query and try again.`);
    return;
  }
  if (hasVisitedPage(state, nextUrl)) {
    await finishRefresh(state, 'Screener pagination repeated a page. The refresh stopped without saving an incomplete list.');
    return;
  }

  state.pendingUrl = nextUrl;
  state.needsTabReload = false;
  await saveState(state);
  return { nextUrl, delayMs: 1200 };
}

async function resumeStoredRefresh(state) {
  if (!state?.needsTabReload || !['running', 'waiting_for_login'].includes(state.status)) return;
  try {
    let tab = null;
    if (state.scrapeTabId != null) {
      try {
        tab = await chrome.tabs.get(state.scrapeTabId);
      } catch {}
    }
    if (tab) {
      await chrome.tabs.update(tab.id, { url: state.pendingUrl, active: false });
      state.scrapeTabId = tab.id;
    } else {
      const created = await chrome.tabs.create({ url: state.pendingUrl, active: false });
      state.scrapeTabId = created.id;
    }
    state.needsTabReload = false;
    await saveState(state);
  } catch {
    await finishRefresh(state, 'Chrome could not resume the Screener results tab. Start a new refresh.');
  }
}

async function recoverStoredRefresh() {
  const state = await getState();
  if (!state) return;
  if (['running', 'waiting_for_login'].includes(state.status)) {
    const remainingMs = REFRESH_TIMEOUT_MS - (Date.now() - state.startedAt);
    if (remainingMs <= 0) {
      await finishRefresh(state, 'Screener refresh timed out after ten minutes. Start a new refresh.');
      return;
    }
    state.needsTabReload = true;
    await saveState(state);
    chrome.alarms.create(`${REFRESH_ALARM_PREFIX}${state.jobId}`, {
      delayInMinutes: Math.max(0.5, remainingMs / 60_000),
    });
  } else if (['completed_pending_ack', 'failed_pending_ack'].includes(state.status)) {
    const remainingMs = ACK_TIMEOUT_MS - (Date.now() - state.finishedAt);
    if (remainingMs <= 0) {
      await clearState(state);
      return;
    }
    chrome.alarms.create(`${ACK_ALARM_PREFIX}${state.jobId}`, {
      delayInMinutes: Math.max(0.5, remainingMs / 60_000),
    });
  }
}

async function getRefreshStatus(sender) {
  if (!isAllowedAppSender(sender)) return { active: null };
  let state = await getState();
  if (state && await clearExpiredTerminalState(state)) state = null;
  if (!state) return { active: null };

  state.appTabId = sender.tab.id;
  await resumeStoredRefresh(state);
  state = await getState();
  if (!state) return { active: null };
  state.appTabId = sender.tab.id;
  await saveState(state);
  const active = {
    jobId: state.jobId,
    status: state.status,
    phase: state.progress?.phase || 'Opening Screener',
    pages_completed: state.pagesCompleted,
    candidates_found: state.progress?.candidates_found ?? state.rows.length,
    detail: state.progress?.detail || '',
  };
  if (state.status === 'completed_pending_ack') Object.assign(active, state.result);
  if (state.status === 'failed_pending_ack') active.error = state.error;
  return { active };
}

async function acknowledgeRefresh(message, sender) {
  if (!isAllowedAppSender(sender)) return { ok: false };
  const state = await getState();
  // Any authorized Trend Investorr tab can finish the save. A status request
  // from another app tab may have rebound appTabId since the result arrived.
  if (!state || state.jobId !== message.jobId) return { ok: false };
  if (!['completed_pending_ack', 'failed_pending_ack'].includes(state.status)) return { ok: false };
  await clearState(state);
  if (state.scrapeTabId != null) {
    try {
      await chrome.tabs.remove(state.scrapeTabId);
    } catch {}
  }
  return { ok: true };
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === 'start-refresh') {
    startRefresh(message, sender)
      .then(sendResponse)
      .catch(() => sendResponse({ ok: false, error: 'The Screener connector could not start.' }));
    return true;
  }
  if (message?.type === 'get-refresh-status') {
    getRefreshStatus(sender).then(sendResponse).catch(() => sendResponse({ active: null }));
    return true;
  }
  if (message?.type === 'ack-refresh') {
    acknowledgeRefresh(message, sender).then(sendResponse).catch(() => sendResponse({ ok: false }));
    return true;
  }
  if (message?.type === 'screener-page' && isAllowedScreenerSender(sender)) {
    handlePage(message, sender).then(
      (reply) => sendResponse(reply || { ok: true }),
      async () => {
        const state = await getState();
        if (state) await finishRefresh(state, 'The Screener connector could not read this results page. Retry the refresh.');
        sendResponse({ ok: false });
      },
    );
    return true;
  }
  return false;
});

chrome.tabs.onRemoved.addListener(async (tabId) => {
  const state = await getState();
  if (state?.scrapeTabId === tabId && ['running', 'waiting_for_login'].includes(state.status)) {
    await finishRefresh(state, 'The Screener results tab was closed before the refresh finished. Try again.');
  }
});

chrome.runtime.onStartup.addListener(() => {
  recoverStoredRefresh().catch(() => {});
});

chrome.runtime.onInstalled.addListener((details) => {
  if (details.reason === 'install' || details.reason === 'update') {
    recoverStoredRefresh().catch(() => {});
  }
});

chrome.alarms.onAlarm.addListener(async (alarm) => {
  const state = await getState();
  if (!state) return;
  if (alarm.name === `${REFRESH_ALARM_PREFIX}${state.jobId}` && ['running', 'waiting_for_login'].includes(state.status)) {
    await finishRefresh(state, 'Screener refresh timed out after ten minutes. Check the Chrome tab and try again.');
  } else if (alarm.name === `${ACK_ALARM_PREFIX}${state.jobId}`) {
    await clearExpiredTerminalState(state);
  }
});
