"""
Trend-Investorr: Watchlist Manager Service

Orchestrates watchlist stores, binding stores, and produces router instances.
This is the single-entry-point service that main.py uses — hiding the
complexity of multiple stores and routing table construction.

Architecture (SOLID):
    SRP:  One job — coordinate watchlist operations and produce routers.
    OCP:  Extendable via new router types without modifying this class.
    DIP:  main.py depends on WatchlistManager, not individual stores.
    Facade: Single clean interface for a complex subsystem.

    ┌──────────────────────────────────┐
    │        WatchlistManager          │ ← Facade (main.py uses this)
    │  - load_router() → Router       │
    │  - get_subscribe_tokens()       │
    │  - reload()                     │
    └────────┬──────────┬─────────────┘
             │          │
    ┌────────┴──┐  ┌────┴──────────────┐
    │ Watchlist  │  │ WatchlistAlgorithm │
    │   Store    │  │  BindingStore      │
    └───────────┘  └────────────────────┘
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from storage.models import WatchlistStore, WatchlistAlgorithmBindingStore
from strategies.routing import (
    ObservationRouter,
    PassthroughRouter,
    WatchlistRouter,
)


class WatchlistManager:
    """
    Service layer for watchlist-driven operations.

    Coordinates between:
        - WatchlistStore (stock ↔ watchlist CRUD)
        - WatchlistAlgorithmBindingStore (algorithm ↔ watchlist bindings)
        - ObservationRouter (routing logic)

    Lifecycle:
        __init__() → created during startup (Step 3)
        load_router() → called at scanner_warmup() → returns router for AlgorithmManager
        get_subscribe_tokens() → called at scanner_warmup() → returns tokens for WebSocket
        reload() → called manually or on schedule → refreshes routing without restart

    Usage:
        manager = WatchlistManager(db)
        router = manager.load_router()
        tokens = manager.get_subscribe_tokens()
    """

    def __init__(self, watchlist_store: WatchlistStore, binding_store: WatchlistAlgorithmBindingStore):
        """
        Args:
            watchlist_store: CRUD for watchlists and watchlist items.
            binding_store: CRUD for watchlist ↔ algorithm bindings.
        """
        self._watchlist_store = watchlist_store
        self._binding_store = binding_store

        # Current active router (cached between scanner_warmup cycles)
        self._active_router: ObservationRouter | None = None

        # Cache of the routing table for stats/debugging
        self._last_routing_table: dict[str, set[str]] = {}

    # ── Router Construction ──────────────────────────────

    def load_router(self) -> ObservationRouter:
        """
        Build and return the appropriate ObservationRouter.

        Strategy selection:
            - If watchlist bindings exist → WatchlistRouter (filtered)
            - If no bindings → PassthroughRouter (backwards compatible)

        This is called once per scanner_warmup() cycle. The returned
        router is immutable and thread-safe for concurrent tick processing.

        Returns:
            ObservationRouter instance (WatchlistRouter or PassthroughRouter).
        """
        try:
            routing_table = self._binding_store.build_routing_table()
        except Exception as e:
            logger.error(f"Failed to build routing table: {e} — using passthrough router")
            routing_table = {}

        if routing_table:
            router = WatchlistRouter(routing_table)
            self._last_routing_table = routing_table
            logger.info(
                f"WatchlistManager: loaded WatchlistRouter — "
                f"{len(routing_table)} tokens, "
                f"{len(router.bound_algorithms)} algorithms"
            )
        else:
            router = PassthroughRouter()
            self._last_routing_table = {}
            logger.info(
                "WatchlistManager: no bindings found — "
                "using PassthroughRouter (all algorithms see all observations)"
            )

        self._active_router = router
        return router

    def reload(self) -> ObservationRouter:
        """
        Reload the router from DB (hot-reload without restart).

        Useful when bindings are changed via CLI while the system
        is running. The next observation will use the new router.

        Returns:
            New ObservationRouter instance.
        """
        logger.info("WatchlistManager: reloading routing table from DB...")
        return self.load_router()

    # ── WebSocket Subscription ───────────────────────────

    def get_subscribe_tokens(self) -> list[str]:
        """
        Get all unique tokens from all active watchlists for WebSocket subscription.

        This returns the UNION of all tokens across all active watchlists,
        regardless of algorithm bindings. Filtering happens at the router level.

        Returns:
            List of SmartAPI tokens. Empty list if no watchlists are populated.
        """
        try:
            tokens = self._binding_store.get_subscribe_tokens()
            if tokens:
                logger.info(
                    f"WatchlistManager: {len(tokens)} tokens from active watchlists"
                )
            return tokens
        except Exception as e:
            logger.error(f"Failed to load watchlist tokens: {e}")
            return []

    # ── Watchlist CRUD (Delegation) ──────────────────────

    def create_watchlist(
        self, name: str, description: str = "",
        list_type: str = "custom", max_stocks: int = 500,
    ) -> int | None:
        """Create a new watchlist."""
        return self._watchlist_store.create_watchlist(name, description, list_type, max_stocks)

    def delete_watchlist(self, name: str) -> bool:
        """Soft-delete a watchlist."""
        return self._watchlist_store.delete_watchlist(name)

    def get_all_watchlists(self) -> list[dict]:
        """Get all active watchlists with stock counts and bound algorithms."""
        return self._watchlist_store.get_all_with_counts()

    def get_watchlist_detail(self, name: str) -> dict | None:
        """Get a single watchlist with its items and bindings."""
        wl = self._watchlist_store.get_watchlist(name)
        if not wl:
            return None

        items = self._watchlist_store.get_items(name)
        bindings = self._binding_store.get_bindings_for_watchlist(name)

        return {
            **wl,
            "items": items,
            "bindings": [b["algorithm_name"] for b in bindings],
            "stock_count": len(items),
        }

    def add_stock(self, watchlist_name: str, stock_id: int, source: str = "manual") -> bool:
        """Add a stock to a watchlist."""
        return self._watchlist_store.add_stock(watchlist_name, stock_id, source)

    def remove_stock(self, watchlist_name: str, stock_id: int) -> None:
        """Remove a stock from a watchlist."""
        self._watchlist_store.remove_stock(watchlist_name, stock_id)

    # ── Binding CRUD (Delegation) ────────────────────────

    def bind_algorithm(self, watchlist_name: str, algorithm_name: str) -> bool:
        """Bind an algorithm to a watchlist."""
        return self._binding_store.bind(watchlist_name, algorithm_name)

    def unbind_algorithm(self, watchlist_name: str, algorithm_name: str) -> bool:
        """Unbind an algorithm from a watchlist."""
        return self._binding_store.unbind(watchlist_name, algorithm_name)

    def get_algorithm_bindings(self, algorithm_name: str) -> list[dict]:
        """Get all watchlists bound to an algorithm."""
        return self._binding_store.get_bindings_for_algorithm(algorithm_name)

    # ── Diagnostics ──────────────────────────────────────

    def get_routing_summary(self) -> list[dict]:
        """Get human-readable routing summary for CLI/dashboard."""
        return self._binding_store.get_routing_summary()

    @property
    def active_router(self) -> ObservationRouter | None:
        """The currently active router (None before first load_router call)."""
        return self._active_router

    @property
    def stats(self) -> dict[str, Any]:
        """Service-level stats for monitoring."""
        router_stats = self._active_router.stats if self._active_router else {"mode": "not_loaded"}
        return {
            "router": router_stats,
            "routed_tokens": len(self._last_routing_table),
            "bound_algorithms": (
                sorted(set(
                    algo
                    for algos in self._last_routing_table.values()
                    for algo in algos
                ))
                if self._last_routing_table else []
            ),
        }

    def __repr__(self) -> str:
        mode = self._active_router.mode if self._active_router else "not_loaded"
        return (
            f"WatchlistManager(mode={mode}, "
            f"tokens={len(self._last_routing_table)})"
        )
