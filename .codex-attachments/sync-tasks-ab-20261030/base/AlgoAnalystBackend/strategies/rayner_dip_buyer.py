"""
Trend-Investorr: Rayner Teo 10-Day Low Dip Buyer Algorithm

Based on: "SIMPLE Trading System: +1067% (Backed By Data)" by Rayner Teo
https://www.youtube.com/watch?v=UFJqat3y6T8

Strategy Logic (65% win rate, +1067% over 21 years on S&P 500):
    Strong trending stocks pull back temporarily. We buy the dip
    at a discount and sell when the short-term trend recovers.

Entry Conditions (sequential):
    1. Stock is above its 200-day SMA (long-term uptrend intact)
    2. Stock makes a 10-day low (price pulled back)
    3. Next day: place a 2% buy limit order below previous close
    4. If price trades at/below the limit → FILL (enter the trade)

Exit Conditions:
    1. Stock closes above the 5-day SMA → exit at next day's open
    2. Stop loss: 5% below entry (circuit breaker, not in original but prudent)
    3. Time stop: 20 candles max hold (avoid dead money)

Position Management:
    - Max 10 simultaneous positions (10% capital each)
    - If more setups than slots, rank by 100-day Rate of Change
      (strongest momentum stocks get priority)

Adaptation for Indian Markets:
    - Operates on NSE stocks (watchlist-driven)
    - Uses indicator cache for SMA_200 (pre-computed at warm-up)
    - Computes SMA_5, 10-day low, 100-day ROC from daily candle cache
    - CNC/delivery product type (multi-day swing holds)
    - Tick-level limit fill simulation (no real broker limit order)

Data Sources:
    - _indicators: SMA_200 from warm-up indicator cache
    - _candle_cache: Last 200 daily candles per stock (for SMA_5, 10-day low, ROC)
    - Live ticks: current price for limit fill detection and trade management
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import numpy as np
from loguru import logger

from core.constants import IST, SignalSource
from data.models.market_data import Candle, Observation, Tick, TechnicalIndicators
from strategies.base import (
    ActiveTrade,
    AlgorithmState,
    EntrySignal,
    ExitReason,
    ExitSignal,
    TradingAlgorithm,
)


# ── Internal Data Models ─────────────────────────────────────


@dataclass
class PendingSetup:
    """
    A 10-day low has been detected. We're waiting for price to
    drop to our 2% limit level the next day.

    Lifecycle:
        10-day low detected → PendingSetup created
        → price hits limit → converted to EntrySignal
        → day ends without fill → expired
    """
    symbol: str
    token: str
    trigger_date: date              # When the 10-day low was detected
    prev_close: float               # Closing price on the trigger day
    limit_price: float              # prev_close × 0.98 (2% below)
    sma_200: float                  # SMA_200 at time of setup
    momentum_roc: float             # 100-day ROC for ranking
    ten_day_low: float              # The actual 10-day low price
    created_at: datetime = field(default_factory=lambda: datetime.now(IST))
    expired: bool = False


# ── Algorithm ────────────────────────────────────────────────


class RaynerDipBuyer(TradingAlgorithm):
    """
    Rayner Teo's 10-Day Low Dip Buyer — buy oversold pullbacks
    in strong uptrends, sell when the short-term trend recovers.

    Usage:
        algo = RaynerDipBuyer()
        algo.set_indicator_cache(indicator_cache)
        algo.set_candle_cache(candle_cache)

        # During market hours:
        algo.on_tick(tick)                  # Check pending limit fills
        entries = algo.check_entries()      # Emit filled setups as entries
        exits = algo.check_exits()          # Check SMA_5 exit + stops

        # At EOD:
        algo.on_daily_close(new_candles)    # Update candle cache, detect new setups
    """

    # Minimum candles needed for each computation
    MIN_CANDLES_SMA5 = 5
    MIN_CANDLES_10DAY_LOW = 10
    MIN_CANDLES_ROC100 = 100

    def __init__(
        self,
        limit_discount_pct: float = 2.0,
        sma_exit_period: int = 5,
        lookback_low_period: int = 10,
        momentum_lookback: int = 100,
        max_simultaneous_trades: int = 10,
        stop_loss_pct: float = 5.0,
        max_hold_candles: int = 20,
        enabled: bool = True,
    ):
        """
        Args:
            limit_discount_pct: Buy limit = prev_close × (1 - this/100). Default 2%.
            sma_exit_period: Exit when close > SMA of this period. Default 5.
            lookback_low_period: Number of days for the low detection. Default 10.
            momentum_lookback: Days for ROC ranking. Default 100.
            max_simultaneous_trades: Max positions held at once. Default 10.
            stop_loss_pct: Hard stop loss % below entry. Default 5%.
            max_hold_candles: Time stop — exit after N trading days. Default 20.
        """
        super().__init__(name="rayner_dip_buyer", enabled=enabled)

        self._limit_discount = limit_discount_pct / 100.0
        self._sma_exit_period = sma_exit_period
        self._low_period = lookback_low_period
        self._momentum_lookback = momentum_lookback
        self._max_trades = max_simultaneous_trades
        self._stop_loss_pct = stop_loss_pct
        self._max_hold = max_hold_candles

        # Indicator cache for SMA_200 lookups
        self._indicators: dict[str, TechnicalIndicators] = {}

        # Daily candle cache: token → list[Candle] (chronological, oldest first)
        self._candle_cache: dict[str, list[Candle]] = {}

        # Pending setups waiting for limit fill: token → PendingSetup
        self._pending_setups: dict[str, PendingSetup] = {}

        # Setups that got filled today, ready to emit as EntrySignals
        self._filled_setups: dict[str, PendingSetup] = {}

        # Trades marked for exit at next open (SMA_5 crossover detected at EOD)
        self._pending_exits: set[str] = set()

        # Cooldown: don't re-enter a stock within N candles of exiting
        self._cooldowns: dict[str, int] = {}
        self._cooldown_candles: int = 3

        # Stats
        self._setups_detected: int = 0
        self._limit_fills: int = 0
        self._setups_expired: int = 0

    # ── Cache Loading ────────────────────────────────────

    def set_indicator_cache(self, cache: dict[str, TechnicalIndicators]) -> None:
        """Load indicator cache for SMA_200 lookups."""
        self._indicators = cache

    def set_candle_cache(self, cache: dict[str, list[Candle]]) -> None:
        """
        Load daily candle history for internal computations.

        Args:
            cache: token → list of daily Candle objects, chronological order.
                   Needs at least 200 candles per stock for full functionality.
        """
        self._candle_cache = cache
        logger.info(
            f"[RaynerDip] Candle cache loaded: {len(cache)} stocks | "
            f"avg candles={sum(len(c) for c in cache.values()) / max(len(cache), 1):.0f}"
        )

    def interested_sources(self) -> list[SignalSource]:
        """
        This algorithm is self-scanning from candle data.
        It listens to MOMENTUM for supplementary RSI context but
        primarily drives itself from daily candle analysis.
        """
        return [SignalSource.MOMENTUM]

    # ── Phase 1: Scanner Observations (supplementary) ────

    def on_observation(self, obs: Observation) -> None:
        """
        Process scanner observations. This algo is mostly self-driven
        from candle data, but we note momentum observations for
        confidence boosting.
        """
        # This algorithm is candle-driven, not observation-driven.
        # We could use momentum signals to boost confidence, but
        # the core logic doesn't depend on them.
        pass

    # ── Phase 2: Tick Processing (limit fill detection) ──

    def on_tick(self, tick: Tick) -> None:
        """
        Two jobs on every tick:
        1. Update active trades with new price
        2. Check if any pending limit setups get filled
        """
        # Update active trade prices
        trade = self._active_trades.get(tick.symbol)
        if trade:
            trade.update_price(tick.ltp)

        # Check pending limit fills
        setup = self._pending_setups.get(tick.token)
        if setup and not setup.expired:
            if tick.ltp <= setup.limit_price:
                # FILLED! Price traded at or below our limit
                self._filled_setups[tick.token] = setup
                del self._pending_setups[tick.token]
                self._limit_fills += 1
                logger.info(
                    f"💰 [RaynerDip] LIMIT FILL: {setup.symbol} | "
                    f"limit=₹{setup.limit_price:.2f} | ltp=₹{tick.ltp:.2f} | "
                    f"discount={self._limit_discount * 100:.0f}% below prev close ₹{setup.prev_close:.2f}"
                )

    # ── Phase 3: Check Entries ───────────────────────────

    def check_entries(self) -> list[EntrySignal]:
        """
        Convert filled limit setups into entry signals.

        Ranking: if more filled setups than available slots,
        sort by 100-day momentum (highest ROC first).
        """
        if not self._filled_setups:
            return []

        entries: list[EntrySignal] = []
        available_slots = self._max_trades - len(self._active_trades)

        if available_slots <= 0:
            self._filled_setups.clear()
            return []

        # Rank by momentum (100-day ROC) — strongest momentum first
        ranked = sorted(
            self._filled_setups.values(),
            key=lambda s: s.momentum_roc,
            reverse=True,
        )

        for setup in ranked[:available_slots]:
            # Skip if already in trade or on cooldown
            if self.is_in_trade(setup.symbol):
                continue
            if setup.symbol in self._cooldowns:
                continue

            stop_loss = setup.limit_price * (1 - self._stop_loss_pct / 100)
            risk = setup.limit_price - stop_loss
            target = setup.limit_price + (risk * 2)  # 2:1 reward:risk as reference

            # Confidence based on how strong the uptrend is
            confidence = self._compute_confidence(setup)

            entry = EntrySignal(
                symbol=setup.symbol,
                token=setup.token,
                algorithm_name=self._name,
                direction="BUY",
                price=setup.limit_price,
                stop_loss=stop_loss,
                target_price=target,
                confidence=confidence,
                reason=(
                    f"Rayner Dip Buy: 10-day low detected, limit filled at "
                    f"₹{setup.limit_price:.2f} (2% below prev close ₹{setup.prev_close:.2f}), "
                    f"price > SMA200 ₹{setup.sma_200:.0f}, "
                    f"100d momentum={setup.momentum_roc:+.1f}%"
                ),
                triggering_observations=[],
                metadata={
                    "strategy": "rayner_dip_buyer",
                    "limit_price": round(setup.limit_price, 2),
                    "prev_close": round(setup.prev_close, 2),
                    "sma_200": round(setup.sma_200, 2),
                    "ten_day_low": round(setup.ten_day_low, 2),
                    "momentum_roc_100d": round(setup.momentum_roc, 2),
                    "trigger_date": str(setup.trigger_date),
                },
            )
            entries.append(entry)

        self._filled_setups.clear()
        return entries

    # ── Phase 4: Check Exits ─────────────────────────────

    def check_exits(self) -> list[ExitSignal]:
        """
        Check all active trades for exit conditions.

        Exit triggers (priority order):
            1. Stop loss (hard protection)
            2. Time stop (max hold candles)
            3. Pending exit flag (SMA_5 crossover detected at EOD → exit at open)
        """
        exits: list[ExitSignal] = []

        for symbol, trade in list(self._active_trades.items()):
            if trade.current_price <= 0:
                continue

            # Exit 1: Stop Loss
            if trade.current_price <= trade.stop_loss:
                exits.append(ExitSignal(
                    trade=trade,
                    reason=ExitReason.STOP_LOSS,
                    exit_price=trade.current_price,
                    description=(
                        f"Rayner SL: ₹{trade.current_price:.2f} "
                        f"<= SL ₹{trade.stop_loss:.2f} "
                        f"(loss={trade.unrealized_pnl_pct:.1f}%)"
                    ),
                ))
                continue

            # Exit 2: Time Stop
            if trade.candles_since_entry >= self._max_hold:
                exits.append(ExitSignal(
                    trade=trade,
                    reason=ExitReason.TIME_STOP,
                    exit_price=trade.current_price,
                    description=(
                        f"Rayner time stop: {trade.candles_since_entry} days "
                        f"(max={self._max_hold}), P&L={trade.unrealized_pnl_pct:+.1f}%"
                    ),
                ))
                continue

            # Exit 3: SMA_5 crossover (flagged by on_daily_close)
            if symbol in self._pending_exits:
                exits.append(ExitSignal(
                    trade=trade,
                    reason=ExitReason.INDICATOR_EXIT,
                    exit_price=trade.current_price,
                    description=(
                        f"Rayner SMA5 exit: close crossed above 5-day SMA, "
                        f"exiting at open. P&L={trade.unrealized_pnl_pct:+.1f}%"
                    ),
                ))
                self._pending_exits.discard(symbol)
                continue

        return exits

    # ── EOD Processing ───────────────────────────────────

    def on_daily_close(self, updated_candles: dict[str, list[Candle]] | None = None) -> None:
        """
        Called at end of day (after market close, ~3:45 PM).

        Responsibilities:
            1. Update candle cache with today's new candle
            2. Expire unfilled pending setups from today
            3. Scan all stocks for NEW 10-day low setups (for tomorrow)
            4. Check SMA_5 exit condition for active trades
            5. Increment candle counts for active trades
        """
        # 1. Update candle cache if new data provided
        if updated_candles:
            for token, candles in updated_candles.items():
                self._candle_cache[token] = candles

        # 2. Expire unfilled pending setups
        expired_count = len(self._pending_setups)
        self._setups_expired += expired_count
        if expired_count > 0:
            symbols = [s.symbol for s in self._pending_setups.values()]
            logger.info(
                f"[RaynerDip] Expired {expired_count} unfilled setups: "
                f"{', '.join(symbols[:5])}{'...' if len(symbols) > 5 else ''}"
            )
        self._pending_setups.clear()
        self._filled_setups.clear()

        # 3. Scan for new 10-day low setups
        self._scan_for_setups()

        # 4. Check SMA_5 exit for active trades
        self._check_sma5_exits()

        # 5. Increment candle counts and manage cooldowns
        self._increment_candle_counts()

    def _scan_for_setups(self) -> None:
        """
        Scan all stocks in candle cache for 10-day low setups.

        Conditions:
            1. Price > SMA_200 (uptrend)
            2. Today's low is the lowest in the last 10 days
            3. Not already in a trade for this stock
            4. Not on cooldown
        """
        new_setups = 0

        for token, candles in self._candle_cache.items():
            if len(candles) < self._low_period:
                continue

            # Get indicator for SMA_200 check
            ind = self._indicators.get(token)
            if not ind or ind.sma_200 <= 0:
                continue

            latest = candles[-1]
            symbol = latest.symbol

            # Skip if already in trade or on cooldown
            if self.is_in_trade(symbol):
                continue
            if symbol in self._cooldowns:
                continue

            # Condition 1: Price above SMA_200
            if latest.close <= ind.sma_200:
                continue

            # Condition 2: Today's low is the 10-day low
            recent_lows = [c.low for c in candles[-self._low_period:]]
            ten_day_low = min(recent_lows)

            if latest.low > ten_day_low:
                # Today's low is NOT the 10-day low — skip
                continue

            # 10-day low detected! Calculate limit price for tomorrow
            limit_price = latest.close * (1 - self._limit_discount)

            # Compute 100-day ROC for momentum ranking
            momentum_roc = self._compute_roc(candles, self._momentum_lookback)

            setup = PendingSetup(
                symbol=symbol,
                token=token,
                trigger_date=latest.timestamp.date() if hasattr(latest.timestamp, 'date') else date.today(),
                prev_close=latest.close,
                limit_price=round(limit_price, 2),
                sma_200=ind.sma_200,
                momentum_roc=momentum_roc,
                ten_day_low=ten_day_low,
            )
            self._pending_setups[token] = setup
            self._setups_detected += 1
            new_setups += 1

            logger.info(
                f"📋 [RaynerDip] SETUP: {symbol} | "
                f"10-day low=₹{ten_day_low:.2f} | "
                f"limit=₹{limit_price:.2f} (2% below close ₹{latest.close:.2f}) | "
                f"SMA200=₹{ind.sma_200:.0f} | ROC100={momentum_roc:+.1f}%"
            )

        if new_setups > 0:
            logger.info(f"[RaynerDip] {new_setups} new setups detected for tomorrow")

    def _check_sma5_exits(self) -> None:
        """
        Check if any active trades should exit because the stock
        closed above its 5-day SMA.

        Per the original system: exit at next day's OPEN.
        We flag the trade here; the actual exit happens on the next tick.
        """
        for symbol, trade in self._active_trades.items():
            candles = self._candle_cache.get(trade.token)
            if not candles or len(candles) < self._sma_exit_period:
                continue

            # Compute SMA_5 from the last 5 closing prices
            recent_closes = [c.close for c in candles[-self._sma_exit_period:]]
            sma_5 = sum(recent_closes) / len(recent_closes)

            latest_close = candles[-1].close

            if latest_close > sma_5:
                self._pending_exits.add(symbol)
                logger.info(
                    f"📤 [RaynerDip] EXIT SIGNAL: {symbol} | "
                    f"close=₹{latest_close:.2f} > SMA5=₹{sma_5:.2f} | "
                    f"will exit at tomorrow's open | "
                    f"P&L={trade.unrealized_pnl_pct:+.1f}%"
                )

    # ── Helpers ──────────────────────────────────────────

    @staticmethod
    def _compute_roc(candles: list[Candle], period: int) -> float:
        """
        Compute Rate of Change over N days.
        ROC = (current_close - close_N_days_ago) / close_N_days_ago × 100
        """
        if len(candles) < period + 1:
            # Not enough data — use whatever we have
            if len(candles) < 2:
                return 0.0
            old_close = candles[0].close
        else:
            old_close = candles[-(period + 1)].close

        if old_close <= 0:
            return 0.0

        current_close = candles[-1].close
        return ((current_close - old_close) / old_close) * 100

    def _compute_confidence(self, setup: PendingSetup) -> float:
        """
        Compute entry confidence based on trend strength and momentum.

        Factors:
            - Distance above SMA_200 (stronger uptrend = higher confidence)
            - 100-day momentum (stronger = higher)
            - RSI (more oversold = higher)
        """
        confidence = 0.60  # Base confidence for a valid setup

        # Bonus for strong uptrend (price well above SMA_200)
        ind = self._indicators.get(setup.token)
        if ind:
            distance_pct = (setup.prev_close - setup.sma_200) / setup.sma_200 * 100
            if distance_pct > 20:
                confidence += 0.10
            elif distance_pct > 10:
                confidence += 0.05

            # Bonus for deeply oversold RSI
            if ind.rsi_14 < 25:
                confidence += 0.10
            elif ind.rsi_14 < 35:
                confidence += 0.05

            # Bonus for strong momentum
            if setup.momentum_roc > 30:
                confidence += 0.10
            elif setup.momentum_roc > 15:
                confidence += 0.05

            # Bonus for bullish EMA stack
            if ind.ema_20 > ind.ema_50 > ind.sma_200:
                confidence += 0.05

        return min(confidence, 0.95)

    def _increment_candle_counts(self) -> None:
        """Increment candle counters for active trades and manage cooldowns."""
        for trade in self._active_trades.values():
            trade.candles_since_entry += 1

        # Decrement cooldowns
        expired = [s for s, c in self._cooldowns.items() if c <= 1]
        for s in expired:
            del self._cooldowns[s]
        for s in self._cooldowns:
            self._cooldowns[s] -= 1

    # ── Lifecycle ────────────────────────────────────────

    def close_trade(self, symbol: str, reason: ExitReason) -> ActiveTrade | None:
        """Override to add cooldown on exit."""
        trade = super().close_trade(symbol, reason)
        if trade:
            self._cooldowns[symbol] = self._cooldown_candles
            self._pending_exits.discard(symbol)
        return trade

    def reset_daily(self) -> None:
        """
        Reset for a new trading day.

        Clears filled setups (pending setups survive — they're for today's fills).
        Active trades persist (swing strategy).
        """
        self._filled_setups.clear()
        # NOTE: _pending_setups are NOT cleared — they were set at yesterday's
        # close and are active for today's market session.
        # NOTE: _active_trades persist — this is a swing/positional strategy.
        logger.debug(
            f"[RaynerDip] Daily reset | "
            f"pending_setups={len(self._pending_setups)} | "
            f"active_trades={len(self._active_trades)} | "
            f"pending_exits={len(self._pending_exits)}"
        )

    @property
    def stats(self) -> dict[str, Any]:
        base = super().stats
        base.update({
            "pending_setups": len(self._pending_setups),
            "filled_setups": len(self._filled_setups),
            "pending_exits": len(self._pending_exits),
            "cooldowns": len(self._cooldowns),
            "candle_cache_stocks": len(self._candle_cache),
            "setups_detected_total": self._setups_detected,
            "limit_fills_total": self._limit_fills,
            "setups_expired_total": self._setups_expired,
            "limit_discount_pct": self._limit_discount * 100,
            "sma_exit_period": self._sma_exit_period,
            "low_lookback": self._low_period,
            "max_hold_candles": self._max_hold,
        })
        return base
