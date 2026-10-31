---
type: "query"
date: "2026-10-28T18:29:20.497041+00:00"
question: "What is the next substantial task after candle recovery, restart handling, and live square-off?"
contributor: "graphify"
source_nodes: ["OrderManager", "RiskManager", "Position", "Signal"]
---

# Q: What is the next substantial task after candle recovery, restart handling, and live square-off?

## Answer

The existing graph supplies earlier OrderManager, Position, RiskManager, and Signal context but does not fully describe the newly synchronized live square-off code. Direct-source research and offline reproductions are recorded in reports/next-code-behaviors-task-20261028.md. The recommended task is broker-backed LIVE entry limits with durable pending-order reservations and conservative submission outcome handling. A selected existing regression baseline has 223 passes; eight separate offline probes expose missing broker entry checks, missing pending capital reservations, duplicate submissions across restarts, an installed SDK response-contract mismatch, and duplicate exits after uncertain placement outcomes. These are source-verified findings, not inferred graph edges.

## Source Nodes

- OrderManager
- RiskManager
- Position
- Signal