---
type: "query"
date: "2026-09-24T15:51:44.749463+00:00"
question: "For one missing latest candle, how does the SmartAPI sync fetch it and write it to the database?"
contributor: "graphify"
source_nodes: ["HistoricalFetcher", "CandleStore", "CandleCoverageStore"]
---

# Q: For one missing latest candle, how does the SmartAPI sync fetch it and write it to the database?

## Answer

In the Angel One SmartAPI candle sync, backfill reads candle_coverage for each stock and interval. If a known latest timestamp is behind freshness_cutoff, it computes the tail start as max(latest_ts + interval step, lookback cutoff) and calls HistoricalFetcher.get_candles_range(start, now). A one-day request shorter than the API chunk limit is one getCandleData request for one symboltoken and interval. Returned bars are sorted and mapped to columns, then CandleStore.insert_batch upserts into the interval table by (stock_id, timestamp), replacing values on conflict. CandleCoverageStore.upsert then merges earliest/latest timestamps and updates source/last_updated; its candle_count is set to the returned batch size, not the total table count. candle_sync_checks records result=data and check time. If the missing intraday candle is internal, or the latest intraday candle is already on the current date, the boundary/date freshness logic may skip it; it is not an exact missing-timestamp detector. Weekly candles are aggregated locally from daily rows after the API intervals.

## Source Nodes

- HistoricalFetcher
- CandleStore
- CandleCoverageStore