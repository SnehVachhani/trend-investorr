"""
Trend-Investorr: Paper Trader

Simulates order execution without touching real money or the broker API.
Mirrors the real OrderManager interface exactly so the rest of the system
doesn't know (or care) if trades are real or simulated.

Features:
    - Virtual portfolio with ₹1 Crore starting capital
    - Realistic fill simulation (market orders fill at LTP)
    - Slippage simulation (configurable, default 0.1%)
    - Position tracking with P&L calculation
    - Auto square-off at 3:15 PM for intraday
    - Full order history
    - Daily P&L reporting
"""

from __future__ import annotations

import uuid
from datetime import datetime, date
from typing import Any

from loguru import logger

from core.constants import (
    AuditEventType,
    IST,
    OrderStatus,
    ProductType,
    TransactionType,
)
from core.security.audit import AuditTrail
from data.models.market_data import (
    DailyPnL,
    OrderRequest,
    OrderResult,
    Position,
)


class PaperTrader:
    """
    Virtual order execution engine.

    Usage:
        paper = PaperTrader(capital=1_00_00_000, audit=audit)
        result = paper.place_order(order_request)
        paper.update_prices({"RELIANCE": 2850.50, ...})
        print(paper.portfolio_value)
    """

    def __init__(
        self,
        capital: float = 1_00_00_000.0,
        slippage_pct: float = 0.1,
        audit: AuditTrail | None = None,
    ):
        """
        Args:
            capital: Starting virtual capital in ₹.
            slippage_pct: Simulated slippage percentage (0.1 = 0.1%).
            audit: AuditTrail for logging paper trades.
        """
        self._initial_capital = capital
        self._cash = capital
        self._slippage_pct = slippage_pct
        self._audit = audit

        # Portfolio state
        self._positions: dict[str, Position] = {}  # symbol -> Position
        self._order_history: list[OrderResult] = []
        self._closed_trades: list[dict[str, Any]] = []

        # Daily stats
        self._today_date: date | None = None
        self._daily_realized_pnl: float = 0.0
        self._daily_trades: int = 0
        self._winning_trades: int = 0
        self._losing_trades: int = 0

    def place_order(self, order: OrderRequest) -> OrderResult:
        """
        Simulate order execution.

        BUY: deducts cash, creates/adds to position.
        SELL: releases cash, reduces/closes position.

        Returns:
            OrderResult matching the real broker response format.
        """
        try:
            self._ensure_daily_reset()

            if order.transaction_type == TransactionType.BUY:
                return self._execute_buy(order)
            else:
                return self._execute_sell(order)

        except Exception as e:
            logger.error(f"Paper trade error for {order.symbol}: {e}")
            return OrderResult(
                success=False,
                status=OrderStatus.REJECTED,
                message=f"Paper trade error: {e}",
                is_paper=True,
            )

    def _execute_buy(self, order: OrderRequest) -> OrderResult:
        """Execute a simulated buy order."""
        # Simulate fill price with slippage (buy higher)
        fill_price = order.price * (1 + self._slippage_pct / 100) if order.price > 0 else 0
        total_cost = fill_price * order.quantity

        # Check cash
        if total_cost > self._cash:
            return OrderResult(
                success=False,
                status=OrderStatus.REJECTED,
                message=f"Insufficient paper capital: need ₹{total_cost:,.0f}, have ₹{self._cash:,.0f}",
                is_paper=True,
            )

        # Deduct cash
        self._cash -= total_cost

        # Create or update position
        order_id = f"PAPER-{uuid.uuid4().hex[:8].upper()}"

        if order.symbol in self._positions:
            existing = self._positions[order.symbol]
            # Average up
            total_qty = existing.quantity + order.quantity
            total_value = (existing.avg_buy_price * existing.quantity) + (fill_price * order.quantity)
            new_avg = total_value / total_qty

            self._positions[order.symbol] = Position(
                symbol=order.symbol,
                token=order.token,
                quantity=total_qty,
                avg_buy_price=new_avg,
                current_price=fill_price,
                product_type=order.product_type,
                is_paper=True,
            )
        else:
            self._positions[order.symbol] = Position(
                symbol=order.symbol,
                token=order.token,
                quantity=order.quantity,
                avg_buy_price=fill_price,
                current_price=fill_price,
                product_type=order.product_type,
                is_paper=True,
            )

        self._daily_trades += 1

        result = OrderResult(
            success=True,
            order_id=order_id,
            broker_order_id=order_id,
            status=OrderStatus.FILLED,
            filled_qty=order.quantity,
            avg_fill_price=fill_price,
            message=f"Paper BUY filled at ₹{fill_price:.2f} (slippage: {self._slippage_pct}%)",
            is_paper=True,
        )
        self._order_history.append(result)

        if self._audit:
            self._audit.log(
                AuditEventType.ORDER_FILLED,
                {
                    "symbol": order.symbol,
                    "side": "BUY",
                    "quantity": order.quantity,
                    "fill_price": fill_price,
                    "total_cost": total_cost,
                    "paper": True,
                    "order_id": order_id,
                },
            )

        logger.info(
            f"📄 PAPER BUY: {order.symbol} | qty={order.quantity} | "
            f"₹{fill_price:.2f} | cost=₹{total_cost:,.0f} | "
            f"cash=₹{self._cash:,.0f}"
        )
        return result

    def _execute_sell(self, order: OrderRequest) -> OrderResult:
        """Execute a simulated sell order."""
        if order.symbol not in self._positions:
            return OrderResult(
                success=False,
                status=OrderStatus.REJECTED,
                message=f"No paper position in {order.symbol} to sell",
                is_paper=True,
            )

        position = self._positions[order.symbol]
        sell_qty = min(order.quantity, position.quantity)

        # Simulate fill price with slippage (sell lower)
        fill_price = order.price * (1 - self._slippage_pct / 100) if order.price > 0 else position.current_price
        total_proceeds = fill_price * sell_qty

        # Calculate P&L
        trade_pnl = (fill_price - position.avg_buy_price) * sell_qty
        self._daily_realized_pnl += trade_pnl

        if trade_pnl >= 0:
            self._winning_trades += 1
        else:
            self._losing_trades += 1

        # Release cash
        self._cash += total_proceeds

        # Record closed trade
        self._closed_trades.append({
            "symbol": order.symbol,
            "buy_price": position.avg_buy_price,
            "sell_price": fill_price,
            "quantity": sell_qty,
            "pnl": trade_pnl,
            "pnl_pct": ((fill_price - position.avg_buy_price) / position.avg_buy_price) * 100,
            "closed_at": datetime.now(IST).isoformat(),
        })

        # Update or remove position
        remaining = position.quantity - sell_qty
        if remaining > 0:
            self._positions[order.symbol] = Position(
                symbol=order.symbol,
                token=order.token,
                quantity=remaining,
                avg_buy_price=position.avg_buy_price,
                current_price=fill_price,
                product_type=position.product_type,
                is_paper=True,
            )
        else:
            del self._positions[order.symbol]

        self._daily_trades += 1
        order_id = f"PAPER-{uuid.uuid4().hex[:8].upper()}"

        result = OrderResult(
            success=True,
            order_id=order_id,
            broker_order_id=order_id,
            status=OrderStatus.FILLED,
            filled_qty=sell_qty,
            avg_fill_price=fill_price,
            message=f"Paper SELL filled at ₹{fill_price:.2f} | P&L: ₹{trade_pnl:+,.0f}",
            is_paper=True,
        )
        self._order_history.append(result)

        pnl_emoji = "💚" if trade_pnl >= 0 else "🔴"

        if self._audit:
            self._audit.log(
                AuditEventType.ORDER_FILLED,
                {
                    "symbol": order.symbol,
                    "side": "SELL",
                    "quantity": sell_qty,
                    "fill_price": fill_price,
                    "pnl": trade_pnl,
                    "paper": True,
                    "order_id": order_id,
                },
            )

        logger.info(
            f"📄 PAPER SELL: {order.symbol} | qty={sell_qty} | "
            f"₹{fill_price:.2f} | {pnl_emoji} P&L=₹{trade_pnl:+,.0f} | "
            f"cash=₹{self._cash:,.0f}"
        )
        return result

    def update_prices(self, prices: dict[str, float]) -> None:
        """
        Update current prices for all held positions.

        Args:
            prices: Dict of symbol -> latest price.
        """
        for symbol, price in prices.items():
            if symbol in self._positions:
                pos = self._positions[symbol]
                self._positions[symbol] = Position(
                    symbol=pos.symbol,
                    token=pos.token,
                    quantity=pos.quantity,
                    avg_buy_price=pos.avg_buy_price,
                    current_price=price,
                    product_type=pos.product_type,
                    is_paper=True,
                    opened_at=pos.opened_at,
                )

    def square_off_intraday(self) -> list[OrderResult]:
        """
        Auto square-off all intraday positions (called at 3:15 PM).

        Returns:
            List of sell OrderResults.
        """
        results = []
        intraday = [
            p for p in self._positions.values()
            if p.product_type == ProductType.INTRADAY
        ]

        for pos in intraday:
            order = OrderRequest(
                symbol=pos.symbol,
                token=pos.token,
                transaction_type=TransactionType.SELL,
                quantity=pos.quantity,
                price=pos.current_price,
                product_type=ProductType.INTRADAY,
            )
            result = self.place_order(order)
            results.append(result)

        if results:
            logger.info(f"📄 Squared off {len(results)} intraday paper positions")

        return results

    def _ensure_daily_reset(self) -> None:
        """Reset daily counters if it's a new day."""
        today = datetime.now(IST).date()
        if self._today_date != today:
            self._today_date = today
            self._daily_realized_pnl = 0.0
            self._daily_trades = 0
            self._winning_trades = 0
            self._losing_trades = 0

    # ── Accessors ────────────────────────────────────────

    @property
    def positions(self) -> list[Position]:
        return list(self._positions.values())

    @property
    def cash(self) -> float:
        return self._cash

    @property
    def portfolio_value(self) -> float:
        """Total portfolio value (cash + positions)."""
        position_value = sum(p.current_value for p in self._positions.values())
        return self._cash + position_value

    @property
    def total_pnl(self) -> float:
        """Total P&L since inception."""
        return self.portfolio_value - self._initial_capital

    @property
    def total_pnl_pct(self) -> float:
        if self._initial_capital == 0:
            return 0.0
        return (self.total_pnl / self._initial_capital) * 100

    @property
    def unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl for p in self._positions.values())

    def get_daily_summary(self) -> DailyPnL:
        """Get today's P&L summary."""
        return DailyPnL(
            date=datetime.now(IST).date(),
            realized_pnl=self._daily_realized_pnl,
            unrealized_pnl=self.unrealized_pnl,
            total_trades=self._daily_trades,
            winning_trades=self._winning_trades,
            losing_trades=self._losing_trades,
            is_paper=True,
        )

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "cash": self._cash,
            "portfolio_value": self.portfolio_value,
            "total_pnl": self.total_pnl,
            "total_pnl_pct": f"{self.total_pnl_pct:+.2f}%",
            "positions": len(self._positions),
            "unrealized_pnl": self.unrealized_pnl,
            "daily_realized_pnl": self._daily_realized_pnl,
            "daily_trades": self._daily_trades,
            "win_rate": f"{(self._winning_trades / max(self._daily_trades, 1)) * 100:.0f}%",
            "total_closed_trades": len(self._closed_trades),
        }

    def get_position(self, symbol: str) -> Position | None:
        return self._positions.get(symbol)

    @property
    def order_history(self) -> list[OrderResult]:
        return list(self._order_history)
