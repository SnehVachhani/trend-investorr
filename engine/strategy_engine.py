"""
Trend-Investorr: Strategy Engine

The BRAIN of the system. Sits between scanners and execution.

Architecture:
    Scanners → Observations → StrategyEngine → Trade Decisions → Analyzer → Risk → Execution

Rules:
    1. Scanners ONLY emit Observations (raw facts)
    2. StrategyEngine COLLECTS observations per stock
    3. StrategyEngine SCORES confluence (how many scanners agree?)
    4. StrategyEngine DECIDES which stocks to trade
    5. Only StrategyEngine has authority to trigger the execution pipeline

Confluence Scoring:
    Each scanner has a weight. When multiple scanners agree on the same
    stock in the same direction, their weighted confidences are summed.
    A trade is only triggered when:
        - confluence_score >= min_confluence (default 0.35)
        - scanner_count >= min_scanners (default 2)
        - no conflicting signals (bullish vs bearish)

Market Regime Awareness:
    The engine can adjust behavior based on market conditions:
        - Trending:  boost momentum/breakout weights
        - Choppy:    boost VWAP/mean-reversion weights
        - Crash:     disable most scanners, only allow defensive signals
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from loguru import logger

from core.constants import IST, SignalSource, SignalType
from data.models.market_data import (
    Observation,
    Signal,
    SignalStrength,
    TradeDecision,
)


class StrategyEngine:
    """
    Central decision-making engine.

    Collects observations from all scanners, applies weighted confluence
    scoring, and produces trade decisions for the best setups.

    Usage:
        engine = StrategyEngine(analyzer, order_manager, ...)
        engine.on_observation(obs)         # Called per scanner hit
        decisions = engine.evaluate()      # Called periodically
    """

    # Default scanner weights — how much each scanner's opinion matters
    DEFAULT_WEIGHTS: dict[SignalSource, float] = {
        SignalSource.VOLUME_SPIKE: 0.25,      # Volume is king in Indian markets
        SignalSource.BREAKOUT: 0.20,           # Breakout = new trend territory
        SignalSource.MOMENTUM: 0.15,           # RSI/MACD/Supertrend alignment
        SignalSource.ORB: 0.15,                # ORB = high-probability intraday
        SignalSource.ORDER_FLOW: 0.10,         # Institutional footprint
        SignalSource.VWAP: 0.08,               # VWAP = institutional benchmark
        SignalSource.GAP: 0.05,                # Gap patterns (morning only)
        SignalSource.RELATIVE_STRENGTH: 0.05,  # Sector outperformance
        SignalSource.SECTOR_ROTATION: 0.03,    # Macro sector flow
        SignalSource.DELIVERY: 0.05,           # Post-market delivery signal
        SignalSource.PATTERN: 0.03,            # Candle patterns
        SignalSource.MTF_CONFLUENCE: 0.08,     # Multi-timeframe alignment
        SignalSource.LIQUIDITY_SWEEP: 0.18,    # Stop hunt + reclaim = highest probability
    }

    def __init__(
        self,
        min_confluence: float = 0.35,
        min_scanners: int = 2,
        max_decisions_per_cycle: int = 5,
        observation_window_seconds: int = 30,
        scanner_weights: dict[SignalSource, float] | None = None,
    ):
        """
        Args:
            min_confluence: Minimum weighted confluence score to trigger trade.
            min_scanners: Minimum number of unique scanners that must agree.
            max_decisions_per_cycle: Max trade decisions per evaluation cycle.
            observation_window_seconds: How long observations stay valid.
            scanner_weights: Custom scanner weights (overrides defaults).
        """
        self._min_confluence = min_confluence
        self._min_scanners = min_scanners
        self._max_decisions = max_decisions_per_cycle
        self._window_seconds = observation_window_seconds
        self._weights = scanner_weights or self.DEFAULT_WEIGHTS.copy()

        # Per-stock observation buffer: token → list[Observation]
        self._observations: dict[str, list[Observation]] = defaultdict(list)

        # Cooldown per stock: token → last_decision_time
        self._cooldowns: dict[str, datetime] = {}
        self._cooldown_minutes: int = 30

        # Stats
        self._total_observations: int = 0
        self._total_decisions: int = 0
        self._total_evaluations: int = 0

    # ── Observation Collection ──

    def on_observation(self, obs: Observation) -> None:
        """
        Collect a scanner observation. Does NOT trigger any trade.

        This is called by the tick handler whenever ANY scanner detects
        something interesting. The StrategyEngine stores it and waits
        for the evaluate() call to make decisions.
        """
        self._total_observations += 1
        self._observations[obs.token].append(obs)

    def on_volume_spike(
        self,
        symbol: str,
        token: str,
        price: float,
        volume_ratio: float,
        buy_sell_ratio: float = 1.0,
    ) -> None:
        """
        Convenience method to create an Observation from a VolumeScanner spike.
        VolumeScanner doesn't have to_observation(), so main.py uses this.
        """
        # Volume spikes are always bullish (high volume = demand)
        # unless buy/sell ratio strongly favors selling
        direction = "bullish" if buy_sell_ratio >= 0.8 else "bearish"

        # Confidence scales with volume ratio
        if volume_ratio >= 5.0:
            confidence = 0.90
        elif volume_ratio >= 3.0:
            confidence = 0.75
        elif volume_ratio >= 2.0:
            confidence = 0.60
        else:
            confidence = 0.45

        obs = Observation(
            symbol=symbol,
            token=token,
            source=SignalSource.VOLUME_SPIKE,
            direction=direction,
            confidence=confidence,
            price=price,
            pattern_name="volume_spike",
            details={
                "volume_ratio": round(volume_ratio, 2),
                "buy_sell_ratio": round(buy_sell_ratio, 2),
            },
        )
        self.on_observation(obs)

    # ── Decision Making ──

    def evaluate(self) -> list[TradeDecision]:
        """
        Evaluate all collected observations and produce trade decisions.

        Called periodically by the tick handler (e.g. every N ticks or
        on a timer). This is where the BRAIN works:
            1. Group observations by stock
            2. Expire stale observations
            3. Calculate confluence per stock
            4. Filter by minimum thresholds
            5. Rank by confluence score
            6. Return top N decisions

        Returns:
            List of TradeDecision objects (may be empty).
        """
        self._total_evaluations += 1
        now = datetime.now(IST)

        decisions: list[TradeDecision] = []

        for token, observations in list(self._observations.items()):
            # 1. Expire stale observations
            fresh = [
                obs for obs in observations
                if (now - obs.timestamp).total_seconds() <= self._window_seconds
            ]
            if not fresh:
                del self._observations[token]
                continue

            self._observations[token] = fresh

            # 2. Check cooldown
            if self._is_on_cooldown(token, now):
                continue

            # 3. Get unique scanner sources
            sources = {obs.source for obs in fresh}
            if len(sources) < self._min_scanners:
                continue

            # 4. Calculate directional confluence scores
            bullish_score, bullish_sources, bullish_obs = self._score_direction(fresh, "bullish")
            bearish_score, bearish_sources, bearish_obs = self._score_direction(fresh, "bearish")

            # 5. Check for conflicting signals — if both sides are strong, skip
            if bullish_score > 0 and bearish_score > 0:
                ratio = max(bullish_score, bearish_score) / min(bullish_score, bearish_score)
                if ratio < 2.0:
                    # Too close — conflicting signals, skip this stock
                    logger.debug(
                        f"StrategyEngine: {fresh[0].symbol} CONFLICT — "
                        f"bullish={bullish_score:.2f} vs bearish={bearish_score:.2f}"
                    )
                    continue

            # 6. Choose the dominant direction
            if bullish_score >= bearish_score and bullish_score >= self._min_confluence:
                if len(bullish_sources) >= self._min_scanners:
                    decision = self._create_decision(
                        fresh[0].symbol, token, "BUY",
                        bullish_score, bullish_sources, bullish_obs,
                    )
                    decisions.append(decision)

            elif bearish_score > bullish_score and bearish_score >= self._min_confluence:
                if len(bearish_sources) >= self._min_scanners:
                    decision = self._create_decision(
                        fresh[0].symbol, token, "SELL",
                        bearish_score, bearish_sources, bearish_obs,
                    )
                    decisions.append(decision)

        # 7. Rank by confluence score, pick top N
        decisions.sort(key=lambda d: d.confluence_score, reverse=True)
        top_decisions = decisions[:self._max_decisions]

        # Set cooldowns for decided stocks
        for decision in top_decisions:
            self._cooldowns[decision.token] = now
            self._total_decisions += 1

        if top_decisions:
            for d in top_decisions:
                logger.info(
                    f"🧠 STRATEGY DECISION: {d.direction} {d.symbol} | "
                    f"confluence={d.confluence_score:.2f} | "
                    f"scanners={d.scanner_count} {[s.value for s in d.contributing_sources]}"
                )

        # 8. Clear processed observations for decided stocks
        for decision in top_decisions:
            self._observations.pop(decision.token, None)

        return top_decisions

    def _score_direction(
        self,
        observations: list[Observation],
        direction: str,
    ) -> tuple[float, list[SignalSource], list[Observation]]:
        """
        Calculate weighted confluence score for one direction.

        Returns (score, list_of_sources, list_of_observations).
        """
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

    def _create_decision(
        self,
        symbol: str,
        token: str,
        direction: str,
        score: float,
        sources: list[SignalSource],
        observations: list[Observation],
    ) -> TradeDecision:
        """Create a TradeDecision from aggregated observations."""
        return TradeDecision(
            symbol=symbol,
            token=token,
            direction=direction,
            confluence_score=score,
            contributing_sources=sources,
            observations=observations,
            metadata={
                "scanner_count": len(sources),
                "scanner_names": [s.value for s in sources],
                "observation_count": len(observations),
            },
        )

    def decision_to_signal(self, decision: TradeDecision) -> Signal:
        """
        Convert a TradeDecision to a Signal for the existing pipeline.

        This is the bridge between the new StrategyEngine and the
        existing Analyzer → RiskManager → OrderManager pipeline.
        """
        signal_type = SignalType.BUY if decision.direction == "BUY" else SignalType.SELL

        # Determine strength from confluence score
        if decision.confluence_score >= 0.70:
            strength = SignalStrength.EXTREME
        elif decision.confluence_score >= 0.50:
            strength = SignalStrength.STRONG
        elif decision.confluence_score >= 0.35:
            strength = SignalStrength.MODERATE
        else:
            strength = SignalStrength.WEAK

        # Best price from observations (most recent)
        price = decision.observations[-1].price if decision.observations else 0.0

        # Primary source = the highest-weighted contributing scanner
        primary_source = max(
            decision.contributing_sources,
            key=lambda s: self._weights.get(s, 0),
        ) if decision.contributing_sources else SignalSource.VOLUME_SPIKE

        return Signal(
            symbol=decision.symbol,
            token=decision.token,
            signal_type=signal_type,
            strength=strength,
            score=min(decision.confluence_score, 1.0),
            price=price,
            source=primary_source,
            metadata={
                "confluence_score": round(decision.confluence_score, 3),
                "contributing_scanners": [s.value for s in decision.contributing_sources],
                "scanner_count": decision.scanner_count,
                "strategy_engine": True,
                "observations": [
                    {
                        "source": obs.source.value,
                        "pattern": obs.pattern_name,
                        "direction": obs.direction,
                        "confidence": round(obs.confidence, 2),
                    }
                    for obs in decision.observations
                ],
            },
        )

    # ── Configuration ──

    def set_scanner_weight(self, source: SignalSource, weight: float) -> None:
        """Update weight for a specific scanner (runtime tunable)."""
        self._weights[source] = weight
        logger.info(f"StrategyEngine: {source.value} weight → {weight}")

    def set_min_confluence(self, value: float) -> None:
        self._min_confluence = value
        logger.info(f"StrategyEngine: min_confluence → {value}")

    def set_min_scanners(self, value: int) -> None:
        self._min_scanners = value
        logger.info(f"StrategyEngine: min_scanners → {value}")

    # ── Lifecycle ──

    def _is_on_cooldown(self, token: str, now: datetime) -> bool:
        if token not in self._cooldowns:
            return False
        elapsed = (now - self._cooldowns[token]).total_seconds() / 60
        return elapsed < self._cooldown_minutes

    def reset_daily(self) -> None:
        """Reset for a new trading day."""
        self._observations.clear()
        self._cooldowns.clear()
        self._total_observations = 0
        self._total_decisions = 0
        self._total_evaluations = 0
        logger.info("StrategyEngine reset for new trading day")

    @property
    def stats(self) -> dict[str, Any]:
        active_stocks = len(self._observations)
        total_obs = sum(len(obs) for obs in self._observations.values())
        return {
            "active_stocks": active_stocks,
            "buffered_observations": total_obs,
            "total_observations": self._total_observations,
            "total_decisions": self._total_decisions,
            "total_evaluations": self._total_evaluations,
            "min_confluence": self._min_confluence,
            "min_scanners": self._min_scanners,
        }

    def __repr__(self) -> str:
        return (
            f"StrategyEngine(stocks={len(self._observations)}, "
            f"decisions={self._total_decisions}, "
            f"min_confluence={self._min_confluence}, "
            f"min_scanners={self._min_scanners})"
        )
