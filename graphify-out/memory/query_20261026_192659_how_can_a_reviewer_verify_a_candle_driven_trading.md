---
type: "query"
date: "2026-10-26T19:26:59.791131+00:00"
question: "How can a reviewer verify a candle-driven trading project change without market candles or PostgreSQL?"
contributor: "graphify"
source_nodes: ["RiskManager", "OrderManager", "PaperTrader", "Signal", "Position", "Database"]
---

# Q: How can a reviewer verify a candle-driven trading project change without market candles or PostgreSQL?

## Answer

For the proposed sector-position-cap task, candles and the database are upstream inputs used to generate real trading signals. The downstream safeguard only needs a signal with a sector, the configured cap, and current open positions. OrderManager and RiskManager can be exercised in memory with synthetic Signal, AnalysisResult, and Position objects; PaperTrader stores positions in memory, and existing integration tests already build the signal-to-trade pipeline without starting a database or loading candle history. A reviewer can verify boundary cases: reject a BUY when the sector is at its cap, allow another sector, allow SELL orders that reduce exposure, and exercise the chosen policy for missing sector metadata. The current implementation is incomplete because RiskManager uses a placeholder count, Position has no sector field, and OrderManager's sector argument defaults to an empty string. If the task instead changed candle ingestion or indicator calculations, the reviewer would need controlled candle fixtures or a test database; that is a broader, separate task. No tests were run for this explanation.

## Source Nodes

- RiskManager
- OrderManager
- PaperTrader
- Signal
- Position
- Database