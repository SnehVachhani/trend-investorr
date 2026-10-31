---
type: "query"
date: "2026-10-26T19:36:12.021519+00:00"
question: "Is the sector position cap a suitable three-hour, five-turn Code Behaviors task, and should five prompts be prewritten?"
contributor: "graphify"
source_nodes: ["Signal", "OrderManager", "Position", "PaperTrader", "RiskManager"]
---

# Q: Is the sector position cap a suitable three-hour, five-turn Code Behaviors task, and should five prompts be prewritten?

## Answer

The cap is not literally one line: Signal carries sector, OrderManager has a default-empty sector parameter, Position lacks sector, PaperTrader creates and updates positions, and RiskManager has a placeholder that never rejects for the sector cap. A complete change would touch multiple files and require direct risk checks plus paper-trade flow checks. But task suitability cannot be guaranteed from file count: an advanced model may finish a well-scoped fix in one or two responses. Project instructions call for a real task expected to take hours and many turns; do not manufacture five prompts or log idle time. Use a substantive real task, review each response, ask follow-ups only for actual missing work or findings, and report actual active time.

## Source Nodes

- Signal
- OrderManager
- Position
- PaperTrader
- RiskManager