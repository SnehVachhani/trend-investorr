# Upstox fundamentals pipeline

This repository now has a read-only Upstox company-fundamentals pipeline. It
uses the Analytics Token only for `GET` requests and keeps the existing Angel
One connection responsible for market data and trading.

## Required configuration

Add these values to `.env`:

```dotenv
UPSTOX_ANALYTICS_TOKEN=replace_with_your_upstox_analytics_token
UPSTOX_API_BASE_URL=https://api.upstox.com/v2
UPSTOX_INSTRUMENTS_URL=https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz
```

The Analytics Token is stored in the encrypted application vault on startup.
It is read-only and is not used for order placement. The remaining settings
are optional controls:

| Setting | Default | Purpose |
|---|---:|---|
| `UPSTOX_HTTP_TIMEOUT_SECONDS` | `20` | Per-request timeout |
| `UPSTOX_MAX_RETRIES` | `3` | Bounded retry attempts for transport, server and rate-limit responses |
| `UPSTOX_MAX_REQUESTS_PER_RUN` | `2000` | Logical ISIN/endpoint jobs per batch |
| `UPSTOX_REQUESTS_PER_MINUTE` | `500` | Maximum physical HTTP attempts per rolling minute |
| `UPSTOX_REQUESTS_PER_30_MINUTES` | `2000` | Maximum physical HTTP attempts per rolling 30-minute window, including retries |
| `UPSTOX_MAX_BATCHES_PER_CYCLE` | `24` | Maximum batches in one daily or Saturday cycle |
| `UPSTOX_BATCH_INTERVAL_MINUTES` | `30` | Wait after each completed batch while due work remains |
| `UPSTOX_MAX_STOCKS_PER_RUN` | `0` | Maximum stocks per run; `0` means all active NSE equities |
| `UPSTOX_SYNC_HOUR` | `16` | Daily sync hour in IST |
| `UPSTOX_SYNC_MINUTE` | `20` | Daily sync minute in IST |
| `UPSTOX_WEEKLY_SYNC_HOUR` | `0` | Saturday statement cycle hour in IST |
| `UPSTOX_WEEKLY_SYNC_MINUTE` | `0` | Saturday statement cycle minute in IST |
| `UPSTOX_PROFILE_REFRESH_DAYS` | `30` | Profile and sector refresh cadence |
| `UPSTOX_STATEMENT_REFRESH_DAYS` | `7` | Income, balance-sheet and cash-flow refresh cadence |
| `UPSTOX_RATIO_REFRESH_DAYS` | `1` | Key-ratio refresh cadence |
| `UPSTOX_SHAREHOLDING_REFRESH_DAYS` | `7` | Shareholding refresh cadence |
| `UPSTOX_CORPORATE_ACTION_REFRESH_DAYS` | `1` | Dividend, split, bonus and rights refresh cadence |
| `UPSTOX_COMPETITOR_REFRESH_DAYS` | `30` | Competitor refresh cadence |

## First run

After PostgreSQL and the application environment are available, run a small
one-batch credential/mapping check:

```bash
./.venv/bin/python scripts/sync_fundamentals.py --limit 10
```

Use a small limit to validate credentials and data mapping. Omitting `--limit`
uses the full stock universe but still runs only one bounded batch. The API
backend runs daily feeds every day and statement/monthly feeds on Saturdays,
with additional batches and cooldowns handled by its scheduler.

To manually run one additional bounded batch:

```bash
./.venv/bin/python scripts/sync_fundamentals.py
```

For a one-time full due-only fill, run sequential batches and cooldowns until
the current backlog is drained:

```bash
./.venv/bin/python scripts/sync_fundamentals.py --all-batches
```

This mode holds the shared sync-cycle lock for the entire cycle so the API
scheduler or another CLI process cannot interleave calls during cooldowns.
Do not combine it with `--force`; completed rows would otherwise be fetched
again in each batch.

Use `--force` only when a full refresh is intentional. The normal scheduler
runs at the configured IST time, refreshes only data whose cadence is due, and
can resume after a restart. Failures are stored per stock and endpoint, with a
30-minute retry due time.

## What is stored

For each active NSE equity that can be mapped to an ISIN, the pipeline stores:

- the stable NSE/Upstox ISIN bridge;
- raw dated endpoint payloads for audit and future re-normalization;
- company profile and sector text;
- yearly and quarterly income-statement line items;
- balance-sheet and yearly cash-flow line items;
- current company ratios and sector comparison values;
- quarterly shareholding history;
- corporate actions and their event dates/details;
- competitor snapshots;
- per-endpoint sync status, retry state and payload hashes.

The data is exposed through:

```text
GET /api/stocks/{symbol}/fundamentals
GET /api/system/fundamentals-sync
```

The first endpoint returns the latest profile and ratios, ratio snapshots,
normalized statement/shareholding/action/competitor history, and the latest
successful raw response for each Upstox endpoint variant. The second endpoint
includes per-endpoint universe, mapping, freshness, due and failure coverage,
plus the most recent batch runs.

## Current field boundary

The Upstox fundamentals endpoints directly cover sector/profile text, annual
and quarterly income history, balance-sheet history, cash-flow history,
quarterly shareholding, P/E, P/B, ROA, ROE, ROCE, EV/EBITDA and sector
benchmarks, plus corporate actions and competitors. The key-ratio endpoint
calls its comparison field `sector_value`; that is usable as a sector/industry
benchmark, but it is not a separately named `industry_pe` field.

EPS is retained when it appears in the income statement's full statement; the
provider documents that detailed `full_statement` data is annual even when the
headline income query is quarterly. The pipeline does not claim that Upstox
directly supplies founded year, face value, dividend yield, debt-to-equity,
book value per share or company market cap.
Those require either a separate licensed data source or a derived-metrics
layer. The existing design deliberately retains raw responses so derived
metrics can be added without another historical re-download.

## Coverage, cadence, and expected time

The API process owns the scheduler, so the sync continues when the backend is
running without the separate trading-engine process. Daily feeds start at
16:20 IST every day. The statement and shareholding cycle starts Saturday at
00:00 IST; profile and competitor refreshes are included in that cycle only
when their 30-day cadence makes them due. Failures get a 30-minute retry due
time; they are picked up by a later pending batch or the next scheduled cycle.

Upstox publishes limits of 50 requests/second, 500/minute, and 2,000/30 minutes
for its Other Standard APIs, enforced per API and user. Fundamentals is not
listed as a separate rate-limit category, so this pipeline deliberately shares
one conservative quota across all its Fundamentals calls. It paces at no more
than 500 physical HTTP attempts per rolling minute and 2,000 per rolling 30
minutes, counting retries. Each batch contains at most 2,000 logical
ISIN/endpoint jobs. If work remains, the next batch starts after a 30-minute
wait measured from completion of the preceding batch. Upstox does not document
whether its windows are fixed or rolling; the local rolling-window limiter is
safe under either interpretation. The minute cap is about 8.3 requests/second,
well below the published 50/second ceiling. See Upstox's [rate-limit
table](https://upstox.com/developer/api-documentation/rate-limiting/).

At the last database count, the active NSE equity universe contained 4,153
listing rows but only 3,498 distinct non-empty ISINs. Alternate NSE series for
the same company share an ISIN. The service now makes one provider request per
ISIN and endpoint, then stores the response against each local listing alias;
it does not repeat the same API request for each series. Index instruments are
not in this company-equity universe and have no company ISIN for these
fundamentals calls.

| Feed | Endpoints | Cadence | Calls per full due cycle | Batches | Minimum elapsed time* |
|---|---:|---|---:|---:|---:|
| Ratios and corporate actions | 2 | Daily | 6,996 | 4 | About 1h 44m |
| Statements and shareholding | 7 | Saturday | 24,486 | 13 | About 6h 49m |
| Profile and competitors | 2 | Every 30 days, checked Saturday | 6,996 when due | 4 alone; 3 added to weekly | About 1h 44m |
| Saturday when monthly feeds are due | 9 total | Saturday | 31,482 | 16 | About 8h 33m |

\* Estimates assume the full 500/minute pace, 2,000 requests per batch, and a
30-minute wait after every batch except the last. Network latency, provider
errors, retries, database persistence, mapping changes, and other clients using
the same Upstox API quota can increase elapsed time. On a Saturday when monthly
feeds are due, the 9 weekly/monthly endpoints take about 8h 33m; the separate
daily run starts at 16:20 and takes about 1h 44m. On the first Saturday
population, all 11 retained request variants total 38,478 unique-ISIN endpoint
jobs. The configured batch ceiling is 24, which covers the 16-batch Saturday
case.

Upstox announces eight company-specific API groups ([fundamentals API
overview](https://upstox.com/developer/api-documentation/announcements/company-fundamentals-api/)).
The initial implementation expanded these into 13 request variants, including
separate yearly and quarterly income queries. The [income statement
reference](https://upstox.com/developer/api-documentation/get-income-statement/)
says `full_statement` is annual regardless of the `time_period` filter, so a
quarterly query with `fs=true` already returns annual detailed line items. The
sync now requests quarterly income for standalone and consolidated only,
keeping the quarterly headline data and annual detailed line items while
avoiding two redundant yearly requests per company. Separate annual headline
category and change fields from the yearly query are therefore not refreshed.
With that reduction, the retained 8 API groups produce 11 request variants:
two income, two balance-sheet, two cash-flow, and one each for profile,
shareholdings, ratios, corporate actions, and competitors.

The Upstox documentation exposes these as company-specific ISIN requests and
does not document a bulk Fundamentals endpoint; combining multiple companies
into one request is not currently available in the documented suite. One
returned payload is fanned out to every local listing alias for that ISIN.
Income headline summaries are quarterly; their detailed full-statement lines
remain annual. Balance-sheet and cash-flow histories are annual in the
documented response. The normalized line items are retained with their
provider periods, not mislabeled as quarterly.

The cycle stops early when no selected endpoint has due work. Coverage and
actual elapsed times are available from `GET /api/system/fundamentals-sync`.
The published rate-limit page lists standard API limits but does not state
whether its windows are fixed or rolling, so local enforcement intentionally
uses rolling windows and serializes this application's API cycles.

The initial pipeline exposes these records through the backend API; it does not
yet add a fundamentals dashboard or use the data as a trading signal. The API
token is read-only and must be configured in `.env` or the encrypted vault. A
live population cannot start until that token is present and the API process is
restarted.

## Implementation phases

1. **Provider boundary** — token, read-only client, retry/rate-limit handling,
   instrument-master caching and ISIN resolution.
2. **Durable ingestion** — idempotent PostgreSQL migrations, raw snapshots,
   normalized tables and per-endpoint due state.
3. **Scheduled operation** — daily and Saturday due-only syncs, bounded request budget,
   startup catch-up, structured run statistics and failure retry state.
4. **Derived fundamentals** — calculate only fields that can be reconstructed
   from stored statements and market data, with explicit as-of dates.
5. **Strategy integration** — add fundamental filters only after the data has
   point-in-time semantics and enough history; never use today's ratio in a
   historical backtest.
6. **Decision validation** — compare technical-only versus technical-plus-
   fundamentals strategies after charges, slippage and data availability
   are included.
