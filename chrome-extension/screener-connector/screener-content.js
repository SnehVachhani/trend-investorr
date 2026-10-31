(() => {
  if (!/^\/(screen\/raw|login|register|accounts\/login|user\/login)(\/|$)/i.test(location.pathname)) return;
  const core = globalThis.ScreenerConnectorCore;
  const PAGE_READY_TIMEOUT_MS = 25_000;
  const LOGIN_WAIT_TIMEOUT_MS = 10 * 60_000;
  const RETRY_DELAY_MS = 800;
  const startedAt = Date.now();
  let loginReported = false;
  let pageReported = false;
  let previousRowCount = -1;
  let stableRowCountPolls = 0;

  function sendPage(message) {
    chrome.runtime.sendMessage({ type: 'screener-page', ...message }).then((reply) => {
      if (!reply?.nextUrl) return;
      const query = new URL(location.href).searchParams.get('query');
      const nextUrl = core.canonicalPageUrl(reply.nextUrl, query);
      if (!nextUrl) return;
      window.setTimeout(() => location.assign(nextUrl), Math.max(0, Number(reply.delayMs) || 0));
    }).catch(() => {});
  }

  function pollPage() {
    if (pageReported) return;
    const table = core.findResultsTable(document);
    const login = core.isLoginPage(location, document);
    const elapsed = Date.now() - startedAt;
    const renderedRowCount = table
      ? (table.querySelectorAll('tr').length || table.querySelectorAll('tbody tr').length || 0)
      : 0;

    if (login && (!table || renderedRowCount === 0)) {
      if (!loginReported) {
        loginReported = true;
        sendPage({ pageUrl: location.href, isLogin: true });
      }
      if (elapsed < LOGIN_WAIT_TIMEOUT_MS) {
        window.setTimeout(pollPage, RETRY_DELAY_MS);
      }
      return;
    }

    if ((!table || renderedRowCount === 0) && elapsed < PAGE_READY_TIMEOUT_MS) {
      window.setTimeout(pollPage, RETRY_DELAY_MS);
      return;
    }
    if (table && renderedRowCount > 0 && elapsed < PAGE_READY_TIMEOUT_MS) {
      stableRowCountPolls = renderedRowCount === previousRowCount ? stableRowCountPolls + 1 : 1;
      previousRowCount = renderedRowCount;
      if (stableRowCountPolls < 2) {
        window.setTimeout(pollPage, RETRY_DELAY_MS);
        return;
      }
    }

    pageReported = true;
    if (!table) {
      sendPage({
        pageUrl: location.href,
        isLogin: false,
        hasTable: false,
        error: login ? 'Screener sign-in did not finish within ten minutes.' : 'Screener results did not render within 25 seconds.',
      });
      return;
    }

    try {
      const parsed = core.parseTable(table);
      sendPage({
        pageUrl: location.href,
        isLogin: false,
        hasTable: true,
        companyRowCount: parsed.companyRowCount,
        parsedRowCount: parsed.rows.length,
        rows: parsed.rows,
        nextUrl: core.nextPageUrl(document, core.pageNumber(location.href)),
      });
    } catch {
      sendPage({
        pageUrl: location.href,
        isLogin: false,
        hasTable: false,
        error: 'The Screener results table could not be read. No results were saved.',
      });
    }
  }

  pollPage();
})();
