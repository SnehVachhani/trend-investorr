"""
Trend-Investorr: Order Manager

Orchestrates the full order lifecycle:
    Signal → Risk Check → Circuit Breaker → Analyzer Validation
    → Position Sizing → Order Placement (Paper or Live)
    → Fill Tracking → P&L Recording

This is the top-level trading coordinator that ties together
risk_manager, circuit_breaker, paper_trader, and the real broker.

Modes:
    PAPER: All orders go through PaperTrader (default, safe)
    LIVE:  Orders go through SmartAPI (requires credentials)
    DUAL:  Both paper AND live simultaneously (shadow trading)
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from loguru import logger

from core.constants import (
    AuditEventType,
    IST,
    OrderStatus,
    ProductType,
    TransactionType,
)
from core.exceptions import (
    CircuitBreakerOpenError,
    OrderRejectedError,
    RiskLimitExceededError,
)
from core.security.audit import AuditTrail
from data.models.market_data import (
    AnalysisResult,
    OrderRequest,
    OrderResult,
    Position,
    Signal,
)
from trading.circuit_breaker import CircuitBreaker
from trading.paper_trader import PaperTrader
from trading.risk_manager import RiskManager


class TradingMode(str, Enum):
    PAPER = "PAPER"
    LIVE = "LIVE"
    DUAL = "DUAL"


class OrderManager:
    """
    Top-level order orchestrator.

    Usage:
        mgr = OrderManager(
            risk_manager=risk_mgr,
            circuit_breaker=cb,
            paper_trader=paper,
            audit=audit,
            mode=TradingMode.PAPER,
        )
        result = mgr.execute_signal(signal, analysis_result)
    """

    def __init__(
        self,
        risk_manager: RiskManager,
        circuit_breaker: CircuitBreaker,
        paper_trader: PaperTrader,
        audit: AuditTrail,
        mode: TradingMode = TradingMode.PAPER,
        live_session: Any = None,
    ):
        self._risk_manager = risk_manager
        self._circuit_breaker = circuit_breaker
        self._paper_trader = paper_trader
        self._audit = audit
        self._mode = mode
        self._live_session = live_session

        # Stats
        self._orders_placed: int = 0
        self._orders_filled: int = 0
        self._orders_rejected: int = 0

        logger.info(f"Order Manager initialized: mode={mode.value}")

    def execute_signal(
        self,
        signal: Signal,
        analysis: AnalysisResult,
        sector: str = "",
    ) -> OrderResult | None:
        """
        Execute a validated signal through the full trading pipeline.

        Pipeline:
            1. Circuit breaker check
            2. Build order request from signal + analysis
            3. Risk manager evaluation (7 gates)
            4. Position sizing adjustment (circuit breaker multiplier)
            5. Order execution (paper or live)
            6. Record results

        Args:
            signal: The trading signal.
            analysis: Analyzer's validation result.
            sector: Stock's sector for risk limits.

        Returns:
            OrderResult if order was placed, None if skipped/rejected.
        """
        try:
            # Gate 1: Circuit breaker
            try:
                self._circuit_breaker.check()
            except CircuitBreakerOpenError as e:
                logger.warning(f"Order blocked by circuit breaker: {signal.symbol}")
                return None

            # Gate 2: Analysis must be actionable
            if not analysis.is_actionable:
                logger.debug(f"Signal {signal.symbol} not actionable: {analysis.action.value}")
                return None

            # Build order request
            order = self._build_order(signal, analysis)

            # Gate 3: Risk manager evaluation
            positions = self._get_current_positions()
            indicators = signal.metadata.get("precomputed_indicators")
            risk_decision = self._risk_manager.evaluate(
                order, positions, sector, indicators=indicators,
            )

            if not risk_decision.approved:
                self._orders_rejected += 1
                self._circuit_breaker.record_order_failure()
                logger.info(f"Order rejected by risk manager: {risk_decision.reason}")
                return OrderResult(
                    success=False,
                    status=OrderStatus.REJECTED,
                    message=risk_decision.reason,
                    is_paper=(self._mode != TradingMode.LIVE),
                )

            # Adjust quantity for circuit breaker state
            cb_multiplier = self._circuit_breaker.get_position_size_multiplier()
            adjusted_qty = max(1, int(risk_decision.quantity * cb_multiplier))
            order.quantity = adjusted_qty

            # Set stop loss from risk decision
            if risk_decision.stop_loss_price > 0:
                order.trigger_price = risk_decision.stop_loss_price

            # Execute order
            result = self._execute_order(order)

            # Record outcome
            if result.success:
                self._orders_placed += 1
                self._orders_filled += 1

                self._audit.log(
                    AuditEventType.ORDER_FILLED,
                    {
                        "symbol": order.symbol,
                        "side": order.transaction_type.value,
                        "quantity": result.filled_qty,
                        "fill_price": result.avg_fill_price,
                        "mode": self._mode.value,
                        "order_id": result.order_id,
                        "signal_score": signal.score,
                        "analysis_confidence": analysis.confidence,
                        "analyzer": analysis.analyzer_name,
                    },
                )
            else:
                self._orders_rejected += 1
                self._circuit_breaker.record_order_failure()

            return result

        except Exception as e:
            logger.error(f"Order execution error for {signal.symbol}: {e}")
            self._circuit_breaker.record_order_failure()
            return OrderResult(
                success=False,
                status=OrderStatus.REJECTED,
                message=f"Execution error: {e}",
                is_paper=(self._mode != TradingMode.LIVE),
            )

    def _build_order(self, signal: Signal, analysis: AnalysisResult) -> OrderRequest:
        """Build an OrderRequest from a signal and analysis result."""
        transaction_type = (
            TransactionType.BUY if signal.signal_type.value == "BUY"
            else TransactionType.SELL
        )

        return OrderRequest(
            symbol=signal.symbol,
            token=signal.token,
            transaction_type=transaction_type,
            quantity=0,  # Set by risk manager
            price=signal.price,
            product_type=ProductType.INTRADAY,  # Default to intraday
            signal_id=None,
        )

    def _execute_order(self, order: OrderRequest) -> OrderResult:
        """Execute order through the appropriate channel."""
        match self._mode:
            case TradingMode.PAPER:
                return self._paper_trader.place_order(order)

            case TradingMode.LIVE:
                return self._execute_live(order)

            case TradingMode.DUAL:
                # Execute on both, return paper result
                paper_result = self._paper_trader.place_order(order)
                try:
                    live_result = self._execute_live(order)
                    logger.info(
                        f"DUAL mode: paper={paper_result.status.value}, "
                        f"live={live_result.status.value}"
                    )
                except Exception as e:
                    logger.error(f"DUAL mode live execution failed: {e}")
                return paper_result

    def _execute_live(self, order: OrderRequest) -> OrderResult:
        """Execute order through SmartAPI (real broker)."""
        if not self._live_session:
            return OrderResult(
                success=False,
                status=OrderStatus.REJECTED,
                message="No live session — SmartAPI not connected",
            )

        try:
            api = self._live_session.get_api()

            order_params = {
                "variety": order.variety.value,
                "tradingsymbol": order.trading_symbol,
                "symboltoken": order.token,
                "transactiontype": order.transaction_type.value,
                "exchange": order.exchange.value,
                "ordertype": order.order_type.value,
                "producttype": order.product_type.value,
                "duration": "DAY",
                "quantity": str(order.quantity),
                "price": str(order.price) if order.price > 0 else "0",
                "triggerprice": str(order.trigger_price) if order.trigger_price > 0 else "0",
            }

            response = api.placeOrder(order_params)

            if response and response.get("status"):
                broker_order_id = response.get("data", {}).get("orderid", "")
                return OrderResult(
                    success=True,
                    order_id=f"LIVE-{broker_order_id}",
                    broker_order_id=broker_order_id,
                    status=OrderStatus.PLACED,
                    filled_qty=order.quantity,
                    avg_fill_price=order.price,
                    message=f"Live order placed: {broker_order_id}",
                )
            else:
                error_msg = response.get("message", "Unknown error") if response else "No response"
                return OrderResult(
                    success=False,
                    status=OrderStatus.REJECTED,
                    message=f"Broker rejected: {error_msg}",
                )

        except Exception as e:
            logger.error(f"Live order execution failed: {e}")
            return OrderResult(
                success=False,
                status=OrderStatus.REJECTED,
                message=f"Live execution error: {e}",
            )

    def _get_current_positions(self) -> list[Position]:
        """Get current positions from the active trading mode."""
        if self._mode in (TradingMode.PAPER, TradingMode.DUAL):
            return self._paper_trader.positions
        # For LIVE mode, would query broker — placeholder
        return self._paper_trader.positions

    def square_off_intraday(self) -> list[OrderResult]:
        """Square off all intraday positions (called at 3:15 PM)."""
        logger.info("🔔 Intraday square-off triggered")

        results = self._paper_trader.square_off_intraday()

        # Record P&L in circuit breaker
        for result in results:
            if result.success:
                # P&L tracking happens inside paper_trader
                pass

        self._audit.log(
            AuditEventType.SCHEDULER_TASK,
            {
                "action": "intraday_square_off",
                "mode": self._mode.value,
                "positions_closed": len(results),
            },
        )

        return results

    def update_prices(self, prices: dict[str, float]) -> None:
        """Update position prices for P&L calculation."""
        self._paper_trader.update_prices(prices)

        # Update risk manager with unrealized P&L
        daily = self._paper_trader.get_daily_summary()
        self._risk_manager.update_daily_pnl(daily.realized_pnl, daily.unrealized_pnl)

    def set_mode(self, mode: TradingMode) -> None:
        """Switch trading mode at runtime."""
        old_mode = self._mode
        self._mode = mode
        logger.info(f"Trading mode changed: {old_mode.value} → {mode.value}")

        self._audit.log(
            AuditEventType.CONFIG_CHANGE,
            {"setting": "trading_mode", "old": old_mode.value, "new": mode.value},
        )

    @property
    def mode(self) -> TradingMode:
        return self._mode

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "mode": self._mode.value,
            "orders_placed": self._orders_placed,
            "orders_filled": self._orders_filled,
            "orders_rejected": self._orders_rejected,
            "fill_rate": f"{(self._orders_filled / max(self._orders_placed, 1)) * 100:.0f}%",
            "paper_stats": self._paper_trader.stats,
            "circuit_breaker": self._circuit_breaker.stats,
        }
