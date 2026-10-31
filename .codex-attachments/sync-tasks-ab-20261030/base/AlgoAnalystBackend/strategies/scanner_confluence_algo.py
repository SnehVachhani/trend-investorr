"""
Trend-Investorr: Scanner Confluence Algorithm

A trading algorithm that enters trades when multiple scanners agree
on the same stock in the same direction.

This was previously the StrategyEngine — now properly structured
as a TradingAlgorithm inside the AlgorithmManager.

Strategy Logic:
    - Collect observations from ALL scanners
    - Group by stock, score weighted confluence
    - If 2+ scanners agree with combined score >= 0.35 → ENTRY
    - No specific exit logic — relies on intraday square-off
      (future: add trailing stop or time-based exit)

When it's useful:
    Catches multi-signal setups that no single-strategy algorithm
    covers. E.g., Volume spike + Breakout + Momentum all aligning
    on the same stock at the same time.
"""

from __future__ import annotations

from collections import defaultdict
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


class ScannerConfluenceAlgo(TradingAlgorithm):
    """
    Enters trades when multiple scanners agree on the same stock.

    This is a "meta-algorithm" — it doesn't have its own market logic.
    Instead, it trusts the scanners: if enough independent scanners
    detect something on the same stock, that's a high-probability setup.

    Usage:
        algo = ScannerConfluenceAlgo()
        algo.on_observation(obs)         # Fed by AlgorithmManager
        entries = algo.check_entries()    # Returns entries when confluence met
    """

    # How much each scanner's vote counts
    DEFAULT_WEIGHTS: dict[SignalSource, float] = {
        SignalSource.VOLUME_SPIKE: 0.25,
        SignalSource.BREAKOUT: 0.20,
        SignalSource.LIQUIDITY_SWEEP: 0.18,
        SignalSource.MOMENTUM: 0.15,
        SignalSource.ORB: 0.15,
        SignalSource.ORDER_FLOW: 0.10,
        SignalSource.VWAP: 0.08,
        SignalSource.MTF_CONFLUENCE: 0.08,
        SignalSource.GAP: 0.05,
        SignalSource.RELATIVE_STRENGTH: 0.05,
        SignalSource.DELIVERY: 0.05,
        SignalSource.PATTERN: 0.03,
        SignalSource.SECTOR_ROTATION: 0.03,
    }

    def __init__(
        self,
        min_confluence: float = 0.35,
        min_scanners: int = 2,
        max_entries_per_cycle: int = 5,
        observation_window_seconds: int = 30,
        cooldown_minutes: int = 30,
        scanner_weights: dict[SignalSource, float] | None = None,
        enabled: bool = True,
    ):
        """
        Args:
            min_confluence: Minimum weighted score to trigger trade.
            min_scanners: Minimum unique scanners that must agree.
            max_entries_per_cycle: Max entries per evaluation cycle.
            observation_window_seconds: How long observations stay valid.
            cooldown_minutes: Don't re-enter same stock within N minutes.
            scanner_weights: Custom weights (overrides defaults).
        """
        super().__init__(name="scanner_confluence", enabled=enabled)

        self._min_confluence = min_confluence
        self._min_scanners = min_scanners
        self._max_entries = max_entries_per_cycle
        self._window_seconds = observation_window_seconds
        self._cooldown_minutes = cooldown_minutes
        self._weights = scanner_weights or self.DEFAULT_WEIGHTS.copy()

        # Per-stock observation buffer: token → list[Observation]
        self._observations: dict[str, list[Observation]] = defaultdict(list)

        # Overnight bias: token → list[Observation]
        # Loaded from scanner_signals at warm-up. Persists all day (never expires).
        # Merged into confluence scoring so overnight + 1 live scanner = trade.
        self._overnight_bias: dict[str, list[Observation]] = defaultdict(list)

        # Sources that come from post-market batch scanners
        self._OVERNIGHT_SOURCES = {
            SignalSource.DELIVERY,
            SignalSource.PATTERN,
            SignalSource.MTF_CONFLUENCE,
        }

        # Cooldown per stock: token → last_entry_time
        self._cooldowns: dict[str, datetime] = {}

        # Stats
        self._total_observations: int = 0
        self._total_evaluations: int = 0

    def interested_sources(self) -> list[SignalSource]:
        """Listens to ALL scanner sources."""
        return list(SignalSource)

    # ── Phase 1: Collect observations ──

    def on_observation(self, obs: Observation) -> None:
        """Store observation. Routes overnight sources to persistent bias."""
        if not self._enabled:
            return
        self._total_observations += 1

        # Overnight batch signals go to persistent bias (no expiry)
        if obs.source in self._OVERNIGHT_SOURCES:
            # Avoid duplicates by source per token
            existing_sources = {o.source for o in self._overnight_bias[obs.token]}
            if obs.source not in existing_sources:
                self._overnight_bias[obs.token].append(obs)
        else:
            self._observations[obs.token].append(obs)

    # ── Phase 2: No tick-level trade management ──

    def on_tick(self, tick: Tick) -> None:
        """Update active trades with price. Confluence algo is entry-only for now."""
        trade = self._active_trades.get(tick.symbol)
        if trade:
            trade.update_price(tick.ltp)

    # ── Phase 3: Check entries (confluence scoring) ──

    def check_entries(self) -> list[EntrySignal]:
        """
        Score confluence across all stocks with buffered observations.

        For each stock:
            1. Expire stale observations (older than window_seconds)
            2. Check cooldown
            3. Count unique scanner sources
            4. Calculate directional weighted score
            5. If bullish score >= threshold AND scanners >= min → ENTRY
        """
        self._total_evaluations += 1
        now = datetime.now(IST)
        entries: list[EntrySignal] = []

        # Collect all tokens that have ANY signal (live or overnight)
        all_tokens = set(self._observations.keys()) | set(self._overnight_bias.keys())

        for token in all_tokens:
            live_observations = self._observations.get(token, [])
            overnight_observations = self._overnight_bias.get(token, [])

            # 1. Expire stale LIVE observations (overnight never expires)
            fresh_live = [
                obs for obs in live_observations
                if (now - obs.timestamp).total_seconds() <= self._window_seconds
            ]
            if token in self._observations:
                if fresh_live:
                    self._observations[token] = fresh_live
                else:
                    del self._observations[token]

            # Combine: overnight (persistent) + live (fresh)
            combined = overnight_observations + fresh_live
            if not combined:
                continue

            # 2. Check cooldown
            if self._is_on_cooldown(token, now):
                continue

            # 3. Count unique sources (overnight + live)
            sources = {obs.source for obs in combined}
            if len(sources) < self._min_scanners:
                continue

            # 4. Score each direction
            bull_score, bull_sources, bull_obs = self._score_direction(combined, "bullish")
            bear_score, bear_sources, bear_obs = self._score_direction(combined, "bearish")

            # 5. Check for conflicting signals
            if bull_score > 0 and bear_score > 0:
                ratio = max(bull_score, bear_score) / min(bull_score, bear_score)
                if ratio < 2.0:
                    continue

            # 6. Choose dominant direction
            if bull_score >= bear_score and bull_score >= self._min_confluence:
                if len(bull_sources) >= self._min_scanners:
                    entry = self._create_entry(
                        combined[0].symbol, token, "BUY",
                        bull_score, bull_sources, bull_obs,
                    )
                    entries.append(entry)

            elif bear_score > bull_score and bear_score >= self._min_confluence:
                if len(bear_sources) >= self._min_scanners:
                    entry = self._create_entry(
                        combined[0].symbol, token, "SELL",
                        bear_score, bear_sources, bear_obs,
                    )
                    entries.append(entry)

        # 7. Rank by score, pick top N
        entries.sort(key=lambda e: e.confidence, reverse=True)
        top_entries = entries[:self._max_entries]

        # Set cooldowns
        for entry in top_entries:
            self._cooldowns[entry.token] = now

        # Clear live observations for entered stocks
        # Overnight bias stays (can trigger again after cooldown)
        for entry in top_entries:
            self._observations.pop(entry.token, None)

        return top_entries

    # ── Phase 4: Exit logic (minimal for now) ──

    def check_exits(self) -> list[ExitSignal]:
        """
        Confluence algo has no specific exit logic yet.
        Relies on intraday square-off at 3:15 PM.

        Future: add trailing stop or time-based exit.
        """
        exits: list[ExitSignal] = []

        for symbol, trade in list(self._active_trades.items()):
            if trade.current_price <= 0:
                continue

            # Basic stop loss: 3% below entry
            if trade.direction == "BUY":
                if trade.current_price <= trade.entry_price * 0.97:
                    exits.append(ExitSignal(
                        trade=trade,
                        reason=ExitReason.STOP_LOSS,
                        exit_price=trade.current_price,
                        description=(
                            f"Confluence SL: ₹{trade.current_price:.2f} "
                            f"dropped 3% below entry ₹{trade.entry_price:.2f}"
                        ),
                    ))

            # Basic target: 2% above entry
            if trade.direction == "BUY":
                if trade.current_price >= trade.entry_price * 1.02:
                    exits.append(ExitSignal(
                        trade=trade,
                        reason=ExitReason.PROFIT_TARGET,
                        exit_price=trade.current_price,
                        description=(
                            f"Confluence TP: ₹{trade.current_price:.2f} "
                            f"hit 2% above entry ₹{trade.entry_price:.2f}"
                        ),
                    ))

        return exits

    # ── Helpers ──

    def _score_direction(
        self,
        observations: list[Observation],
        direction: str,
    ) -> tuple[float, list[SignalSource], list[Observation]]:
        """Calculate weighted confluence score for one direction."""
        score = 0.0
        sources: list[SignalSource] = []
        matching: list[Observation] = []

        for obs in observations:
            if obs.direction != direction:
                continue
            weight = self._weights.get(obs.source, 0.05)
            score += weight * obs.confidence
            if obs.source not in sources:
                sources.append(obs.source)
            matching.append(obs)

        return score, sources, matching

    def _create_entry(
        self,
        symbol: str,
        token: str,
        direction: str,
        score: float,
        sources: list[SignalSource],
        observations: list[Observation],
    ) -> EntrySignal:
        """Create an EntrySignal from confluence scoring."""
        price = observations[-1].price if observations else 0.0

        # Stop loss and target based on direction
        if direction == "BUY":
            stop_loss = price * 0.97   # 3% stop
            target = price * 1.02      # 2% target
        else:
            stop_loss = price * 1.03
            target = price * 0.98

        return EntrySignal(
            symbol=symbol,
            token=token,
            algorithm_name=self._name,
            direction=direction,
            price=price,
            stop_loss=stop_loss,
            target_price=target,
            confidence=min(score, 1.0),
            reason=(
                f"Scanner Confluence: {len(sources)} scanners agree "
                f"({', '.join(s.value for s in sources)}) "
                f"score={score:.2f}"
            ),
            triggering_observations=observations,
            metadata={
                "confluence_score": round(score, 3),
                "scanner_count": len(sources),
                "scanner_names": [s.value for s in sources],
            },
        )

    def _is_on_cooldown(self, token: str, now: datetime) -> bool:
        if token not in self._cooldowns:
            return False
        elapsed = (now - self._cooldowns[token]).total_seconds() / 60
        return elapsed < self._cooldown_minutes

    # ── Lifecycle ──

    def reset_daily(self) -> None:
        """Reset for a new trading day."""
        self._observations.clear()
        self._overnight_bias.clear()
        self._cooldowns.clear()
        self._active_trades.clear()
        self._total_observations = 0
        self._total_evaluations = 0
        logger.info("ScannerConfluenceAlgo reset for new trading day")

    @property
    def stats(self) -> dict[str, Any]:
        base = super().stats
        base.update({
            "buffered_observations": sum(
                len(obs) for obs in self._observations.values()
            ),
            "overnight_bias_stocks": len(self._overnight_bias),
            "overnight_bias_signals": sum(
                len(obs) for obs in self._overnight_bias.values()
            ),
            "active_stocks": len(self._observations),
            "total_observations": self._total_observations,
            "total_evaluations": self._total_evaluations,
            "min_confluence": self._min_confluence,
            "min_scanners": self._min_scanners,
        })
        return base
