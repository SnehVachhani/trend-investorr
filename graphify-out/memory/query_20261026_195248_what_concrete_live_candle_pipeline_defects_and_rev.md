---
type: "query"
date: "2026-10-26T19:52:48.506790+00:00"
question: "What concrete live candle pipeline defects and reviewable task scope were verified?"
contributor: "graphify"
source_nodes: ["CandleAggregator", "CandleBucket", "Tick", "CandleStore", "StockStore"]
---

# Q: What concrete live candle pipeline defects and reviewable task scope were verified?

## Answer

Confirmed in current Python project: websocket_manager._parse_tick maps volume_trade_for_the_day to Tick.volume; CandleBucket.update overwrites five-minute candle volume with that daily cumulative value and weights VWAP by cumulative values. A synthetic prior cumulative 1000 followed by ticks 1040 at price 100 and 1050 at price 110 yielded active bucket volume 1050 and VWAP 105.02, while interval volume should be 50 and VWAP 102. CandleAggregator._do_flush clears the completed queue before CandleStore.insert_batch; a fake store raising once left pending_candles at 0, losing two queued bars. Main constructs CandleAggregator(db=db, interval_min=5) without token_to_stock_id and no set_token_mapping call exists; a fake store captured stock_id=None. Database schema allows NULL stock_id and unique(stock_id,timestamp); downstream get_candles filters by stock_id and daily aggregate sums volume. A coherent Python task is to fix per-interval volume/VWAP, define first-observation and session reset policies, wire known token-to-stock ID mapping, avoid NULL-ID candle rows, and preserve/retry in-process completed bars after transient insert errors, with offline synthetic tick and fake-store tests. No source files were edited.

## Source Nodes

- CandleAggregator
- CandleBucket
- Tick
- CandleStore
- StockStore