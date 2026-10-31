---
type: "query"
date: "2026-10-26T19:19:52.964926+00:00"
question: "Does completing the sector position cap require a live database in Taiga?"
contributor: "graphify"
source_nodes: ["RiskManager", "OrderManager", "PaperTrader", "Signal", "Database"]
---

# Q: Does completing the sector position cap require a live database in Taiga?

## Answer

The current Python order path is in memory: RiskManager receives positions, OrderManager obtains them from PaperTrader, and PaperTrader stores positions in a dictionary. The sector cap is unfinished: the guard uses a placeholder count, Signal has a sector field, the normal main path omits the sector argument, and Position does not retain sector. This task can be bounded to propagating sector metadata and testing the buy cap, a different-sector allowance, sell bypass, and missing-sector behavior with in-memory tests. It does not require a live database.

## Source Nodes

- RiskManager
- OrderManager
- PaperTrader
- Signal
- Database