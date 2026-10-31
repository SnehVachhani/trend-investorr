---
type: "query"
date: "2026-09-25T22:29:50.962991+00:00"
question: "Why did many fundamental 5-minute setup backtests fail with invalid OHLCV candles, what does the database contain, and how can the issue be fixed safely?"
contributor: "graphify"
source_nodes: ["HistoricalFetcher", "DataIngestionService", "CandleStore", "Database", "server.py"]
---

# Q: Why did many fundamental 5-minute setup backtests fail with invalid OHLCV candles, what does the database contain, and how can the issue be fixed safely?

## Answer

I inspected the latest database backtest (78 failed stocks). The first invalid candle for 73 stocks had negative volume; five more had OHLC geometry errors (high below open/close or low above open/close). Seventy-four failed stocks had a row at Saturday 2024-03-02 11:15 IST with flat OHLC and negative volume. Across the 78 failed symbols in the queried history range, I found 480 malformed 5-minute rows: 400 with negative volume, 63 with high mismatch, and 59 with low mismatch; these categories overlap. The historical parser converts provider values and stores them without OHLCV semantic validation, while the candles_5m table previously had no matching CHECK constraint. A single invalid row aborts a standalone stock setup batch. The code now shares OHLCV validation, rejects malformed historical candles, filters legacy malformed bars from fundamental and filter backtest inputs, reports skipped-candle totals in the existing Data notes panel, guards realtime ticks, and adds a NOT VALID candles_5m constraint to enforce future writes without scanning legacy rows. A real database check of ACUTAAS stock_id 1945 now loads and validates 55,717 good bars while excluding its one invalid bar. The legacy rows were not deleted while startup candle synchronization was running; the API process needs a restart after that sync to activate these code changes and migration. Tests: 61 focused tests passed.

## Source Nodes

- HistoricalFetcher
- DataIngestionService
- CandleStore
- Database
- server.py