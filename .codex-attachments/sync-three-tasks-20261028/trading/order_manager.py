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

            # LIVE/DUAL SELL: the paper book is not what the broker holds
            # (after a restart it is empty even when the broker is long).
            # Verify at the broker first, so the risk gates judge the exit
            # against the real holding, and reuse that answer when sizing.
            verified_held: int | None = None
            if (
                order.transaction_type == TransactionType.SELL
                and self._mode in (TradingMode.LIVE, TradingMode.DUAL)
            ):
                verified_held = self._query_verified_holding(order)
                if verified_held is None and self._mode == TradingMode.LIVE:
                    rejection = self._unverifiable_sell_rejection(order)
                    logger.warning(rejection.message)
                    self._orders_rejected += 1
                    return rejection
                if verified_held:
                    positions = positions + [
                        Position(
                            symbol=order.symbol, token=order.token, quantity=verified_held,
                            avg_buy_price=0.0, current_price=order.price,
                            product_type=order.product_type, is_paper=False,
                        )
                    ]

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
            result = self._execute_order(order, verified_held=verified_held)

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

    def _execute_order(
        self, order: OrderRequest, verified_held: int | None = None
    ) -> OrderResult:
        """Execute order through the appropriate channel."""
        match self._mode:
            case TradingMode.PAPER:
                return self._paper_trader.place_order(order)

            case TradingMode.LIVE:
                return self._execute_live(order, verified_held=verified_held)

            case TradingMode.DUAL:
                # Execute on both. The returned result describes the paper
                # leg; the live leg rides along in .live_leg so a broker
                # rejection is never hidden behind a paper fill (and a live
                # fill is never implied by one).
                paper_result = self._paper_trader.place_order(order)
                try:
                    live_result = self._execute_live(order, verified_held=verified_held)
                except Exception as e:
                    logger.error(f"DUAL mode live execution failed: {e}")
                    live_result = OrderResult(
                        success=False, status=OrderStatus.REJECTED,
                        message=f"Live execution error: {e}",
                    )
                logger.info(
                    f"DUAL mode: paper={paper_result.status.value}, "
                    f"live={live_result.status.value}"
                )
                paper_result.live_leg = live_result
                live_note = (
                    f"LIVE leg placed: {live_result.broker_order_id}"
                    if live_result.success
                    else f"LIVE leg REJECTED: {live_result.message}"
                )
                paper_result.message = f"Paper: {paper_result.message} | {live_note}"
                return paper_result

    def _verified_broker_holding(self, api: Any, order: OrderRequest) -> int | None:
        """
        Net quantity the broker reports for this instrument and product type.

        Returns None when holdings cannot be verified (API error, malformed
        response). The paper position book is deliberately NOT consulted:
        it is this process's belief, not the broker's ledger, and after a
        restart it knows nothing about positions the previous process opened.
        """
        try:
            response = api.position()
        except Exception as e:
            logger.error(f"Broker position query failed for {order.symbol}: {e}")
            return None
        if not isinstance(response, dict) or not response.get("status"):
            logger.error(f"Broker position query rejected for {order.symbol}: {response}")
            return None

        rows = response.get("data") or []  # SmartAPI returns data=None when flat
        net = 0
        for row in rows:
            try:
                same_instrument = (
                    str(row.get("symboltoken", "")) == str(order.token)
                    or row.get("tradingsymbol") == order.trading_symbol
                )
                same_product = (
                    str(row.get("producttype", "")).upper() == order.product_type.value
                )
                if same_instrument and same_product:
                    net += int(float(row.get("netqty", 0)))
            except (TypeError, ValueError) as e:
                logger.error(f"Unreadable broker position row for {order.symbol}: {row} ({e})")
                return None
        return net

    def _query_verified_holding(self, order: OrderRequest) -> int | None:
        """Verified broker holding via the live session; None if unavailable/unverifiable."""
        if not self._live_session:
            return None
        try:
            api = self._live_session.get_api()
        except Exception as e:
            logger.error(f"Broker session unavailable for {order.symbol}: {e}")
            return None
        return self._verified_broker_holding(api, order)

    @staticmethod
    def _unverifiable_sell_rejection(order: OrderRequest) -> OrderResult:
        return OrderResult(
            success=False,
            status=OrderStatus.REJECTED,
            message=(
                f"SELL {order.symbol} blocked: broker holdings could not be verified, so the "
                f"order might add (short) exposure. The paper position book is not proof of "
                f"broker holdings — check the broker position book and exit manually if needed."
            ),
        )

    def _size_live_sell_to_verified_holding(
        self, api: Any, order: OrderRequest, verified_held: int | None = None
    ) -> OrderResult | None:
        """
        LIVE/DUAL guard: a SELL may only reduce a holding verified at the
        broker. Caps the quantity to that holding; returns a rejection when
        there is no verified long to exit or holdings cannot be verified.

        Limitation: this process is long-only and cannot tell a deliberate
        short entry from an exit whose size was computed from capital
        rather than from the holding (execute_signal sizes every order from
        capital). Either would add short exposure, so both are blocked.
        """
        held = verified_held if verified_held is not None else self._verified_broker_holding(api, order)
        if held is None:
            return self._unverifiable_sell_rejection(order)
        if held <= 0:
            return OrderResult(
                success=False,
                status=OrderStatus.REJECTED,
                message=(
                    f"SELL {order.quantity} {order.symbol} blocked: no verified broker "
                    f"{order.product_type.value} holding (net {held}) — would open a short"
                ),
            )
        if order.quantity > held:
            logger.warning(
                f"SELL {order.symbol}: sizing {order.quantity} → {held} (verified broker "
                f"holding); anything beyond would flip to short"
            )
            order.quantity = held
        return None

    def _execute_live(
        self, order: OrderRequest, verified_held: int | None = None
    ) -> OrderResult:
        """Execute order through SmartAPI (real broker)."""
        if not self._live_session:
            return OrderResult(
                success=False,
                status=OrderStatus.REJECTED,
                message="No live session — SmartAPI not connected",
            )

        try:
            api = self._live_session.get_api()

            if order.transaction_type == TransactionType.SELL:
                blocked = self._size_live_sell_to_verified_holding(api, order, verified_held)
                if blocked:
                    logger.warning(blocked.message)
                    self._audit.log(
                        AuditEventType.ORDER_REJECTED,
                        {"symbol": order.symbol, "side": "SELL", "reason": blocked.message},
                    )
                    return blocked

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

        # From here until the next pre-market reset nothing may add exposure,
        # whether this square-off ran on schedule or was replayed after a
        # restart. Exits stay allowed.
        self._risk_manager.close_new_entries("intraday square-off done", key="square_off")

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
