# Fundamental query backtests

The **Backtest → Technical setup backtest** screen pairs a versioned
stock-selection query with candle-based technical presets. The query uses
reported financial metrics and runs once at the preview date; it is not
recalculated at historical dates or evaluated inside the technical setup
engine. All selected rules must pass. Missing values exclude a stock and are
counted separately from values that fail a threshold. A saved run keeps the
exact query version and selected stock list, plus its target, holding-period,
and stop settings.

## Fundamental universe

The available fields are annual net-profit growth (1 year and 5-year CAGR),
quarterly net-profit growth (quarter-on-quarter and year-on-year), ROE, ROCE,
latest positive annual and quarterly net profit, and a minimum estimated market
capitalisation. Growth is calculated only between matching reported periods
with positive starting and latest profit. Annual reports older than 18 months,
quarterly reports older than 6 months, and ratio snapshots older than 6 months
are treated as missing.

The stock master has no populated company market-cap values. The displayed
market-cap field is therefore an **estimate** of as-of P/E multiplied by the
latest known annual net profit, in crore rupees. It should be used as a rough
size screen, not an audited market capitalisation.

The query preview selects the stock list once, on the date shown in the
preview. The backtest uses that exact list for the full requested price range;
technical setups read daily candles and do not re-run the stock query at each
historical rebalance. The selected list is saved with the run for review.

When the selection date is later than the backtest start, this is a
retrospective technical comparison on the query's selected stocks. The current
active stock master also lacks complete delisted-company coverage, so historical
comparisons can retain selection and survivorship bias.

## Technical setups and execution

The setup selector offers these system-authored presets. The query remains
separate from the technical setups, and the UI only selects presets; it does
not ask the user to author technical rules.

1. **Consolidation, sweep and reclaim:** a completed 20-session narrow base,
   a modest close below its low, then a bullish reclaim of the frozen estimated
   high-volume price level within eight sessions.
2. **Consolidation breakout:** a completed 20-session narrow base followed by a
   close at least 0.5% above its high on at least 1.2 times its average volume.
3. **Uptrend pullback reclaim:** a rising 50-session trend, a dip below its
   20-session moving average, then a bullish reclaim within eight sessions.
4. **Volatility contraction breakout (VCP-style):** three contracting
   five-session ranges, a 15-session base no wider than 15%, and a close at
   least 0.5% above the base on 1.5 times the prior 20-session average volume.
5. **High-volume gap continuation:** a 2–8% gap up, a bullish close in the
   upper quarter of its range, 1.8 times average volume, and price above a
   rising 50-session average.
6. **52-week high breakout:** a close above the prior 252-session high on 1.5
   times average volume, with the 50-session average above a rising
   200-session average.
7. **Donchian breakout and retest:** a 20-session high breaks on 1.3 times
   average volume in a rising 50-session trend, then price retests the former
   high and closes back above it within eight sessions.
8. **RSI-2 oversold rebound in an uptrend:** Wilder RSI(2) crosses back above
   10 after an oversold reading, while the close is above a rising 200-session
   average and the 50-session average is above the 200-session average.
9. **Inside-bar breakout in a rising trend:** after an inside day, a bullish
   close clears the prior mother candle's high on 1.2 times average volume,
   with the 20-session average above a rising 50-session average.
10. **Strong trend continuation:** the Watchlist Analysis preset with its same
    daily 200-session trend, 20/50 EMA alignment, daily ADX, and 5-minute
    relative-volume trigger.
11. **Liquid swing momentum:** the Watchlist Analysis preset with its same
    30-session traded-value and 20-session trend gates, 5-minute RSI and
    relative-volume checks.

Signals are formed at a completed daily close. Entries are simulated at the
following session's open. Stops use the observed sweep, base, or pullback
reference; the target is a configurable multiple of entry risk. A configurable
session limit exits remaining positions. A stop wins when stop and target both
trade in one daily candle, and a gap through a stop fills at the gap open.
Configured delivery charges and 0.05% adverse slippage are included. Planned
entries with more than 15% risk to the stop are skipped.

The interface compares all presets by default or can run one selected preset.
Qualifying-stock previews are paginated at 25 rows per page; the query and its
conditions are unchanged by setup selection.

Strong trend continuation and Liquid swing momentum reuse the same filter
expressions, 5-minute execution clock, exit defaults, and backtest engine as
Watchlist Analysis. They are additive here; their Watchlist Analysis presets
remain available there.

Daily-pattern setups estimate the high-volume price level from OHLCV by
distributing each daily candle's volume across its high-low range. Exact volume
traded at each price would require intraday trade or market-profile data.

Every selected stock receives a fixed ₹1,00,000 starting wallet. A run with
106 selected stocks therefore starts with ₹1,06,00,000 total. Each stock's
wallet compounds independently: the next position is sized from that stock's
cash after prior profits, losses, and configured charges. Cash is not shared
between stocks.

Portfolio period return is the change from total starting capital to the sum
of the ending stock wallets over the selected dates. If a stock/setup run could
not be completed, its ₹1,00,000 allocation is treated as idle cash in this
aggregate. Mean tested-stock return is separately calculated from completed
stock runs. Neither figure is annualized. Average trade return is the
arithmetic mean of all closed trades for that setup. These historical results
do not guarantee future returns or executable portfolio performance.
