"""
Trend-Investorr: Trading Algorithm Base

Abstract base class for all trading algorithms (strategies).

Architecture:
    Scanners (eyes) → Observations → AlgorithmManager → TradingAlgorithm → OrderManager
    
    Each TradingAlgorithm is a self-contained strategy with:
        - Entry conditions (when to open a position)
        - Exit conditions (when to close — profit, stop, time, indicator)
        - Trade management (trailing stops, partial exits, scaling)
        - Position tracking (what it currently holds, entry price/time)
        - State machine (IDLE → WATCHING → IN_TRADE → EXITED)

Scanners vs Algorithms:
    Scanner:   "RSI is 28 on RELIANCE"              (fact, no action)
    Algorithm: "RSI < 30 AND price > SMA200 → BUY"  (decision + action)
    
    Scanner:   No concept of exit, no state
    Algorithm: "RSI crossed 40 → EXIT" / "10 days elapsed → EXIT"
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from core.constants import IST, SignalSource, SignalType
from data.models.market_data import Observation, Signal, SignalStrength, Tick


# ── Enums ──


class AlgorithmState(str, Enum):
    """State of a trading algorithm."""
    IDLE = "idle"               # No active setup, just listening
    WATCHING = "watching"       # Setup detected, waiting for entry conditions
    PENDING_ENTRY = "pending"   # Entry conditions met, waiting for next candle/tick to enter
    IN_TRADE = "in_trade"       # Position is open, managing the trade
    PENDING_EXIT = "pending_exit"  # Exit conditions met, waiting to close


class ExitReason(str, Enum):
    """Why a trade was exited."""
    PROFIT_TARGET = "profit_target"
    STOP_LOSS = "stop_loss"
    TRAILING_STOP = "trailing_stop"
    TIME_STOP = "time_stop"
    INDICATOR_EXIT = "indicator_exit"
    MANUAL = "manual"
    SQUARE_OFF = "square_off"         # End-of-day auto square-off


# ── Data Models ──


@dataclass
class ActiveTrade:
    """
    Represents a trade currently being managed by an algorithm.
    
    Created when the algorithm decides to enter, updated on each tick,
    closed when exit conditions are met.
    """
    symbol: str
    token: str
    algorithm_name: str
    direction: str                          # "BUY" or "SELL"
    entry_price: float
    entry_time: datetime
    quantity: int = 0
    
    # Risk levels set by the algorithm
    stop_loss: float = 0.0
    target_price: float = 0.0
    trailing_stop: float = 0.0
    
    # Trade management state
    highest_price: float = 0.0              # Highest price since entry (for trailing)
    lowest_price: float = float("inf")      # Lowest price since entry
    current_price: float = 0.0
    ticks_since_entry: int = 0
    candles_since_entry: int = 0            # Trading days/candles since entry
    
    # P&L tracking
    unrealized_pnl: float = 0.0
    unrealized_pnl_pct: float = 0.0
    
    # Metadata from the triggering observation
    entry_observation: Observation | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def update_price(self, price: float) -> None:
        """Update with latest price."""
        self.current_price = price
        self.ticks_since_entry += 1
        
        if price > self.highest_price:
            self.highest_price = price
        if price < self.lowest_price:
            self.lowest_price = price
        
        # Calculate P&L
        if self.direction == "BUY":
            self.unrealized_pnl = (price - self.entry_price) * self.quantity
            self.unrealized_pnl_pct = (price - self.entry_price) / self.entry_price * 100
        else:
            self.unrealized_pnl = (self.entry_price - price) * self.quantity
            self.unrealized_pnl_pct = (self.entry_price - price) / self.entry_price * 100


@dataclass
class EntrySignal:
    """
    An algorithm's decision to enter a trade.
    
    This is NOT a scanner observation. This is a concrete trading decision
    with specific price, stop loss, target, and position sizing intent.
    """
    symbol: str
    token: str
    algorithm_name: str
    direction: str                  # "BUY" or "SELL"
    price: float                    # Intended entry price
    stop_loss: float                # Where to place stop loss
    target_price: float             # Profit target
    confidence: float               # 0.0-1.0 how confident the algorithm is
    reason: str                     # Human-readable reason
    
    # What triggered this entry
    triggering_observations: list[Observation] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExitSignal:
    """
    An algorithm's decision to exit a trade.
    """
    trade: ActiveTrade
    reason: ExitReason
    exit_price: float
    description: str                # Human-readable reason
    metadata: dict[str, Any] = field(default_factory=dict)


# ── Base Class ──


class TradingAlgorithm(ABC):
    """
    Abstract base class for all trading algorithms.

    Every algorithm must implement:
        - on_observation(): process incoming scanner data
        - on_tick(): update active trades with new price
        - check_entries(): evaluate if entry conditions are met
        - check_exits(): evaluate if exit conditions are met for active trades

    The AlgorithmManager calls these methods in the correct order
    during the tick processing loop.

    Lifecycle:
        1. Scanners emit observations
        2. AlgorithmManager routes relevant observations to this algorithm
        3. Algorithm stores observations it cares about (on_observation)
        4. On each tick, algorithm updates active trades (on_tick)
        5. Periodically, algorithm checks if entry conditions met (check_entries)
        6. For active trades, algorithm checks exit conditions (check_exits)
    """

    def __init__(self, name: str, enabled: bool = True):
        self._name = name
        self._enabled = enabled
        
        # Active trades managed by this algorithm: symbol → ActiveTrade
        self._active_trades: dict[str, ActiveTrade] = {}
        
        # Stats
        self._total_entries: int = 0
        self._total_exits: int = 0
        self._winning_exits: int = 0
        self._losing_exits: int = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def active_trades(self) -> dict[str, ActiveTrade]:
        return self._active_trades

    @property
    def has_active_trades(self) -> bool:
        return len(self._active_trades) > 0

    # ── Abstract Methods (must implement) ──

    @abstractmethod
    def interested_sources(self) -> list[SignalSource]:
        """
        Which scanner sources does this algorithm care about?
        
        The AlgorithmManager uses this to route observations efficiently.
        Only observations from these sources will be sent to on_observation().
        
        Example: ConnorsRSI only cares about MomentumScanner observations.
        """
        ...

    @abstractmethod
    def on_observation(self, obs: Observation) -> None:
        """
        Process an incoming scanner observation.
        
        The algorithm should store relevant observations and update its
        internal state. This does NOT trigger any trade — it just feeds
        information to the algorithm.
        """
        ...

    @abstractmethod
    def on_tick(self, tick: Tick) -> None:
        """
        Update active trades with new tick data.
        
        Called on EVERY tick (not just when scanners fire).
        Use this to:
            - Update current price of active trades
            - Track highest/lowest price for trailing stops
            - Count ticks/time elapsed
        """
        ...

    @abstractmethod
    def check_entries(self) -> list[EntrySignal]:
        """
        Check if any entry conditions are met.
        
        Called periodically by the AlgorithmManager.
        Returns a list of EntrySignal objects for stocks that meet
        all entry criteria.
        
        Important: This only PROPOSES entries. The AlgorithmManager
        still sends them through RiskManager before executing.
        """
        ...

    @abstractmethod
    def check_exits(self) -> list[ExitSignal]:
        """
        Check if any active trades should be exited.
        
        Called on every tick (or periodically) by the AlgorithmManager.
        Returns a list of ExitSignal objects for trades that should be closed.
        """
        ...

    # ── Common Methods (shared by all algorithms) ──

    def register_trade(self, trade: ActiveTrade) -> None:
        """
        Called by AlgorithmManager after a trade is successfully placed.
        Stores the trade for ongoing management.
        """
        self._active_trades[trade.symbol] = trade
        self._total_entries += 1

    def close_trade(self, symbol: str, reason: ExitReason) -> ActiveTrade | None:
        """Remove a trade from active tracking after it's closed."""
        trade = self._active_trades.pop(symbol, None)
        if trade:
            self._total_exits += 1
            if trade.unrealized_pnl >= 0:
                self._winning_exits += 1
            else:
                self._losing_exits += 1
        return trade

    def is_in_trade(self, symbol: str) -> bool:
        """Check if this algorithm currently has a position in a stock."""
        return symbol in self._active_trades

    def reset_daily(self) -> None:
        """
        Reset for a new trading day.
        
        Override in subclass if algorithm has overnight state.
        Default behavior: clear all active trades (intraday assumption).
        """
        self._active_trades.clear()

    def entry_to_signal(self, entry: EntrySignal) -> Signal:
        """
        Convert an EntrySignal to a Signal for the existing pipeline.
        
        This bridges the algorithm layer with the existing
        Analyzer → RiskManager → OrderManager pipeline.
        """
        signal_type = SignalType.BUY if entry.direction == "BUY" else SignalType.SELL

        if entry.confidence >= 0.80:
            strength = SignalStrength.EXTREME
        elif entry.confidence >= 0.60:
            strength = SignalStrength.STRONG
        elif entry.confidence >= 0.40:
            strength = SignalStrength.MODERATE
        else:
            strength = SignalStrength.WEAK

        # Determine primary source from triggering observations
        primary_source = SignalSource.MOMENTUM
        if entry.triggering_observations:
            primary_source = entry.triggering_observations[0].source

        return Signal(
            symbol=entry.symbol,
            token=entry.token,
            signal_type=signal_type,
            strength=strength,
            score=entry.confidence,
            price=entry.price,
            source=primary_source,
            metadata={
                "algorithm": entry.algorithm_name,
                "stop_loss": entry.stop_loss,
                "target_price": entry.target_price,
                "entry_reason": entry.reason,
                "algorithm_confidence": round(entry.confidence, 3),
                **entry.metadata,
            },
        )

    @property
    def stats(self) -> dict[str, Any]:
        win_rate = (
            self._winning_exits / self._total_exits * 100
            if self._total_exits > 0 else 0.0
        )
        return {
            "name": self._name,
            "enabled": self._enabled,
            "active_trades": len(self._active_trades),
            "total_entries": self._total_entries,
            "total_exits": self._total_exits,
            "winning_exits": self._winning_exits,
            "losing_exits": self._losing_exits,
            "win_rate": round(win_rate, 1),
        }

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"name={self._name}, "
            f"active={len(self._active_trades)}, "
            f"entries={self._total_entries})"
        )
