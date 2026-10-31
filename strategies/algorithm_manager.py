"""
Trend-Investorr: Algorithm Manager

The orchestrator that sits between scanners and trading algorithms.

Architecture:
    Scanners → Observations → AlgorithmManager → Trading Algorithms → Orders

Responsibilities:
    1. Register and manage multiple trading algorithms
    2. Route scanner observations to interested algorithms (via ObservationRouter)
    3. Call check_entries() periodically to get entry signals
    4. Call check_exits() on every tick for active trades
    5. Convert entry/exit signals into orders via the execution pipeline
    6. Track which algorithm owns which trade (for P&L attribution)

SOLID Principles:
    SRP: Manages algorithm lifecycle + delegates routing to ObservationRouter.
    OCP: New routing strategies don't require changes here.
    DIP: Depends on ObservationRouter protocol, not concrete implementations.

This REPLACES the StrategyEngine's role as the decision maker.
The StrategyEngine did confluence scoring (counting scanners).
The AlgorithmManager delegates decisions to individual algorithms,
each with their own strategy-specific entry/exit/management rules.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Callable

from loguru import logger

from core.constants import IST, SignalSource, SignalType
from data.models.market_data import Observation, Signal, Tick
from strategies.base import (
    ActiveTrade,
    EntrySignal,
    ExitReason,
    ExitSignal,
    TradingAlgorithm,
)
from strategies.routing import ObservationRouter, PassthroughRouter


class AlgorithmManager:
    """
    Central orchestrator for all trading algorithms.

    Usage:
        manager = AlgorithmManager()
        manager.register(ConnorsRSIPullback())
        manager.register(BreakoutMomentum())

        # On each tick:
        manager.on_observation(obs)         # Route to interested algorithms
        manager.on_tick(tick)               # Update active trades
        entries, exits = manager.evaluate() # Get trading decisions
    """

    def __init__(
        self,
        eval_interval_ticks: int = 50,
        router: ObservationRouter | None = None,
    ):
        """
        Args:
            eval_interval_ticks: Check entries every N ticks.
                                  Exits are checked on every tick.
            router: ObservationRouter instance that filters which algorithms
                    receive which observations. Defaults to PassthroughRouter
                    (all algorithms see all observations).
        """
        self._eval_interval = eval_interval_ticks

        # Registered algorithms
        self._algorithms: list[TradingAlgorithm] = []

        # Quick lookup by name: algorithm_name → TradingAlgorithm
        self._algo_by_name: dict[str, TradingAlgorithm] = {}

        # Source routing: SignalSource → list of algorithms interested
        # This is the FIRST filter (by scanner type).
        self._source_routing: dict[SignalSource, list[TradingAlgorithm]] = defaultdict(list)

        # Observation router: the SECOND filter (by token/watchlist).
        # Implements Strategy Pattern — swappable at runtime.
        self._router: ObservationRouter = router or PassthroughRouter()

        # Stats
        self._total_observations: int = 0
        self._total_routed: int = 0
        self._total_entries: int = 0
        self._total_exits: int = 0
        self._tick_count: int = 0

    def register(self, algorithm: TradingAlgorithm) -> None:
        """
        Register a trading algorithm.

        Builds the source routing table based on which scanner sources
        the algorithm is interested in.
        """
        self._algorithms.append(algorithm)
        self._algo_by_name[algorithm.name] = algorithm

        for source in algorithm.interested_sources():
            self._source_routing[source].append(algorithm)

        logger.info(
            f"Algorithm registered: {algorithm.name} | "
            f"listens to: {[s.value for s in algorithm.interested_sources()]} | "
            f"enabled={algorithm.enabled}"
        )

    def set_router(self, router: ObservationRouter) -> None:
        """
        Inject an ObservationRouter (Strategy Pattern).

        Called at scanner_warmup() to swap in the watchlist-based router.
        The router is immutable after construction — swapping it here
        atomically replaces the routing strategy.

        Args:
            router: Any ObservationRouter implementation.
                    WatchlistRouter for production, PassthroughRouter for fallback.
        """
        self._router = router
        logger.info(f"Router set: {router.mode} | stats={router.stats}")

    # ── Observation Routing ──

    def on_observation(self, obs: Observation) -> None:
        """
        Route a scanner observation through the two-stage filter.

        Stage 1 (Source Filter):
            Select algorithms that declared interest in this SignalSource.
            E.g., ConnorsRSI listens to momentum/breakout but not gap.

        Stage 2 (Router Filter — Strategy Pattern):
            Delegate to self._router.filter() which applies token/watchlist
            filtering. The router decides the final recipients.

        This two-stage design means:
            - Source filter is always active (inherent to algorithm design)
            - Token filter is pluggable via the router (configurable at runtime)
        """
        self._total_observations += 1

        # Stage 1: Source-based filtering (which algos care about this scanner?)
        candidates = self._source_routing.get(obs.source, [])
        if not candidates:
            return

        # Stage 2: Router-based filtering (which algos are bound to this token?)
        recipients = self._router.filter(obs, candidates)

        # Deliver to recipients
        for algo in recipients:
            try:
                algo.on_observation(obs)
                self._total_routed += 1
            except Exception as e:
                logger.error(
                    f"Algorithm {algo.name} failed on observation "
                    f"{obs.source.value}/{obs.symbol}: {e}"
                )

    # ── Tick Processing ──

    def on_tick(self, tick: Tick) -> None:
        """
        Update all active trades with new tick data.
        Called on EVERY tick from the tick handler.
        """
        self._tick_count += 1

        for algo in self._algorithms:
            if algo.enabled and algo.has_active_trades:
                try:
                    algo.on_tick(tick)
                except Exception as e:
                    logger.error(f"Algorithm {algo.name} on_tick error: {e}")

    # ── Decision Making ──

    def evaluate(self) -> tuple[list[EntrySignal], list[ExitSignal]]:
        """
        Evaluate all algorithms for entry and exit signals.

        Returns:
            (entries, exits) — lists of entry and exit signals.
            The caller (main.py) is responsible for sending these
            through the execution pipeline.
        """
        all_entries: list[EntrySignal] = []
        all_exits: list[ExitSignal] = []

        for algo in self._algorithms:
            if not algo.enabled:
                continue

            try:
                # Check exits FIRST (always — protect capital before seeking new trades)
                exits = algo.check_exits()
                for exit_sig in exits:
                    logger.info(
                        f"🔴 [{algo.name}] EXIT: {exit_sig.trade.symbol} | "
                        f"reason={exit_sig.reason.value} | "
                        f"P&L={exit_sig.trade.unrealized_pnl_pct:+.1f}% | "
                        f"{exit_sig.description}"
                    )
                all_exits.extend(exits)

                # Check entries
                entries = algo.check_entries()
                for entry_sig in entries:
                    logger.info(
                        f"🟢 [{algo.name}] ENTRY: {entry_sig.direction} "
                        f"{entry_sig.symbol} @ ₹{entry_sig.price:.2f} | "
                        f"SL=₹{entry_sig.stop_loss:.2f} | "
                        f"TP=₹{entry_sig.target_price:.2f} | "
                        f"conf={entry_sig.confidence:.0%} | "
                        f"{entry_sig.reason}"
                    )
                all_entries.extend(entries)

            except Exception as e:
                logger.error(f"Algorithm {algo.name} evaluate error: {e}")

        self._total_entries += len(all_entries)
        self._total_exits += len(all_exits)

        return all_entries, all_exits

    def check_exits_only(self) -> list[ExitSignal]:
        """
        Check ONLY exit conditions across all algorithms.
        Called on every tick (exits are time-sensitive).
        """
        all_exits: list[ExitSignal] = []

        for algo in self._algorithms:
            if not algo.enabled or not algo.has_active_trades:
                continue

            try:
                exits = algo.check_exits()
                for exit_sig in exits:
                    logger.info(
                        f"🔴 [{algo.name}] EXIT: {exit_sig.trade.symbol} | "
                        f"reason={exit_sig.reason.value} | "
                        f"P&L={exit_sig.trade.unrealized_pnl_pct:+.1f}%"
                    )
                all_exits.extend(exits)
            except Exception as e:
                logger.error(f"Algorithm {algo.name} check_exits error: {e}")

        self._total_exits += len(all_exits)
        return all_exits

    def register_fill(
        self, entry: EntrySignal, fill_price: float, quantity: int,
    ) -> None:
        """
        Called after an entry order is successfully filled.
        Creates an ActiveTrade and registers it with the owning algorithm.
        """
        # Find the algorithm that created this entry
        algo = self._get_algorithm(entry.algorithm_name)
        if not algo:
            logger.error(f"Algorithm {entry.algorithm_name} not found for fill")
            return

        trade = ActiveTrade(
            symbol=entry.symbol,
            token=entry.token,
            algorithm_name=entry.algorithm_name,
            direction=entry.direction,
            entry_price=fill_price,
            entry_time=datetime.now(IST),
            quantity=quantity,
            stop_loss=entry.stop_loss,
            target_price=entry.target_price,
            highest_price=fill_price,
            lowest_price=fill_price,
            current_price=fill_price,
            entry_observation=(
                entry.triggering_observations[0]
                if entry.triggering_observations else None
            ),
            metadata=entry.metadata,
        )

        algo.register_trade(trade)
        logger.info(
            f"✅ [{algo.name}] FILL: {entry.direction} {entry.symbol} "
            f"qty={quantity} @ ₹{fill_price:.2f} | "
            f"SL=₹{entry.stop_loss:.2f} | TP=₹{entry.target_price:.2f}"
        )

    def register_exit_fill(self, exit_sig: ExitSignal) -> None:
        """Called after an exit order is successfully filled."""
        algo = self._get_algorithm(exit_sig.trade.algorithm_name)
        if algo:
            algo.close_trade(exit_sig.trade.symbol, exit_sig.reason)

    # ── Lifecycle ──

    def reset_daily(self) -> None:
        """Reset all algorithms for a new trading day."""
        for algo in self._algorithms:
            algo.reset_daily()
        self._tick_count = 0
        # NOTE: _router is NOT replaced here — it persists
        # until next scanner_warmup() rebuilds it via set_router().
        logger.info(
            f"AlgorithmManager reset: {len(self._algorithms)} algorithms | "
            f"router={self._router.mode}"
        )

    # ── Helpers ──

    def _get_algorithm(self, name: str) -> TradingAlgorithm | None:
        for algo in self._algorithms:
            if algo.name == name:
                return algo
        return None

    @property
    def has_active_trades(self) -> bool:
        return any(a.has_active_trades for a in self._algorithms)

    @property
    def total_active_trades(self) -> int:
        return sum(len(a.active_trades) for a in self._algorithms)

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "algorithms": len(self._algorithms),
            "total_observations": self._total_observations,
            "total_routed": self._total_routed,
            "total_entries": self._total_entries,
            "total_exits": self._total_exits,
            "total_active_trades": self.total_active_trades,
            "tick_count": self._tick_count,
            "router": self._router.stats,
            "algorithm_stats": [a.stats for a in self._algorithms],
        }

    def __repr__(self) -> str:
        return (
            f"AlgorithmManager("
            f"algorithms={len(self._algorithms)}, "
            f"active_trades={self.total_active_trades}, "
            f"entries={self._total_entries})"
        )
