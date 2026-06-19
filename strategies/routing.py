"""
Trend-Investorr: Observation Routing

Implements the Strategy Pattern for observation filtering.
The router decides WHICH algorithms receive WHICH observations.

Architecture (SOLID):
    ┌─────────────────────────────┐
    │     ObservationRouter       │  ← Protocol (DIP: depend on abstraction)
    │  filter(obs, candidates)    │
    │  stats                      │
    └────────────┬────────────────┘
                 │
    ┌────────────┴────────────────┐
    │                             │
    ▼                             ▼
  WatchlistRouter           PassthroughRouter
  (token-based filter)      (allow everything)
  (future: CompositeRouter, SectorRouter, TimeWindowRouter...)

Design Principles:
    SRP:  Routing logic lives here, NOT in AlgorithmManager.
    OCP:  New routing strategies = new classes, no changes to existing code.
    DIP:  AlgorithmManager depends on ObservationRouter protocol, not concrete impl.
    ISP:  Router interface is minimal — just filter() and stats.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from loguru import logger

from data.models.market_data import Observation
from strategies.base import TradingAlgorithm


# ── Router Protocol (Interface) ──────────────────────────────

@runtime_checkable
class ObservationRouter(Protocol):
    """
    Strategy interface for observation routing.

    All routers must implement filter() which takes an observation
    and a list of candidate algorithms, returning only the ones
    that should receive this observation.

    This is the abstraction that AlgorithmManager depends on (DIP).
    New routing strategies implement this protocol — no changes needed
    in AlgorithmManager or existing routers (OCP).
    """

    def filter(
        self,
        obs: Observation,
        candidates: list[TradingAlgorithm],
    ) -> list[TradingAlgorithm]:
        """
        Filter candidate algorithms that should receive this observation.

        Args:
            obs: The scanner observation to route.
            candidates: Algorithms that declared interest in this source
                        (already filtered by SignalSource).

        Returns:
            Subset of candidates that should receive the observation.
            May return empty list (observation is dropped).
        """
        ...

    @property
    def mode(self) -> str:
        """Human-readable routing mode name for logging/stats."""
        ...

    @property
    def stats(self) -> dict[str, Any]:
        """Routing statistics for monitoring."""
        ...


# ── Passthrough Router (Fallback) ────────────────────────────

class PassthroughRouter:
    """
    Routes all observations to all interested algorithms.

    This is the default/fallback router used when no watchlist
    bindings are configured. Preserves the original system behavior
    where every algorithm sees every observation.

    Used when:
        - Fresh install (no bindings in DB)
        - Development/testing mode
        - Explicit opt-in to "see everything" mode
    """

    def __init__(self):
        self._total_routed: int = 0

    def filter(
        self,
        obs: Observation,
        candidates: list[TradingAlgorithm],
    ) -> list[TradingAlgorithm]:
        """Pass all enabled candidates through (no filtering)."""
        result = [algo for algo in candidates if algo.enabled]
        self._total_routed += len(result)
        return result

    @property
    def mode(self) -> str:
        return "passthrough"

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "total_routed": self._total_routed,
        }


# ── Watchlist Router (Token-Based Filter) ────────────────────

class WatchlistRouter:
    """
    Routes observations based on watchlist → algorithm bindings.

    Uses a pre-built routing table (token → set[algorithm_name])
    to decide which algorithms are allowed to process each observation.

    This implements Option (A): explicit binding is required.
    Algorithms without any binding receive NOTHING.

    The routing table is immutable after construction — thread-safe
    for concurrent tick processing. To update, create a new router
    instance at the next scanner_warmup() cycle.

    Performance:
        - O(1) token lookup (dict)
        - O(1) algorithm name check (set)
        - Total: O(k) where k = number of interested algorithms (tiny, 2-5)

    Usage:
        routing = binding_store.build_routing_table()
        router = WatchlistRouter(routing)
        algorithm_manager.set_router(router)
    """

    def __init__(self, routing_table: dict[str, set[str]]):
        """
        Args:
            routing_table: {token: {algorithm_name, ...}} mapping.
                           Built by WatchlistAlgorithmBindingStore.build_routing_table().
        """
        # Freeze the routing table — immutable after construction
        self._routing: dict[str, frozenset[str]] = {
            token: frozenset(algos)
            for token, algos in routing_table.items()
        }

        # Pre-compute all algorithm names that have ANY binding
        self._bound_algorithms: frozenset[str] = frozenset(
            algo
            for algos in routing_table.values()
            for algo in algos
        )

        # Stats
        self._total_routed: int = 0
        self._total_filtered: int = 0
        self._total_dropped_unbound_token: int = 0

        logger.info(
            f"WatchlistRouter initialized: {len(self._routing)} tokens → "
            f"{len(self._bound_algorithms)} algorithms "
            f"({', '.join(sorted(self._bound_algorithms))})"
        )

    def filter(
        self,
        obs: Observation,
        candidates: list[TradingAlgorithm],
    ) -> list[TradingAlgorithm]:
        """
        Filter candidates to only those bound to this observation's token.

        Logic:
            1. Look up obs.token in routing table
            2. If token not found → observation is dropped (no algo wants it)
            3. If token found → only return algorithms in the allowed set
        """
        allowed = self._routing.get(obs.token)

        if allowed is None:
            # No algorithm is bound to a watchlist containing this token
            self._total_dropped_unbound_token += 1
            return []

        result = [
            algo for algo in candidates
            if algo.enabled and algo.name in allowed
        ]

        self._total_routed += len(result)
        self._total_filtered += len(candidates) - len(result)
        return result

    @property
    def mode(self) -> str:
        return "watchlist_filtered"

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "routed_tokens": len(self._routing),
            "bound_algorithms": sorted(self._bound_algorithms),
            "total_routed": self._total_routed,
            "total_filtered": self._total_filtered,
            "total_dropped_unbound_token": self._total_dropped_unbound_token,
        }

    @property
    def bound_algorithms(self) -> frozenset[str]:
        """Set of algorithm names that have at least one watchlist binding."""
        return self._bound_algorithms

    def get_tokens_for_algorithm(self, algorithm_name: str) -> set[str]:
        """Get all tokens that a specific algorithm is allowed to trade."""
        return {
            token for token, algos in self._routing.items()
            if algorithm_name in algos
        }

    def __repr__(self) -> str:
        return (
            f"WatchlistRouter("
            f"tokens={len(self._routing)}, "
            f"algorithms={len(self._bound_algorithms)}, "
            f"routed={self._total_routed})"
        )
