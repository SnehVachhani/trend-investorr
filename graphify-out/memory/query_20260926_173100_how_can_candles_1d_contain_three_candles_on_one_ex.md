---
type: "query"
date: "2026-09-26T17:31:00.702614+00:00"
question: "How can candles_1d contain three candles on one exchange date?"
contributor: "graphify"
source_nodes: ["CandleStore", "HistoricalFetcher", "BhavcopyFetcher", "DataIngestionService", "ProfileBacktester"]
---

# Q: How can candles_1d contain three candles on one exchange date?

## Answer

These are duplicate daily representations, not three NSE sessions. The candles_1d table is unique by exact (stock_id, timestamp), and CandleStore upserts on that exact pair. HistoricalFetcher preserves the provider timestamp; Bhavcopy and 5-minute aggregation timestamp daily rows at 15:30. No session-date canonicalization or source provenance is stored. In TEMBO 2025-06-26 data, 00:00 and 15:30 rows are on the ~₹48 scale; 05:30 is ~₹478 with one-tenth volume. The 15:30 OHLCV exactly matches the 5-minute aggregate. This is consistent with adjusted/unadjusted split-history mixed in one daily table; exact provenance of the 00:00 and 05:30 rows cannot be recovered because source is not stored.

## Source Nodes

- CandleStore
- HistoricalFetcher
- BhavcopyFetcher
- DataIngestionService
- ProfileBacktester