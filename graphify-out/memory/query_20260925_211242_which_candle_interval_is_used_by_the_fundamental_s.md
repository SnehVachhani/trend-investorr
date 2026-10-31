---
type: "query"
date: "2026-09-25T21:12:42.324563+00:00"
question: "Which candle interval is used by the fundamental setup backtest for trading entries and exits?"
contributor: "graphify"
source_nodes: ["server.py", "Candle"]
---

# Q: Which candle interval is used by the fundamental setup backtest for trading entries and exits?

## Answer

The fundamental setup backtest uses daily (1D) candles from candles_1d. Signals are evaluated on completed daily bars; entries fill at the following session open, and the setup simulator evaluates stops and targets using daily OHLCV bars.

## Source Nodes

- server.py
- Candle