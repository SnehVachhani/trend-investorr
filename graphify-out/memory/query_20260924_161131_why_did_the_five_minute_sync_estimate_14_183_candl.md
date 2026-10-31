---
type: "query"
date: "2026-09-24T16:11:31.489505+00:00"
question: "Why did the FIVE_MINUTE sync estimate 14,183 candles or requests?"
contributor: "graphify"
source_nodes: ["DataIngestionService", "HistoricalFetcher", "CandleCoverageStore"]
---

# Q: Why did the FIVE_MINUTE sync estimate 14,183 candles or requests?

## Answer

14,183 was the estimated number of API request chunks, not candle rows. At the 2026-09-24 21:40 IST database snapshot it split into 10,133 older-prefix chunks plus 4,050 newer-tail chunks across 4,047 stocks (3 more chunks than one per stock). FIVE_MINUTE backfill is configured for 1,095 days, and HistoricalFetcher splits older ranges into 100-day requests. The old-prefix chunks were all associated with 1,542 coverage rows where history_complete=true, because _missing_ranges requests an older prefix whenever earliest_ts is later than the cutoff without checking history_complete. Those 10,133 calls are therefore repeat work under the current logic. The 4,050 tail chunks are roughly one current-tail request per stock; they can return many bars, up to around 75 for a fully traded session, not one. Thus the number is not 14,183 candles. Sources: data/data_ingestion.py L173-L206; data/fetchers/historical_fetcher.py L396-L415; storage/models.py L967-L975.

## Source Nodes

- DataIngestionService
- HistoricalFetcher
- CandleCoverageStore