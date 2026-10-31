---
type: "query"
date: "2026-10-26T19:43:08.213811+00:00"
question: "What substantial Python Code Behaviors task is grounded in the current live candle pipeline?"
contributor: "graphify"
source_nodes: ["CandleAggregator", "CandleBucket", "Tick", "CandleStore", "DataIngestionService"]
---

# Q: What substantial Python Code Behaviors task is grounded in the current live candle pipeline?

## Answer

The WebSocket parser maps volume_trade_for_the_day into Tick.volume. CandleBucket.update stores that cumulative field directly as the interval candle volume and weights each price by the cumulative value for VWAP. For prior cumulative 1000, then 1040 at price 100 and 1050 at price 110 in a five-minute bucket, correct incremental volume is 50 and VWAP is 102, but current bucket reports volume 1050 and weights 1040 and 1050. CandleAggregator._do_flush clears pending candles before insert and drops the batch on failure. CandleStore upserts bars by stock_id/timestamp, and daily aggregation sums intraday volume. A coherent task is to make live five-minute candle volumes/VWAP derive from per-tick deltas, define first-tick and daily reset behavior, preserve pending data for retry on failed flush, and add deterministic tests using synthetic Tick values and a fake store. No dedicated candle aggregator test exists currently. This is static code analysis; isolated reproduction could not import the module in the default Python environment because loguru was not installed.

## Source Nodes

- CandleAggregator
- CandleBucket
- Tick
- CandleStore
- DataIngestionService