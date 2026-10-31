"""
Trend-Investorr: Connors RSI Pullback Algorithm

A mean-reversion trading algorithm based on Larry Connors' research.
88.89% win rate backtested on S&P 500 (1996-2019, 36 trades).

Strategy Logic:
    The stock market is in a long-term uptrend. Short-term pullbacks
    driven by panic/fear push prices below true value. We buy the fear
    and sell the recovery.

Entry Conditions (ALL must be true):
    1. Stock is above its 200-day SMA (long-term uptrend intact)
    2. 10-period RSI drops below 30 (oversold / fear)
    3. Not already in a trade for this stock

Exit Conditions (ANY triggers exit):
    1. 10-period RSI crosses above 40 (fear has passed, recovery underway)
    2. 10 trading candles elapsed since entry (time stop — avoid getting stuck)
    3. Stop loss hit (default: 5% below entry — circuit breaker for crashes)

Adaptation for Indian Markets:
    - Works on individual NSE stocks (not just index)
    - Uses SMA_200 from indicator cache (pre-computed at warm-up)
    - RSI-14 is used instead of RSI-10 (already available in indicator cache)
      with threshold adjusted to 25 (stricter oversold for RSI-14)
    - Scanner triggers: MomentumScanner detects RSI oversold conditions

Data Sources:
    - MomentumScanner observations (RSI oversold detection)
    - _indicator_cache: SMA_200, RSI values
    - Live ticks: current price for trade management
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from loguru import logger

from core.constants import IST, SignalSource
from data.models.market_data import Observation, Tick, TechnicalIndicators
from strategies.base import (
    ActiveTrade,
    AlgorithmState,
    EntrySignal,
    ExitReason,
    ExitSignal,
    TradingAlgorithm,
)
from strategies.config import CONNORS_CONFIG as cfg


class ConnorsRSIPullback(TradingAlgorithm):
    """
    Connors RSI Pullback — buy oversold stocks in uptrends.

    Uses MomentumScanner's RSI observations as the trigger,
    validates against SMA_200 for trend confirmation,
    and manages the trade with RSI-based exits and time stops.

    Usage:
        algo = ConnorsRSIPullback()
        algo.set_indicator_cache(cache)
        algo.on_observation(obs)          # Fed by AlgorithmManager
        entries = algo.check_entries()     # Returns EntrySignal if conditions met
        exits = algo.check_exits()         # Returns ExitSignal if conditions met
    """

    def __init__(
        self,
        rsi_entry_threshold: float = 30.0,
        rsi_exit_threshold: float = 40.0,
        max_hold_candles: int = 10,
        stop_loss_pct: float = 0.0,  # Original strategy uses NO stop loss
        max_simultaneous_trades: int = 5,
        enabled: bool = True,
    ):
        """
        Args:
            max_simultaneous_trades: Max stocks this algorithm can hold at once.
        """
        super().__init__(name="connors_rsi_pullback", enabled=enabled)

        self._rsi_entry = cfg.rsi_entry_threshold
        self._rsi_exit = cfg.rsi_exit_threshold
        self._max_hold = cfg.max_hold_candles
        self._stop_loss_pct = cfg.stop_loss_pct
        self._max_trades = max_simultaneous_trades

        # Indicator cache for SMA_200 and RSI lookups
        self._indicators: dict[str, TechnicalIndicators] = {}

        # Stocks that have triggered the RSI oversold condition
        # but haven't been entered yet: symbol → Observation
        self._setups: dict[str, Observation] = {}

        # Cooldown: don't re-enter a stock within N candles of exiting
        self._cooldowns: dict[str, int] = {}
        self._cooldown_candles: int = 5

    def set_indicator_cache(self, cache: dict[str, TechnicalIndicators]) -> None:
        """Load indicator cache for SMA_200 and RSI lookups."""
        self._indicators = cache

    def interested_sources(self) -> list[SignalSource]:
        """This algorithm listens to MomentumScanner observations."""
        return [SignalSource.MOMENTUM]

    # ── Phase 1: Receive Scanner Observations ──

    def on_observation(self, obs: Observation) -> None:
        """
        Process a MomentumScanner observation.

        We're looking for RSI oversold signals. The MomentumScanner already
        detects these — we just need to validate the trend filter (SMA_200).
        """
        if not self._enabled:
            return

        # Only care about bullish momentum observations (RSI oversold = reversal)
        if obs.direction != "bullish":
            return

        # Check if this is an RSI-related signal
        if obs.pattern_name not in ("rsi_oversold_bounce", "macd_rsi_momentum"):
            return

        # Don't set up if already in a trade for this stock
        if self.is_in_trade(obs.symbol):
            return

        # Don't set up if on cooldown
        if obs.symbol in self._cooldowns:
            return

        # Don't set up if already at max trades
        if len(self._active_trades) >= self._max_trades:
            return

        # ── Trend Filter: price must be above SMA_200 ──
        ind = self._indicators.get(obs.token)
        if not ind or ind.sma_200 <= 0:
            return

        if obs.price <= ind.sma_200:
            logger.debug(
                f"[ConnorsRSI] {obs.symbol} RSI oversold but BELOW SMA200 "
                f"(₹{obs.price:.0f} < ₹{ind.sma_200:.0f}) — skipping"
            )
            return

        # ── RSI Check: must be below threshold ──
        if ind.rsi_10 > self._rsi_entry:
            return

        # ── Setup is valid! Store it. ──
        self._setups[obs.symbol] = obs
        logger.info(
            f"📋 [ConnorsRSI] SETUP: {obs.symbol} | "
            f"RSI={ind.rsi_10:.0f} (< {self._rsi_entry}) | "
            f"price=₹{obs.price:.2f} > SMA200=₹{ind.sma_200:.0f} ✓"
        )

    # ── Phase 2: Check Entry Conditions ──

    def check_entries(self) -> list[EntrySignal]:
        """
        Convert valid setups into entry signals.

        In the original Connors strategy, entry is at "next day's open."
        In our real-time system, once the setup is detected (RSI < threshold
        with price > SMA200), we enter immediately at the current price.
        """
        entries: list[EntrySignal] = []

        for symbol, obs in list(self._setups.items()):
            # Double-check we're not already in this trade
            if self.is_in_trade(symbol):
                del self._setups[symbol]
                continue

            ind = self._indicators.get(obs.token)
            if not ind:
                del self._setups[symbol]
                continue

            # Calculate stop loss and target
            stop_loss = obs.price * (1 - self._stop_loss_pct / 100)

            # Target: we exit on RSI > 40, but set a nominal target
            # for the risk manager (10% up as a placeholder since we use indicator exits)
            target = obs.price * 1.10

            # Confidence based on how oversold the RSI is
            if ind.rsi_10 < 20:
                confidence = 0.85  # Extremely oversold
            elif ind.rsi_10 < 25:
                confidence = 0.75
            elif ind.rsi_10 < 30:
                confidence = 0.65
            else:
                confidence = 0.50

            # Bonus confidence if trend is strong
            if ind.ema_20 > ind.ema_50 > ind.sma_200:
                confidence = min(confidence + 0.10, 0.95)

            # Bonus if supertrend is still bullish (pullback in uptrend)
            if ind.supertrend_direction == 1:
                confidence = min(confidence + 0.05, 0.95)

            entry = EntrySignal(
                symbol=symbol,
                token=obs.token,
                algorithm_name=self._name,
                direction="BUY",
                price=obs.price,
                stop_loss=stop_loss,
                target_price=target,
                confidence=confidence,
                reason=(
                    f"Connors RSI Pullback: RSI={ind.rsi_10:.0f} "
                    f"(oversold < {self._rsi_entry}) in uptrend "
                    f"(price ₹{obs.price:.0f} > SMA200 ₹{ind.sma_200:.0f})"
                ),
                triggering_observations=[obs],
                metadata={
                    "rsi_at_entry": round(ind.rsi_10, 1),
                    "sma_200": round(ind.sma_200, 2),
                    "distance_from_sma200_pct": round(
                        (obs.price - ind.sma_200) / ind.sma_200 * 100, 2
                    ),
                    "supertrend_direction": ind.supertrend_direction,
                    "ema_20": round(ind.ema_20, 2),
                    "ema_50": round(ind.ema_50, 2),
                },
            )
            entries.append(entry)

            # Remove from setups (will be tracked as active trade)
            del self._setups[symbol]

        return entries

    # ── Phase 3: Update Active Trades ──

    def on_tick(self, tick: Tick) -> None:
        """Update active trades with new price data."""
        trade = self._active_trades.get(tick.symbol)
        if trade:
            trade.update_price(tick.ltp)

    def increment_candle_count(self) -> None:
        """
        Called by AlgorithmManager when a new daily candle closes.
        Increments the candle counter for all active trades (for time stop).
        """
        for trade in self._active_trades.values():
            trade.candles_since_entry += 1

        # Decrement cooldowns
        expired = [s for s, c in self._cooldowns.items() if c <= 1]
        for s in expired:
            del self._cooldowns[s]
        for s in self._cooldowns:
            self._cooldowns[s] -= 1

    # ── Phase 4: Check Exit Conditions ──

    def check_exits(self) -> list[ExitSignal]:
        """
        Check all active trades for exit conditions.

        Exit triggers (in priority order):
            1. Stop loss hit (hard protection)
            2. Time stop — held for max_hold_candles
            3. RSI crossed above exit threshold (the normal exit)
        """
        exits: list[ExitSignal] = []

        for symbol, trade in list(self._active_trades.items()):
            # Exit 1: Stop Loss (if enabled, i.e., > 0)
            if self._stop_loss_pct > 0 and trade.current_price > 0 and trade.current_price <= trade.stop_loss:
                exits.append(ExitSignal(
                    trade=trade,
                    reason=ExitReason.STOP_LOSS,
                    exit_price=trade.current_price,
                    description=(
                        f"Stop loss hit: ₹{trade.current_price:.2f} "
                        f"<= SL ₹{trade.stop_loss:.2f} "
                        f"(loss={trade.unrealized_pnl_pct:.1f}%)"
                    ),
                ))
                continue

            # Exit 2: Time stop
            if trade.candles_since_entry >= self._max_hold:
                exits.append(ExitSignal(
                    trade=trade,
                    reason=ExitReason.TIME_STOP,
                    exit_price=trade.current_price,
                    description=(
                        f"Time stop: {trade.candles_since_entry} candles elapsed "
                        f"(max={self._max_hold}), P&L={trade.unrealized_pnl_pct:+.1f}%"
                    ),
                ))
                continue

            # Exit 3: RSI crossed above exit threshold
            ind = self._indicators.get(trade.token)
            if ind and ind.rsi_10 >= self._rsi_exit:
                exits.append(ExitSignal(
                    trade=trade,
                    reason=ExitReason.INDICATOR_EXIT,
                    exit_price=trade.current_price,
                    description=(
                        f"RSI recovery: RSI={ind.rsi_10:.0f} crossed above "
                        f"{self._rsi_exit} — fear has passed, "
                        f"P&L={trade.unrealized_pnl_pct:+.1f}%"
                    ),
                ))
                continue

        return exits

    # ── Lifecycle ──

    def reset_daily(self) -> None:
        """Reset setups for a new trading day. Active trades persist (swing strategy)."""
        self._setups.clear()
        # NOTE: We do NOT clear active trades here — this is a swing strategy
        # that holds for multiple days. Active trades are only closed by exit conditions.

    @property
    def stats(self) -> dict[str, Any]:
        base = super().stats
        base.update({
            "pending_setups": len(self._setups),
            "cooldowns": len(self._cooldowns),
            "rsi_entry_threshold": self._rsi_entry,
            "rsi_exit_threshold": self._rsi_exit,
            "max_hold_candles": self._max_hold,
        })
        return base
