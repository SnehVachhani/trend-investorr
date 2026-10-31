(() => {
  const prefix = 'trend-investorr:screener:';
  const allowedHosts = new Set(['localhost', '127.0.0.1']);
  const allowedPort = '3000';

  if (
    window.location.protocol !== 'http:'
    || !allowedHosts.has(window.location.hostname)
    || window.location.port !== allowedPort
  ) return;

  function postToPage(type, payload = {}) {
    window.postMessage({ ...payload, type: `${prefix}${type}` }, window.location.origin);
  }

  function sendRuntimeMessage(message) {
    // Wrap the call itself as well as its returned promise: Chrome can throw
    // synchronously when the extension context is being unloaded or reloaded.
    return Promise.resolve().then(() => chrome.runtime.sendMessage(message));
  }

  function sendStatusRequest() {
    sendRuntimeMessage({ type: 'get-refresh-status' }).then((reply) => {
      if (reply?.active) postToPage('status', reply.active);
    }).catch(() => {});
  }

  window.addEventListener('message', (event) => {
    if (event.source !== window || event.origin !== window.location.origin) return;
    const message = event.data;
    if (!message || typeof message !== 'object') return;
    if (message.type === `${prefix}ping`) {
      postToPage('ready', { nonce: message.nonce });
      return;
    }
    if (message.type === `${prefix}status-request`) {
      sendStatusRequest();
      return;
    }
    if (message.type === `${prefix}ack`) {
      sendRuntimeMessage({ type: 'ack-refresh', jobId: message.jobId }).catch(() => {});
      return;
    }
    if (message.type === `${prefix}start`) {
      sendRuntimeMessage({
        type: 'start-refresh',
        jobId: message.jobId,
        query: message.query,
      }).then((reply) => {
        if (!reply?.ok) postToPage('error', { jobId: message.jobId, error: reply?.error || 'The Chrome connector could not start the refresh.' });
      }).catch(() => {
        postToPage('error', { jobId: message.jobId, error: 'Chrome could not contact the Screener connector. Reload the app and try again.' });
      });
    }
  });

  chrome.runtime.onMessage.addListener((message) => {
    if (!message || typeof message.type !== 'string' || !message.type.startsWith('screener-')) return;
    postToPage(message.type.slice('screener-'.length), message.payload);
  });
})();
