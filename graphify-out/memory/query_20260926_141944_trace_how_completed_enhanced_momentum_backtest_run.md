---
type: "query"
date: "2026-09-26T14:19:44.324509+00:00"
question: "Trace how completed Enhanced Momentum backtest runs are saved and refreshed in the frontend history list, including why a new run may appear in the saved-run table without its displayed run number and details updating."
contributor: "graphify"
source_nodes: ["server.py"]
---

# Q: Trace how completed Enhanced Momentum backtest runs are saved and refreshed in the frontend history list, including why a new run may appear in the saved-run table without its displayed run number and details updating.

## Answer

The graph identifies api/server.py as part of the backtest persistence path, but it does not contain the frontend polling and history-refresh flow needed to establish this UI bug. Source inspection found that EnhancedMomentumBacktestPanel clears activeJobId before awaited requests for the completed run and updated history finish. That state change triggers the polling effect cleanup, marks its active flag false, and suppresses both UI updates. The fix clears activeJobId only after those requests complete.

## Source Nodes

- server.py