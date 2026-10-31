---
type: "query"
date: "2026-09-24T15:47:34.154299+00:00"
question: "Does candle synchronization persist coverage and fetch only missing candle data, and what are its limitations?"
contributor: "graphify"
source_nodes: ["DataIngestionService", "CandleCoverageStore"]
---

# Q: Does candle synchronization persist coverage and fetch only missing candle data, and what are its limitations?

## Answer

The code persists one candle_coverage record per stock and interval with earliest_ts, latest_ts, candle_count, and history_complete, plus the last result/time in candle_sync_checks (storage/models.py L898-L1000; storage/database.py L553-L589). The startup backfill reconciles earliest/latest/count from the actual candle table before each API interval (data/data_ingestion.py L904-L935; storage/models.py L1108-L1143). backfill() then requests the full lookback when coverage is absent, an older prefix when earliest_ts is newer than the requested cutoff, and a newer tail when latest_ts is older than freshness_cutoff; otherwise it skips (data/data_ingestion.py L173-L206, L237-L279). Successful ranges update candle tables and coverage (data/data_ingestion.py L329-L350). This is edge-range coverage, not an exact inventory of missing timestamps: internal holes between earliest_ts and latest_ts are not detected. candle_count is not used for gap detection and is overwritten with the size of each latest fetch response during upsert (storage/models.py L935-L943). Freshness is also date-granular: any latest candle at or after that trading date's midnight qualifies as fresh, even if an intraday series is incomplete later that session (data/data_ingestion.py L88-L108, L251-L254).

## Source Nodes

- DataIngestionService
- CandleCoverageStore