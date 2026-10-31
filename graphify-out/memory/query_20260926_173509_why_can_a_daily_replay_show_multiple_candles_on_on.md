---
type: "query"
date: "2026-09-26T17:35:09.694489+00:00"
question: "Why can a daily replay show multiple candles on one session date, and should it filter bars to regular NSE market hours?"
contributor: "graphify"
source_nodes: ["CandleStore", "HistoricalFetcher", "BhavcopyFetcher", "DataIngestionService", "ProfileBacktester"]
---

# Q: Why can a daily replay show multiple candles on one session date, and should it filter bars to regular NSE market hours?

## Answer

The data issue is that candles_1d is keyed by stock_id and full timestamp rather than stock_id and exchange session date, so rows timestamped at 00:00, 05:30, and 15:30 IST can all coexist for one stock-day. Historical fetch preserves provider timestamps; bhavcopy and 5m aggregation can write a daily record at 15:30. Replay selects all rows and walks them as separate daily bars; higher-timeframe intraday context can also expose a complete daily OHLC bar before session close. This creates duplicate pseudo-bars and lookahead risk. For 1m/5m bars, normalize timestamps/timezones and enforce the NSE regular session, normally 09:15–15:30 IST (09:00–09:15 is pre-open; 09:30 is not the normal open). For daily bars, do not filter by time of day: normalize to one canonical exchange session date, reconcile source and corporate-action adjustment basis, and make the full daily OHLC available only after the close; intraday strategies use the prior completed daily bar. The current enhanced momentum returns should not be trusted until the duplicate daily rows and adjustment inconsistencies are reconciled and replayed.

## Source Nodes

- CandleStore
- HistoricalFetcher
- BhavcopyFetcher
- DataIngestionService
- ProfileBacktester