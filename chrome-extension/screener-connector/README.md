# Trend Investorr Screener Connector

This Manifest V3 extension reads Screener.in result tables in the same regular Chrome profile that runs Trend Investorr. Chrome opens the query tab and supplies the existing Screener sign-in session. The extension never requests account credentials or cookie access.

## Install in the Chrome profile you use for both sites

1. In regular Chrome, open `chrome://extensions`.
2. Turn on **Developer mode**.
3. Select **Load unpacked** and choose this `screener-connector` folder.
4. In that same Chrome profile, open Trend Investorr at `http://localhost:3000` or `http://127.0.0.1:3000`, then reload the app page.

The extension requires Chrome 114 or later. After changing the extension files, press **Reload** on its `chrome://extensions` card, then reload the Trend Investorr page.

## Refresh behavior

The Master scanner sends the saved query to the extension. Chrome opens the first Screener results page; if Screener requests sign-in, complete it in that tab. The connector detects company rows from Screener company links and table column labels, so heading rows and layout-only rows do not count as companies. It checks that every detected company row produced a valid name and symbol, then follows the Screener pagination links with a short pause between pages. An unexpected page URL, unreadable company row, missing results table, repeated page, or timeout marks the refresh as failed and preserves the last successful candidate list. Backend refresh failures are recorded in `logs/screener-query.log` with a reason and page numbers, but without query text or full URLs.

The extension stores the query and collected result rows temporarily in this Chrome profile so a background worker restart or Chrome restart does not discard a completed result. The completed result remains available until the app confirms that it saved it; unacknowledged results expire after 20 minutes. Active refreshes time out after 10 minutes. The app keeps the saved query and last successful update time.

## Permissions and data handling

- `storage`: keeps refresh progress and the temporary result in this Chrome profile. It does not store authentication credentials.
- `alarms`: expires stuck refreshes and removes results the app did not acknowledge.
- Page access: Screener.in pages and local Trend Investorr pages. The app bridge accepts commands only from port 3000 on `localhost` or `127.0.0.1`; the Screener reader only accepts the query results and sign-in routes.
- The connector sends parsed results to the local Trend Investorr app. It does not call an external service or access browser cookies.

## Focused checks

From the project root, run:

```sh
node --test chrome-extension/screener-connector/tests/*.test.cjs
node --check chrome-extension/screener-connector/service-worker.js
node --check chrome-extension/screener-connector/screener-content.js
```

The tests cover query URL validation, Screener number and row parsing, pagination, result retention until acknowledgement, incomplete-row rejection, and the local-app origin check.
