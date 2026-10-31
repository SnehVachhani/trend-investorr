"""
Trend-Investorr: Risk Manager

Multi-gate risk validation that every order MUST pass before execution.
This is the last line of defense between signal generation and real money.

Gates (in order):
    1. Circuit Breaker Gate    — is the system halted?
    2. Market Hours Gate       — is the market open?
    3. Position Limit Gate     — max positions per stock/sector/total
    4. Capital Allocation Gate — max % of capital per trade
    5. Daily Loss Limit Gate   — max daily drawdown
    6. Position Size Gate      — calculate safe quantity
    7. Stop Loss Gate          — ensure SL is set

If ANY gate fails, the order is rejected with a detailed reason.

All thresholds are configurable via app_config (runtime-tunable).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any

from loguru import logger

from core.constants import (
    AuditEventType,
    IST,
    MARKET_OPEN,
    MARKET_CLOSE,
    ProductType,
    TransactionType,
)
from core.exceptions import RiskLimitExceededError
from core.security.audit import AuditTrail
from data.models.market_data import OrderRequest, Position, RiskDecision


@dataclass
class RiskConfig:
    """Runtime risk parameters — populated from app_config table."""

    # Capital management
    total_capital: float = 1_00_00_000.0      # ₹1 Crore paper trading
    max_capital_per_trade_pct: float = 5.0     # Max 5% of capital per trade
    max_capital_deployed_pct: float = 70.0     # Max 70% total deployed

    # Position limits
    max_open_positions: int = 20               # Max simultaneous positions
    max_positions_per_sector: int = 5          # Max per sector
    max_positions_per_stock: int = 1           # Only 1 position per stock

    # Loss limits
    max_daily_loss_pct: float = 2.0            # Halt at 2% daily loss
    max_weekly_loss_pct: float = 5.0           # Halt at 5% weekly loss
    max_per_trade_loss_pct: float = 1.0        # Max 1% loss per trade

    # Stop loss
    default_stop_loss_pct: float = 2.0         # 2% stop loss
    default_target_pct: float = 5.0            # 5% target
    trailing_stop_pct: float = 1.5             # 1.5% trailing stop

    # Order constraints
    min_order_value: float = 5_000.0           # Min ₹5,000 per order
    max_order_value: float = 5_00_000.0        # Max ₹5,00,000 per order
    min_quantity: int = 1
    max_quantity: int = 10_000


class RiskManager:
    """
    Multi-gate risk validation for every trade.

    Usage:
        risk_mgr = RiskManager(config, audit)
        decision = risk_mgr.evaluate(order_request, current_positions)
        if decision.approved:
            execute_order(order_request, decision.quantity)
    """

    def __init__(
        self,
        config: RiskConfig | None = None,
        audit: AuditTrail | None = None,
    ):
        self._config = config or RiskConfig()
        self._audit = audit

        # Daily P&L tracking
        self._daily_realized_pnl: float = 0.0
        self._daily_unrealized_pnl: float = 0.0
        self._daily_trades: int = 0
        self._trade_date: datetime | None = None
        # Entry holds: key -> (reason, trading day). While any hold is in
        # place, orders that add exposure are rejected. Holds are scoped to
        # the day they were placed (see reset_daily) and can be released
        # individually (e.g. "data_unverified" once a warm-up succeeds).
        self._entry_holds: dict[str, tuple[str, date]] = {}

        # Weekly tracking
        self._weekly_pnl: float = 0.0

    def evaluate(
        self,
        order: OrderRequest,
        positions: list[Position],
        sector: str = "",
        indicators=None,
    ) -> RiskDecision:
        """
        Run all risk gates on an order request.

        Args:
            order: The proposed order.
            positions: Current open positions.
            sector: Sector of the stock being traded.

        Returns:
            RiskDecision with approved/rejected status and safe quantity.
        """
        try:
            # Gate 1: Market hours
            rejection = self._check_market_hours(order)
            if rejection:
                return rejection

            # Gate 1b: New entries closed for the day (after square-off)
            rejection = self._check_entries_closed(order, positions)
            if rejection:
                return rejection

            # Gate 2: Position limits
            rejection = self._check_position_limits(order, positions, sector)
            if rejection:
                return rejection

            # Gate 3: Daily loss limit
            rejection = self._check_daily_loss_limit()
            if rejection:
                return rejection

            # Gate 4: Capital allocation
            max_value = self._get_max_trade_value(positions)
            if max_value <= 0:
                return self._reject(
                    order, "Capital fully deployed — no allocation available"
                )

            # Gate 5: Calculate safe position size
            quantity = self._calculate_position_size(order, max_value, indicators)
            if quantity < self._config.min_quantity:
                return self._reject(
                    order,
                    f"Calculated quantity ({quantity}) below minimum ({self._config.min_quantity})",
                )

            # Gate 6: Order value bounds
            order_value = order.price * quantity if order.price > 0 else 0
            if order_value > 0 and order_value < self._config.min_order_value:
                return self._reject(
                    order,
                    f"Order value ₹{order_value:,.0f} below minimum ₹{self._config.min_order_value:,.0f}",
                )

            # Gate 7: Calculate stop loss and target (ATR-based when available)
            stop_loss = self._calculate_stop_loss(order, indicators)
            target = self._calculate_target(order)

            # All gates passed
            decision = RiskDecision(
                approved=True,
                quantity=quantity,
                reason=f"Approved: {quantity} shares, SL=₹{stop_loss:.2f}, T=₹{target:.2f}",
                max_position_value=max_value,
                stop_loss_price=stop_loss,
                target_price=target,
            )

            self._daily_trades += 1

            if self._audit:
                self._audit.log(
                    AuditEventType.ORDER_PLACED,
                    {
                        "symbol": order.symbol,
                        "action": "risk_approved",
                        "quantity": quantity,
                        "price": order.price,
                        "stop_loss": stop_loss,
                        "target": target,
                        "daily_trades": self._daily_trades,
                    },
                )

            logger.info(
                f"✅ Risk APPROVED: {order.symbol} | "
                f"qty={quantity} | SL=₹{stop_loss:.2f} | T=₹{target:.2f} | "
                f"max_value=₹{max_value:,.0f}"
            )
            return decision

        except Exception as e:
            logger.error(f"Risk evaluation error for {order.symbol}: {e}")
            return self._reject(order, f"Risk evaluation error: {e}")

    # ── Risk Gates ───────────────────────────────────────

    def _check_market_hours(self, order: OrderRequest) -> RiskDecision | None:
        """Gate 1: Is the market open?"""
        now = datetime.now(IST).time()

        # Allow slightly before open for pre-market orders
        buffer_open = time(9, 10)

        if now < buffer_open or now > MARKET_CLOSE:
            if order.product_type == ProductType.INTRADAY:
                return self._reject(
                    order,
                    f"Market closed — intraday orders not allowed "
                    f"(current: {now.strftime('%H:%M')}, market: {MARKET_OPEN}-{MARKET_CLOSE})",
                )
        return None

    def _check_entries_closed(
        self, order: OrderRequest, positions: list[Position]
    ) -> RiskDecision | None:
        """
        Gate 1b: Are new entries closed for the day?

        Set by the intraday square-off (scheduled or replayed after a
        restart) and cleared by reset_daily() at the next pre-market.

        Only orders that *reduce* an existing holding pass: a BUY always
        adds exposure, and so does a SELL with nothing held (a short entry —
        the live path would send it to the broker as an MIS short) or a SELL
        larger than the holding (flips to short). Genuine exits pass.
        """
        if not self._entry_holds:
            return None
        reason = f"New entries closed for today ({self._entries_closed_reason})"

        if order.transaction_type == TransactionType.BUY:
            return self._reject(order, reason)

        held = sum(p.quantity for p in positions if p.symbol == order.symbol)
        if held <= 0:
            return self._reject(
                order, f"{reason}: SELL with no open position in {order.symbol} would open a short"
            )
        if order.quantity > held:
            return self._reject(
                order,
                f"{reason}: SELL {order.quantity} exceeds held {held} in {order.symbol} "
                f"— would flip to short",
            )
        return None

    def _check_position_limits(
        self,
        order: OrderRequest,
        positions: list[Position],
        sector: str,
    ) -> RiskDecision | None:
        """Gate 2: Position count limits."""
        # Skip for sell orders (reducing exposure)
        if order.transaction_type == TransactionType.SELL:
            return None

        # Total positions
        if len(positions) >= self._config.max_open_positions:
            return self._reject(
                order,
                f"Max open positions reached ({self._config.max_open_positions})",
            )

        # Per-stock limit
        stock_positions = [p for p in positions if p.symbol == order.symbol]
        if len(stock_positions) >= self._config.max_positions_per_stock:
            return self._reject(
                order,
                f"Already holding {len(stock_positions)} position(s) in {order.symbol}",
            )

        # Per-sector limit
        if sector:
            # Note: sector info would need to be on Position — for now count by convention
            sector_count = sum(1 for _ in positions)  # Placeholder — enhanced in Phase 7
            if sector_count >= self._config.max_positions_per_sector:
                logger.debug(f"Sector limit check deferred — needs sector mapping on positions")

        return None

    def _check_daily_loss_limit(self) -> RiskDecision | None:
        """Gate 3: Daily loss limit."""
        total_daily_pnl = self._daily_realized_pnl + self._daily_unrealized_pnl
        max_loss = self._config.total_capital * (self._config.max_daily_loss_pct / 100)

        if total_daily_pnl < -max_loss:
            msg = (
                f"Daily loss limit hit: ₹{total_daily_pnl:,.0f} "
                f"(limit: -₹{max_loss:,.0f} = {self._config.max_daily_loss_pct}%)"
            )
            logger.warning(f"🛑 {msg}")

            if self._audit:
                self._audit.log(
                    AuditEventType.RISK_LIMIT_HIT,
                    {
                        "limit": "daily_loss",
                        "pnl": total_daily_pnl,
                        "max_loss": max_loss,
                    },
                )

            return RiskDecision(
                approved=False,
                quantity=0,
                reason=msg,
            )
        return None

    def _get_max_trade_value(self, positions: list[Position]) -> float:
        """Calculate the maximum value available for a new trade."""
        # Total deployed capital
        deployed = sum(p.invested_value for p in positions)
        max_deployed = self._config.total_capital * (self._config.max_capital_deployed_pct / 100)
        available = max_deployed - deployed

        # Per-trade cap
        per_trade_max = self._config.total_capital * (self._config.max_capital_per_trade_pct / 100)

        # Order value cap
        return min(available, per_trade_max, self._config.max_order_value)

    def _calculate_position_size(
        self,
        order: OrderRequest,
        max_value: float,
        indicators=None,
    ) -> int:
        """
        Calculate safe position size based on capital and risk.

        Uses the risk-per-trade method:
            Position Size = Risk Capital / Stop Loss Distance

        When ATR is available, uses ATR-based stop distance instead of
        fixed percentage, giving volatility-adaptive sizing.
        """
        if order.price <= 0:
            return 0

        # Method 1: Simple capital allocation
        quantity_by_capital = int(max_value / order.price)

        # Method 2: Risk-based sizing
        # Use ATR for stop distance when available (volatility-adaptive)
        if indicators and hasattr(indicators, 'atr_14') and indicators.atr_14 > 0:
            stop_loss_distance = indicators.atr_14 * 2.0  # 2x ATR stop
        else:
            stop_loss_distance = order.price * (self._config.default_stop_loss_pct / 100)

        risk_capital = self._config.total_capital * (self._config.max_per_trade_loss_pct / 100)

        if stop_loss_distance > 0:
            quantity_by_risk = int(risk_capital / stop_loss_distance)
        else:
            quantity_by_risk = quantity_by_capital

        # Use the smaller (more conservative) quantity
        quantity = min(quantity_by_capital, quantity_by_risk)

        # Clamp to bounds
        quantity = max(self._config.min_quantity, min(quantity, self._config.max_quantity))

        return quantity

    def _calculate_stop_loss(self, order: OrderRequest, indicators=None) -> float:
        """
        Calculate stop loss price.

        Prefers ATR-based dynamic stop loss when indicators are available.
        Falls back to fixed percentage (default_stop_loss_pct) otherwise.

        ATR advantage: volatile stocks (e.g. ADANIENT ATR=40) get wider stops
        to avoid noise, while stable stocks (e.g. TCS ATR=15) get tighter stops.
        """
        if order.trigger_price > 0:
            return order.trigger_price

        if order.price <= 0:
            return 0.0

        # ATR-based dynamic stop loss (preferred)
        if indicators and hasattr(indicators, 'atr_14') and indicators.atr_14 > 0:
            atr_multiplier = 2.0  # 2x ATR is industry standard
            if order.transaction_type == TransactionType.BUY:
                return order.price - (indicators.atr_14 * atr_multiplier)
            else:
                return order.price + (indicators.atr_14 * atr_multiplier)

        # Fallback: fixed percentage (existing behavior)
        if order.transaction_type == TransactionType.BUY:
            return order.price * (1 - self._config.default_stop_loss_pct / 100)
        else:
            return order.price * (1 + self._config.default_stop_loss_pct / 100)

    def _calculate_target(self, order: OrderRequest) -> float:
        """Calculate target price."""
        if order.price <= 0:
            return 0.0

        if order.transaction_type == TransactionType.BUY:
            return order.price * (1 + self._config.default_target_pct / 100)
        else:
            return order.price * (1 - self._config.default_target_pct / 100)

    def _reject(self, order: OrderRequest, reason: str) -> RiskDecision:
        """Create a rejection decision."""
        logger.warning(f"❌ Risk REJECTED: {order.symbol} — {reason}")

        if self._audit:
            self._audit.log(
                AuditEventType.ORDER_REJECTED,
                {"symbol": order.symbol, "reason": reason},
            )

        return RiskDecision(approved=False, quantity=0, reason=reason)

    # ── P&L Updates ──────────────────────────────────────

    def update_daily_pnl(self, realized: float, unrealized: float) -> None:
        """Update daily P&L for loss limit checks."""
        self._daily_realized_pnl = realized
        self._daily_unrealized_pnl = unrealized

    def record_trade_pnl(self, pnl: float) -> None:
        """Record a closed trade's P&L."""
        self._daily_realized_pnl += pnl
        self._daily_trades += 1

    def close_new_entries(self, reason: str, key: str = "square_off") -> None:
        """
        Place an entry hold: orders that add exposure are rejected until
        the hold is released or the next trading day's reset_daily().

        Keys: "square_off" (intraday square-off), "after_close" (process
        started after close), "data_unverified" (backfill failed — trading
        data not verified). Exits remain allowed.
        """
        self._entry_holds[key] = (reason, datetime.now(IST).date())
        logger.warning(f"🔒 New entries closed for today [{key}]: {reason}")
        if self._audit:
            self._audit.log(
                AuditEventType.RISK_LIMIT_HIT,
                {"action": "entries_closed", "key": key, "reason": reason},
            )

    def release_entry_hold(self, key: str) -> bool:
        """Release one hold (e.g. data verified by a later warm-up). Other holds stay."""
        if key not in self._entry_holds:
            return False
        reason, _day = self._entry_holds.pop(key)
        logger.info(f"🔓 Entry hold released [{key}]: {reason}")
        return True

    @property
    def entries_closed(self) -> bool:
        return bool(self._entry_holds)

    def entry_gate(self, order: OrderRequest, positions: list[Position]) -> RiskDecision | None:
        """
        Re-run only the entry-hold gate (no sizing, no limits). Used by the
        order manager immediately before placement: an order approved just
        before square-off must not reach the broker after entries closed.
        """
        return self._check_entries_closed(order, positions)

    @property
    def _entries_closed_reason(self) -> str | None:
        if not self._entry_holds:
            return None
        return "; ".join(reason for reason, _day in self._entry_holds.values())

    def reset_daily(self) -> None:
        """Reset daily counters for a new trading day."""
        self._daily_realized_pnl = 0.0
        self._daily_unrealized_pnl = 0.0
        self._daily_trades = 0
        self._trade_date = datetime.now(IST)
        # Entry holds are scoped to the trading day they were placed on. A
        # same-day reset (pre-market replayed after a restart) must not
        # reopen entries that square-off already closed.
        today = self._trade_date.date()
        stale = [key for key, (_reason, day) in self._entry_holds.items() if day != today]
        for key in stale:
            del self._entry_holds[key]
        if self._entry_holds:
            logger.warning(
                f"Risk manager daily reset: keeping entries closed for today "
                f"({self._entries_closed_reason})"
            )
        logger.info("Risk manager daily counters reset")

    def update_config(self, config: RiskConfig) -> None:
        """Update risk config (from app_config refresh)."""
        self._config = config
        logger.info(f"Risk config updated: max_per_trade={config.max_capital_per_trade_pct}%")

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "daily_realized_pnl": self._daily_realized_pnl,
            "daily_unrealized_pnl": self._daily_unrealized_pnl,
            "daily_total_pnl": self._daily_realized_pnl + self._daily_unrealized_pnl,
            "daily_trades": self._daily_trades,
            "config": {
                "total_capital": self._config.total_capital,
                "max_per_trade_pct": self._config.max_capital_per_trade_pct,
                "max_daily_loss_pct": self._config.max_daily_loss_pct,
                "max_positions": self._config.max_open_positions,
            },
        }
