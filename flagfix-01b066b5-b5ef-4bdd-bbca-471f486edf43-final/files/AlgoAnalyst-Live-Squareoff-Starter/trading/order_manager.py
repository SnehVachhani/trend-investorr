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

import threading
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from enum import Enum
from typing import Any

from loguru import logger

from core.constants import (
    AuditEventType,
    IST,
    OrderStatus,
    OrderType,
    OrderVariety,
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
from trading.exit_intent_store import (
    ExitIntent,
    ExitIntentStore,
    ExitIntentStoreError,
    intent_key,
    order_tag,
)
from trading.paper_trader import PaperTrader
from trading.risk_manager import RiskManager
from trading.square_off_followup import OpenPositionStatus, SquareOffConfirmation


class TradingMode(str, Enum):
    PAPER = "PAPER"
    LIVE = "LIVE"
    DUAL = "DUAL"


class ExitState(str, Enum):
    PLACED = "PLACED"            # new exit accepted by the broker — fill NOT confirmed
    OUTSTANDING = "OUTSTANDING"  # an exit is already open at the broker — nothing new sent
    REJECTED = "REJECTED"        # the broker refused the new exit — position still open
    UNCLEAR = "UNCLEAR"          # broker state cannot be reconciled — nothing sent, operator alerted


# SmartAPI order-book statuses. Anything not listed is treated as UNCLEAR.
_ORDER_OPEN_STATUSES = frozenset({
    "open", "pending", "open pending", "trigger pending", "validation pending",
    "modify pending", "cancel pending", "modified", "partially filled", "partial",
    "put order req received", "after market order req received",
})
_ORDER_TERMINAL_STATUSES = frozenset({
    "complete", "completed", "filled", "rejected", "cancelled", "canceled",
})


@dataclass(frozen=True)
class LiveExit:
    """
    Outcome for one broker INTRADAY position row with open quantity.

    The position is NOT closed in any of these states: the broker still
    shows open quantity. At best an exit is placed or already working.
    """

    trading_symbol: str
    token: str
    exchange: str
    net_qty: int          # broker's net quantity now (+long / -short)
    side: str             # "SELL" closes a long, "BUY" closes a short
    state: ExitState
    result: OrderResult | None = None   # PLACED / REJECTED: the placement result
    existing_order_id: str = ""         # OUTSTANDING: the exit already at the broker
    existing_qty: int = 0               # OUTSTANDING: its size
    filled_qty: int = 0                 # OUTSTANDING: filled so far
    open_qty: int = 0                   # OUTSTANDING: still unfilled
    detail: str = ""                    # UNCLEAR: why

    @property
    def placed(self) -> bool:
        return self.state == ExitState.PLACED

    def describe(self) -> str:
        head = f"{self.side} {abs(self.net_qty)} {self.trading_symbol}"
        if self.state == ExitState.PLACED:
            assert self.result is not None
            return f"{head} (placed {self.result.broker_order_id} — fill NOT confirmed)"
        if self.state == ExitState.REJECTED:
            assert self.result is not None
            return f"{head} (REJECTED: {self.result.message})"
        if self.state == ExitState.OUTSTANDING:
            return (
                f"{self.trading_symbol}: {abs(self.net_qty)} still open, NOT closed — exit "
                f"{self.existing_order_id} already at broker, {self.filled_qty}/{self.existing_qty} "
                f"filled, {self.open_qty} open; no new order sent"
            )
        return (
            f"{self.trading_symbol}: {abs(self.net_qty)} still open, NOT closed — broker state "
            f"unclear, no order sent: {self.detail}"
        )


class CancelState(str, Enum):
    CANCELLED = "CANCELLED"  # broker confirms cancelled/rejected — no exposure added
    FILLED = "FILLED"        # it executed before the cancel landed — exposure exited below
    UNCLEAR = "UNCLEAR"      # still open / cancel pending / cancel failed / not in the book


@dataclass(frozen=True)
class CancelledOrder:
    """An open INTRADAY order that could have added exposure, and what became of it."""

    order_id: str
    trading_symbol: str
    token: str
    side: str
    qty: int
    state: CancelState
    filled_qty: int = 0      # executed in total (partial fills before the cancel, or all of it)
    status: str = ""         # broker status after the cancel attempt
    detail: str = ""
    reason: str = ""         # why it was cancelled (entry that could add exposure / partial exit)

    def describe(self) -> str:
        head = f"{self.side} {self.qty} {self.trading_symbol} {self.order_id}"
        why = f" [{self.reason}]" if self.reason else ""
        if self.state == CancelState.CANCELLED:
            extra = f", {self.filled_qty} had filled" if self.filled_qty else ""
            return f"{head}{why} (cancelled{extra} — remaining exposure exited below)"
        if self.state == CancelState.FILLED:
            return f"{head}{why} (FILLED {self.filled_qty} before the cancel landed — remaining exposure exited below)"
        return f"{head}{why} (UNCLEAR — status '{self.status or '?'}'{': ' + self.detail if self.detail else ''})"


@dataclass
class SquareOffReport:
    """
    Outcome of the intraday square-off, with the paper and live sides kept
    apart: paper closes are simulated fills; live exits are broker
    placements whose fills are not confirmed here.
    """

    mode: TradingMode
    paper: list[OrderResult] = field(default_factory=list)   # PAPER/DUAL: one per paper position
    live: list[LiveExit] = field(default_factory=list)       # LIVE/DUAL: one per broker INTRADAY row
    live_cancelled: list[CancelledOrder] = field(default_factory=list)  # open entries cancelled first
    live_error: str | None = None        # LIVE/DUAL: broker book could not be read — nothing placed
    live_rows_left_alone: int = 0        # LIVE/DUAL: non-INTRADAY (e.g. DELIVERY) rows, untouched
    # LIVE/DUAL: the last position read showed no open INTRADAY quantity and
    # every row was readable. None until positions were read.
    broker_flat: bool | None = None

    @property
    def paper_closed(self) -> list[OrderResult]:
        return [r for r in self.paper if r.success]

    @property
    def paper_failed(self) -> list[OrderResult]:
        return [r for r in self.paper if not r.success]

    def _live_in(self, state: ExitState) -> list[LiveExit]:
        return [x for x in self.live if x.state == state]

    @property
    def live_placed(self) -> list[LiveExit]:
        return self._live_in(ExitState.PLACED)

    @property
    def live_outstanding(self) -> list[LiveExit]:
        return self._live_in(ExitState.OUTSTANDING)

    @property
    def live_rejected(self) -> list[LiveExit]:
        return self._live_in(ExitState.REJECTED)

    @property
    def live_unclear(self) -> list[LiveExit]:
        return self._live_in(ExitState.UNCLEAR)

    @property
    def cancels_unclear(self) -> list[CancelledOrder]:
        return [c for c in self.live_cancelled if c.state == CancelState.UNCLEAR]

    @property
    def cancels_filled(self) -> list[CancelledOrder]:
        return [c for c in self.live_cancelled if c.state == CancelState.FILLED]

    @property
    def live_attempted(self) -> bool:
        return self.mode in (TradingMode.LIVE, TradingMode.DUAL)

    @property
    def needs_attention(self) -> bool:
        """
        The operator must look: the broker could not be read or reconciled,
        an exit was rejected, a previously placed exit is still working
        (position not closed), or a paper close failed.
        """
        return bool(
            self.live_error or self.live_rejected or self.live_unclear
            or self.live_outstanding or self.cancels_unclear or self.paper_failed
        )


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
        exit_intents: ExitIntentStore | None = None,
    ):
        self._risk_manager = risk_manager
        self._circuit_breaker = circuit_breaker
        self._paper_trader = paper_trader
        self._audit = audit
        self._mode = mode
        self._live_session = live_session
        self._exit_intents = exit_intents

        # Stats
        self._orders_placed: int = 0
        self._orders_filled: int = 0
        self._orders_rejected: int = 0

        # Square-off is read-decide-place against the broker: two calls that
        # overlap (scheduler job + replay/operator) would each read the same
        # position and the same order book and each send an exit. One call
        # at a time in this process; the second then sees the first's order.
        self._square_off_lock = threading.Lock()

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

            # Execute order (entry gate re-checked under the shared lock)
            result = self._execute_order(order, positions, verified_held=verified_held)

            # Record outcome. Only a FILLED result is audited as a fill; a
            # live order that the broker merely accepted is PLACED and is
            # audited as such (no fill quantity or price is known yet).
            if result.success:
                self._orders_placed += 1
                signal_ctx = {
                    "symbol": order.symbol,
                    "side": order.transaction_type.value,
                    "mode": self._mode.value,
                    "order_id": result.order_id,
                    "signal_score": signal.score,
                    "analysis_confidence": analysis.confidence,
                    "analyzer": analysis.analyzer_name,
                }
                if result.status == OrderStatus.FILLED:
                    self._orders_filled += 1
                    self._audit.log(
                        AuditEventType.ORDER_FILLED,
                        {**signal_ctx, "quantity": result.filled_qty, "fill_price": result.avg_fill_price},
                    )
                else:
                    self._audit.log(
                        AuditEventType.ORDER_PLACED,
                        {
                            **signal_ctx,
                            "status": result.status.value,
                            "placed_qty": result.placed_qty,
                            "broker_order_id": result.broker_order_id,
                            "note": "broker acknowledged placement — fill not confirmed",
                        },
                    )
                # DUAL: the top-level result is the paper leg; the live leg
                # is audited on its own so the paper fill never stands in
                # for a broker fill.
                live_leg = result.live_leg
                if live_leg is not None and live_leg.success:
                    self._audit.log(
                        AuditEventType.ORDER_PLACED,
                        {
                            **signal_ctx,
                            "leg": "live",
                            "status": live_leg.status.value,
                            "placed_qty": live_leg.placed_qty,
                            "broker_order_id": live_leg.broker_order_id,
                            "note": "broker acknowledged placement — fill not confirmed",
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
        self, order: OrderRequest, positions: list[Position], verified_held: int | None = None
    ) -> OrderResult:
        """
        Execute order through the appropriate channel.

        Runs under the same lock as square-off, so a signal order and the
        square-off never decide against the broker at the same time. The
        entry gate is re-checked here, immediately before placement: an
        order approved by the risk gates a moment before 15:15 must not
        reach the broker (or the paper book) after square-off closed entries.
        """
        with self._square_off_lock:
            # The gate judges a SELL by what will actually be sent: the paper
            # trader and the live path both cap a SELL to the holding, so a
            # capital-sized quantity must not make a genuine exit look like a
            # flip to short.
            gate_order = order
            if order.transaction_type == TransactionType.SELL:
                held = sum(p.quantity for p in positions if p.symbol == order.symbol)
                if 0 < held < order.quantity:
                    gate_order = replace(order, quantity=held)
            gate = self._risk_manager.entry_gate(gate_order, positions)
            if gate is not None:
                logger.warning(f"Order blocked at placement: {gate.reason}")
                self._audit.log(
                    AuditEventType.ORDER_REJECTED,
                    {"symbol": order.symbol, "side": order.transaction_type.value,
                     "reason": gate.reason, "stage": "pre-placement entry gate"},
                )
                return OrderResult(
                    success=False, status=OrderStatus.REJECTED, message=gate.reason,
                    is_paper=(self._mode != TradingMode.LIVE),
                )
            return self._execute_order_locked(order, verified_held)

    def _execute_order_locked(
        self, order: OrderRequest, verified_held: int | None = None
    ) -> OrderResult:
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

            exit_key: str | None = None
            if order.transaction_type == TransactionType.SELL:
                blocked = self._size_live_sell_to_verified_holding(api, order, verified_held)
                if blocked is None:
                    # A live SELL is an exit of the same position square-off
                    # exits: it must see any exit already working or recorded
                    # (square-off's, or an earlier signal's) and record its own.
                    blocked, exit_key = self._guard_signal_exit(api, order)
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

            if exit_key is not None:
                order_params["ordertag"] = order_tag(datetime.now(IST).date(), order.token, "SELL")
            response = None
            try:
                response = api.placeOrder(order_params)
                result = self._live_placement_result(response, order.quantity)
            except Exception as e:
                logger.error(f"Live order execution failed: {e}")
                result = OrderResult(
                    success=False, status=OrderStatus.REJECTED, message=f"Live execution error: {e}",
                )
            if exit_key is not None:
                self._record_exit_placement(exit_key, order.symbol, result, response)
            return result

        except Exception as e:
            logger.error(f"Live order execution failed: {e}")
            return OrderResult(
                success=False,
                status=OrderStatus.REJECTED,
                message=f"Live execution error: {e}",
            )

    def _guard_signal_exit(self, api: Any, order: OrderRequest) -> tuple[OrderResult | None, str | None]:
        """
        Square-off's protections applied to a signal-path live SELL: the
        broker order book and the durable exit intents must show no exit
        already working or recorded for this instrument today, and this
        exit is recorded before it is placed.

        Returns (rejection, None) when the SELL must not be sent, or
        (None, intent_key) once the intent is claimed.
        """
        def reject(detail: str) -> tuple[OrderResult, None]:
            return OrderResult(
                success=False, status=OrderStatus.REJECTED,
                message=f"SELL {order.quantity} {order.symbol} blocked: {detail}",
            ), None

        if self._exit_intents is None:
            return reject("no exit-intent store configured — a live exit cannot be recorded durably")
        today = datetime.now(IST).date()
        orders = self._read_order_book(api, today)
        if orders is None:
            return reject("broker order book could not be read — cannot tell whether an exit is already working")
        row = {
            "tradingsymbol": order.trading_symbol, "symboltoken": order.token,
            "exchange": order.exchange.value,
        }
        existing = self._existing_exit_for(row, order.quantity, orders, api)
        if existing is not None:
            return reject(f"an exit is already working or recorded — {existing.describe()}")
        intent = ExitIntent(
            key=intent_key(today, order.exchange.value, order.token, TransactionType.SELL.value),
            day=f"{today:%Y%m%d}", exchange=order.exchange.value, token=order.token,
            side=TransactionType.SELL.value, trading_symbol=order.trading_symbol,
            quantity=order.quantity, order_tag=order_tag(today, order.token, TransactionType.SELL.value),
        )
        detail = self._claim_exit_intent(intent)
        if detail is not None:
            return reject(detail)
        return None, intent.key

    def _claim_exit_intent(self, intent: ExitIntent) -> str | None:
        """Record the intent durably; a reason string when the exit must not be sent."""
        assert self._exit_intents is not None
        try:
            claimed = self._exit_intents.claim(intent)
        except ExitIntentStoreError as e:
            detail = f"exit intent could not be recorded ({e}) — not placing an exit that could not be tracked"
            logger.error(f"{intent.trading_symbol}: {detail}")
            return detail
        if not claimed:
            detail = (
                f"another square-off call, signal or instance recorded an exit intent for "
                f"{intent.key} first — no order sent; it will be reconciled against the broker order book"
            )
            logger.warning(f"{intent.trading_symbol}: {detail}")
            return detail
        return None

    def _record_exit_placement(
        self, key: str, symbol: str, result: OrderResult, response: Any
    ) -> None:
        """After placeOrder: store the broker's ids, or release the intent on rejection."""
        assert self._exit_intents is not None
        if result.success:
            unique_id = str(((response or {}).get("data") or {}).get("uniqueorderid", ""))
            try:
                self._exit_intents.mark_placed(key, result.broker_order_id, unique_id)
            except ExitIntentStoreError as e:
                # The order is out but the record still says "intended". The
                # next check treats that as unclear until the broker lists
                # the tagged order — no duplicate, but the operator must know.
                logger.error(
                    f"{symbol}: exit {result.broker_order_id} placed but its intent record could "
                    f"not be updated ({e})"
                )
                self._audit.log(
                    AuditEventType.ERROR,
                    {"action": "exit_intent_update_failed", "key": key,
                     "broker_order_id": result.broker_order_id, "error": str(e)},
                )
        else:
            # Nothing is on its way: release the intent so a later exit may try again.
            try:
                self._exit_intents.resolve(key, f"rejected: {result.message}")
            except ExitIntentStoreError as e:
                logger.error(f"could not resolve intent {key} after rejection: {e}")

    @staticmethod
    def _live_placement_result(response: Any, quantity: int) -> OrderResult:
        """
        Turn a SmartAPI placeOrder response into an OrderResult.

        A successful response only means the broker ACCEPTED the order into
        its book. It says nothing about execution, so the result is PLACED
        with placed_qty set and filled_qty/avg_fill_price left at 0. Fill
        confirmation (order book / trade book) is a separate step.
        """
        if response and response.get("status"):
            broker_order_id = (response.get("data") or {}).get("orderid", "")
            return OrderResult(
                success=True,
                order_id=f"LIVE-{broker_order_id}",
                broker_order_id=broker_order_id,
                status=OrderStatus.PLACED,
                placed_qty=quantity,
                filled_qty=0,
                avg_fill_price=0.0,
                message=f"Live order placed: {broker_order_id} (qty {quantity}; fill not confirmed)",
            )
        error_msg = response.get("message", "Unknown error") if response else "No response"
        return OrderResult(
            success=False,
            status=OrderStatus.REJECTED,
            message=f"Broker rejected: {error_msg}",
        )

    def _get_current_positions(self) -> list[Position]:
        """Get current positions from the active trading mode."""
        if self._mode in (TradingMode.PAPER, TradingMode.DUAL):
            return self._paper_trader.positions
        # For LIVE mode, would query broker — placeholder
        return self._paper_trader.positions

    def square_off_intraday(self) -> SquareOffReport:
        """
        Square off intraday positions (called at 3:15 PM).

        PAPER: closes the paper book's INTRADAY positions (simulated fills).
        LIVE:  reads the broker's position book and sends one MARKET exit
               per INTRADAY row with a non-zero net quantity — SELL for a
               long, BUY for a short, for exactly the broker's net quantity
               and instrument. The paper book is not consulted: it is this
               process's belief, and after a restart it is empty while the
               broker may still be long. Non-INTRADAY rows (DELIVERY etc.)
               are left alone.
        DUAL:  both, reported separately.

        Before placing, the broker's order book is checked: a position that
        already has an exit working (open / partially filled) gets no second
        one, and if the book cannot be read or reconciled nothing is sent.
        Live exits are PLACED, not filled: the broker only acknowledged
        them. The report says so; nothing here confirms a fill.
        """
        logger.info(f"🔔 Intraday square-off triggered (mode={self._mode.value})")
        if not self._square_off_lock.acquire(blocking=False):
            logger.warning("Square-off already running in this process — waiting for it to finish")
            self._square_off_lock.acquire()
        try:
            return self._square_off_locked()
        finally:
            self._square_off_lock.release()

    def confirm_square_off(self) -> SquareOffConfirmation:
        """
        Read-only follow-up: does the broker show the intraday book flat?

        Never places, cancels or retries an order. Reports flat only when
        the broker's position book shows no open INTRADAY quantity and
        every row was readable; otherwise lists each open row with the
        exit working for it (status, filled/unfilled), or that none is.
        Exit intents for flat instruments are resolved.
        """
        with self._square_off_lock:
            return self._confirm_square_off_locked()

    def _confirm_square_off_locked(self) -> SquareOffConfirmation:
        now = datetime.now(IST)
        conf = SquareOffConfirmation(checked_at=now)
        if self._mode not in (TradingMode.LIVE, TradingMode.DUAL):
            conf.error = f"not a live mode ({self._mode.value}) — nothing to confirm at the broker"
            return conf
        if not self._live_session:
            conf.error = "No live session — SmartAPI not connected"
            return conf
        try:
            api = self._live_session.get_api()
        except Exception as e:
            conf.error = f"broker session unavailable: {e}"
            return conf

        scratch = SquareOffReport(mode=self._mode)
        rows = self._read_positions(api, scratch)
        if rows is None:
            conf.error = scratch.live_error
            return conf
        open_rows = self._open_intraday_rows(rows, scratch)
        conf.rows_unreadable = len(scratch.live_unclear)

        if self._exit_intents is not None:
            try:
                intents = self._exit_intents.unresolved_for_day(now.date())
                self._resolve_intents_for_flat_instruments(intents, open_rows, now.date())
            except ExitIntentStoreError as e:
                logger.error(f"Square-off follow-up: exit-intent store unreadable: {e}")

        if not open_rows and not conf.rows_unreadable:
            conf.flat = True
            return conf
        conf.flat = False

        orders = self._read_order_book(api, now.date())
        if orders is None:
            conf.error = "broker order book could not be read — exit status unknown"
        for row, net_qty in open_rows:
            side = TransactionType.SELL.value if net_qty > 0 else TransactionType.BUY.value
            trading_symbol = str(row.get("tradingsymbol", ""))
            token = str(row.get("symboltoken", ""))
            exchange = str(row.get("exchange", ""))
            if orders is None:
                conf.open_positions.append(OpenPositionStatus(
                    trading_symbol=trading_symbol, token=token, exchange=exchange, net_qty=net_qty,
                    detail="order book unreadable — exit status unknown",
                ))
                continue
            best: dict | None = None
            for order in orders:
                if str(order.get("producttype", "")).upper() != ProductType.INTRADAY.value:
                    continue
                if str(order.get("transactiontype", "")).upper() != side:
                    continue
                if self._same_instrument(order, exchange, token, trading_symbol) is not True:
                    continue
                # Prefer a working order over a finished one.
                if best is None or (
                    self._order_status(order) in _ORDER_OPEN_STATUSES
                    and self._order_status(best) not in _ORDER_OPEN_STATUSES
                ):
                    best = order
            if best is None:
                conf.open_positions.append(OpenPositionStatus(
                    trading_symbol=trading_symbol, token=token, exchange=exchange, net_qty=net_qty,
                ))
                continue
            try:
                qty, filled, unfilled = self._order_quantities(best)
            except (TypeError, ValueError):
                qty = filled = unfilled = 0
            conf.open_positions.append(OpenPositionStatus(
                trading_symbol=trading_symbol, token=token, exchange=exchange, net_qty=net_qty,
                exit_order_id=str(best.get("orderid", "")), exit_status=self._order_status(best),
                exit_qty=qty, filled=filled, unfilled=unfilled,
            ))
        return conf

    def _square_off_locked(self) -> SquareOffReport:
        report = SquareOffReport(mode=self._mode)

        if self._mode in (TradingMode.PAPER, TradingMode.DUAL):
            report.paper = self._paper_trader.square_off_intraday()

        if self._mode in (TradingMode.LIVE, TradingMode.DUAL):
            self._square_off_live(report)

        # From here until the next pre-market reset nothing may add exposure,
        # whether this square-off ran on schedule or was replayed after a
        # restart. Exits stay allowed.
        self._risk_manager.close_new_entries("intraday square-off done", key="square_off")

        self._audit.log(
            AuditEventType.SCHEDULER_TASK,
            {
                "action": "intraday_square_off",
                "mode": self._mode.value,
                "paper_positions_closed": len(report.paper_closed),
                "paper_exits_failed": len(report.paper_failed),
                "live_exits_placed_unconfirmed": len(report.live_placed),
                "live_exits_outstanding": len(report.live_outstanding),
                "live_exits_rejected": len(report.live_rejected),
                "live_exits_unclear": len(report.live_unclear),
                "live_entries_cancelled": len(report.live_cancelled),
                "live_cancels_filled_then_exited": len(report.cancels_filled),
                "live_cancels_unclear": len(report.cancels_unclear),
                "live_rows_left_alone": report.live_rows_left_alone,
                "live_error": report.live_error,
            },
        )

        return report

    def _square_off_live(self, report: SquareOffReport) -> None:
        """
        Fill report.live / live_cancelled / live_error from the broker's books.

        Order of operations — nothing is sent until the reads succeed:
            1. position book → INTRADAY rows with open quantity
            2. order book    → ALWAYS read, even when flat: an open entry
                               order could still fill after square-off
            3. cancel open INTRADAY orders that could add exposure, then
               re-read the order book to see what became of each, and
               re-read positions (a cancel can lose the race to a fill)
            4. per open row: skip if an exit is outstanding or the state is
               unclear; otherwise place one exit for the exact net quantity.
        """
        if not self._live_session:
            report.live_error = "No live session — SmartAPI not connected"
            logger.error(f"Live square-off: {report.live_error}")
            return
        try:
            api = self._live_session.get_api()
        except Exception as e:
            report.live_error = f"broker session unavailable: {e}"
            logger.error(f"Live square-off: {report.live_error}")
            return

        today = datetime.now(IST).date()

        # Exit intents are the durable record of exits this or another
        # process already decided to send today (see ExitIntentStore). No
        # store, or a store that cannot be read → no exit may be placed.
        if self._exit_intents is None:
            report.live_error = (
                "no exit-intent store configured — cannot record exits durably, so NO exit "
                "orders were placed (a restart could otherwise duplicate them)"
            )
            logger.error(f"Live square-off: {report.live_error}")
            return
        try:
            intents_today = self._exit_intents.unresolved_for_day(today)
        except ExitIntentStoreError as e:
            report.live_error = f"exit-intent store could not be read ({e}) — NO exit orders placed"
            logger.error(f"Live square-off: {report.live_error}")
            return

        rows = self._read_positions(api, report)
        if rows is None:
            return
        to_exit = self._open_intraday_rows(rows, report)
        self._resolve_intents_for_flat_instruments(intents_today, to_exit, today)

        # The order book is the only defence against a duplicate exit (a
        # second call, or a fresh process after a restart) and the only
        # place a still-pending entry shows up. If it cannot be read,
        # nothing is placed: a blind exit could double up into a short.
        orders = self._read_order_book(api, today)
        if orders is None:
            report.live_error = (
                "broker order book could not be read — cannot tell whether exits are "
                "already outstanding or entries still pending, so NO exit orders were placed"
            )
            logger.error(f"Live square-off: {report.live_error}")
            return

        to_cancel: list[tuple[dict, str]] = []
        for order in orders:
            reason = self._cancel_reason(order, to_exit)
            if reason is not None:
                to_cancel.append((order, reason))
        if to_cancel:
            for order, reason in to_cancel:
                self._request_cancel(api, order, reason)
            # What actually happened is in the books, not in the cancel acks.
            orders = self._read_order_book(api, today)
            if orders is None:
                report.live_cancelled = [
                    self._cancel_outcome(o, None, api, reason) for o, reason in to_cancel
                ]
                report.live_error = (
                    f"cancel requested for {len(to_cancel)} open order(s) but the broker order "
                    f"book could not be re-read — their state is unknown, NO exit orders placed"
                )
                logger.error(f"Live square-off: {report.live_error}")
                return
            by_id = {str(o.get("orderid", "")): o for o in orders}
            report.live_cancelled = [
                self._cancel_outcome(o, by_id.get(str(o.get("orderid", ""))), api, reason)
                for o, reason in to_cancel
            ]
            # A cancelled order may have (partly) filled first: refresh positions.
            rows = self._read_positions(api, report)
            if rows is None:
                report.live_error = (
                    f"positions could not be re-read after cancelling {len(to_cancel)} order(s) "
                    f"— {report.live_error}; NO exit orders placed"
                )
                return
            report.live_rows_left_alone = 0
            to_exit = self._open_intraday_rows(rows, report)
            self._resolve_intents_for_flat_instruments(intents_today, to_exit, today)

        report.broker_flat = not to_exit and not report.live_unclear
        if not to_exit:
            logger.info("Live square-off: broker reports no open intraday quantity")
            return

        for row, net_qty in to_exit:
            blocked = self._existing_exit_for(row, net_qty, orders, api)
            if blocked is not None:
                report.live.append(blocked)
                continue
            report.live.append(self._place_live_exit(api, row, net_qty, today))

    def _resolve_intents_for_flat_instruments(
        self, intents: list[ExitIntent], to_exit: list[tuple[dict, int]], today: date
    ) -> None:
        """An unresolved intent whose instrument shows no open quantity is finished."""
        assert self._exit_intents is not None
        open_keys = {
            intent_key(
                today, str(row.get("exchange", "")), str(row.get("symboltoken", "")),
                TransactionType.SELL.value if net > 0 else TransactionType.BUY.value,
            )
            for row, net in to_exit
        }
        for intent in intents:
            if intent.key in open_keys:
                continue
            try:
                self._exit_intents.resolve(intent.key, "broker position flat")
            except ExitIntentStoreError as e:
                logger.error(f"Live square-off: could not resolve intent {intent.key}: {e}")
                continue
            logger.info(
                f"Live square-off: exit intent {intent.key} resolved — broker position flat "
                f"(order {intent.broker_order_id or 'unknown'})"
            )
            self._audit.log(
                AuditEventType.SCHEDULER_TASK,
                {
                    "action": "intraday_square_off_intent_resolved", "key": intent.key,
                    "broker_order_id": intent.broker_order_id, "reason": "broker position flat",
                },
            )

    @staticmethod
    def _read_positions(api: Any, report: SquareOffReport) -> list[dict] | None:
        """Broker position rows ([] when flat), or None with report.live_error set."""
        try:
            response = api.position()
        except Exception as e:
            report.live_error = f"broker position query failed: {e}"
            logger.error(f"Live square-off: {report.live_error}")
            return None
        if not isinstance(response, dict) or not response.get("status"):
            report.live_error = f"broker position query rejected: {response}"
            logger.error(f"Live square-off: {report.live_error}")
            return None
        return response.get("data") or []  # SmartAPI returns data=None when flat

    @staticmethod
    def _open_intraday_rows(rows: list[dict], report: SquareOffReport) -> list[tuple[dict, int]]:
        """INTRADAY rows with non-zero net quantity; counts other rows as left alone."""
        to_exit: list[tuple[dict, int]] = []
        already_reported = {
            (x.token, x.detail) for x in report.live if x.state == ExitState.UNCLEAR and x.side == "?"
        }

        def unreadable(row: dict, detail: str) -> None:
            logger.error(f"Live square-off: {detail}")
            token = str(row.get("symboltoken", "?"))
            if (token, detail) in already_reported:
                return  # already reported on the first read
            report.live.append(LiveExit(
                trading_symbol=str(row.get("tradingsymbol", "?")), token=token,
                exchange=str(row.get("exchange") or "?"),
                net_qty=0, side="?", state=ExitState.UNCLEAR, detail=detail,
            ))

        for row in rows:
            product = str(row.get("producttype", "")).upper()
            if product != ProductType.INTRADAY.value:
                report.live_rows_left_alone += 1
                logger.info(
                    f"Live square-off: leaving {product or '?'} row alone: "
                    f"{row.get('tradingsymbol')} net {row.get('netqty')}"
                )
                continue
            try:
                net_qty = int(float(row.get("netqty", 0)))
            except (TypeError, ValueError) as e:
                unreadable(row, f"unreadable netqty in broker position row {row} ({e})")
                continue
            if net_qty == 0:
                continue  # already flat (e.g. exited earlier today)
            if not str(row.get("exchange") or "").strip():
                # An exit needs the exchange; defaulting it would be a guess.
                unreadable(row, f"position row has no exchange — cannot place an exit without guessing: {row}")
                continue
            to_exit.append((row, net_qty))
        return to_exit

    @staticmethod
    def _same_instrument(order: dict, exchange: str, token: str, trading_symbol: str) -> bool | None:
        """
        Is this order-book row for the instrument (exchange, token/symbol)?

        True/False when the order's exchange is known. None when the token
        or symbol matches but the order carries no exchange: tokens are
        exchange-specific, so the match cannot be verified.
        """
        same_id = (
            str(order.get("symboltoken", "")) == token
            or (bool(trading_symbol) and order.get("tradingsymbol") == trading_symbol)
        )
        if not same_id:
            return False
        order_exchange = str(order.get("exchange") or "").strip().upper()
        if not order_exchange:
            return None
        return order_exchange == exchange.strip().upper()

    @staticmethod
    def _order_status(order: dict) -> str:
        return str(order.get("status") or order.get("orderstatus") or "").strip().lower()

    @staticmethod
    def _order_quantities(order: dict) -> tuple[int, int, int]:
        """(quantity, filled, unfilled) from an order-book row; raises on garbage."""
        qty = int(float(order.get("quantity", 0)))
        filled = int(float(order.get("filledshares", 0)))
        unfilled_raw = order.get("unfilledshares")
        unfilled = int(float(unfilled_raw)) if unfilled_raw not in (None, "") else qty - filled
        return qty, filled, unfilled

    def _cancel_reason(self, order: dict, to_exit: list[tuple[dict, int]]) -> str | None:
        """
        Why an open INTRADAY order must be cancelled before exits are placed,
        or None if it may stay.

        Cancelled:
          - an entry (same direction as the position, or no position): it
            would add exposure if it filled;
          - an exit LARGER than the position: its excess would flip it;
          - an exit SMALLER than the position (e.g. a signal SELL 176 of a
            500 long): neither stacking a full exit on top of it nor leaving
            the rest exposed is acceptable, so it is cancelled and, once its
            final status is confirmed, replaced by one exit for what is
            still held.
        Left alone: an exit that covers exactly the open quantity.

        Positions are matched by exchange + token/symbol. An order whose
        exchange cannot be verified is cancelled only if it is an entry for
        every position it might belong to; if it might be a position's
        exit it is left alone (the exit check reports that position as
        UNCLEAR rather than guessing).
        """
        if str(order.get("producttype", "")).upper() != ProductType.INTRADAY.value:
            return None
        if self._order_status(order) not in _ORDER_OPEN_STATUSES:
            return None
        side = str(order.get("transactiontype", "")).upper()
        if side not in (TransactionType.BUY.value, TransactionType.SELL.value):
            return None

        def exit_side_of(net: int) -> bool:
            return (net > 0) == (side == TransactionType.SELL.value)

        net = 0
        for row, row_net in to_exit:
            match = self._same_instrument(
                order, str(row.get("exchange", "")), str(row.get("symboltoken", "")),
                str(row.get("tradingsymbol", "")),
            )
            if match is None and exit_side_of(row_net):
                return None  # might be this position's exit — do not cancel on a guess
            if match:
                net = row_net
        if net == 0:
            return "pending entry with no position — could add exposure"
        if not exit_side_of(net):
            return f"pending entry on a {'long' if net > 0 else 'short'} of {abs(net)} — could add exposure"
        try:
            _qty, _filled, unfilled = self._order_quantities(order)
        except (TypeError, ValueError):
            return "exit of unreadable size — cannot be trusted to match the position"
        if unfilled > abs(net):
            return f"exit of {unfilled} exceeds the {abs(net)} held — excess would flip the position"
        if unfilled < abs(net):
            return (
                f"working exit covers only {unfilled} of the {abs(net)} held — replaced by one "
                f"exit for the full remaining quantity"
            )
        return None

    def _request_cancel(self, api: Any, order: dict, reason: str) -> None:
        order_id = str(order.get("orderid", ""))
        variety = str(order.get("variety") or OrderVariety.NORMAL.value)
        logger.warning(
            f"Live square-off: cancelling open {order.get('transactiontype')} "
            f"{order.get('quantity')} {order.get('tradingsymbol')} ({order_id}) — {reason}"
        )
        try:
            response = api.cancelOrder(order_id, variety)
        except Exception as e:
            logger.error(f"Live square-off: cancel request for {order_id} failed: {e}")
            return
        if not isinstance(response, dict) or not response.get("status"):
            logger.error(f"Live square-off: cancel request for {order_id} rejected: {response}")

    def _cancel_outcome(
        self, original: dict, current: dict | None, api: Any = None, reason: str = ""
    ) -> CancelledOrder:
        """
        Classify a cancelled order from its order-book row after the attempt.
        If the resolved book no longer lists it (e.g. an older row that
        became terminal), its current status is looked up directly.
        """
        order_id = str(original.get("orderid", ""))
        base = dict(
            order_id=order_id,
            trading_symbol=str(original.get("tradingsymbol", "")),
            token=str(original.get("symboltoken", "")),
            side=str(original.get("transactiontype", "")).upper(),
            reason=reason,
        )
        try:
            qty, _f, _u = self._order_quantities(original)
        except (TypeError, ValueError):
            qty = 0
        if current is None and api is not None:
            current = self._verify_order_status(api, original)
        if current is None:
            outcome = CancelledOrder(
                **base, qty=qty, state=CancelState.UNCLEAR,
                detail="not found in the broker order book after the cancel request and its "
                       "status could not be verified",
            )
        else:
            status = self._order_status(current)
            try:
                qty, filled, _unfilled = self._order_quantities(current)
            except (TypeError, ValueError) as e:
                filled = 0
                outcome = CancelledOrder(
                    **base, qty=qty, state=CancelState.UNCLEAR, status=status,
                    detail=f"unreadable order book entry ({e})",
                )
            else:
                if status in ("cancelled", "canceled", "rejected"):
                    outcome = CancelledOrder(
                        **base, qty=qty, state=CancelState.CANCELLED, filled_qty=filled, status=status,
                    )
                elif status in ("complete", "completed", "filled"):
                    outcome = CancelledOrder(
                        **base, qty=qty, state=CancelState.FILLED, filled_qty=filled, status=status,
                    )
                else:
                    outcome = CancelledOrder(
                        **base, qty=qty, state=CancelState.UNCLEAR, filled_qty=filled, status=status,
                        detail="still open at the broker — could fill after square-off",
                    )

        details = {
            "action": "intraday_square_off_cancel_entry",
            "broker_order_id": outcome.order_id, "tradingsymbol": outcome.trading_symbol,
            "token": outcome.token, "side": outcome.side, "quantity": outcome.qty,
            "outcome": outcome.state.value, "broker_status": outcome.status,
            "filled_qty": outcome.filled_qty,
        }
        if outcome.state == CancelState.CANCELLED:
            logger.info(f"Live square-off: {outcome.describe()}")
            self._audit.log(AuditEventType.ORDER_CANCELLED, details)
        else:
            logger.error(f"Live square-off: {outcome.describe()}")
            self._audit.log(AuditEventType.SCHEDULER_TASK, details)
        return outcome

    # Timestamp formats seen on SmartAPI order-book rows (updatetime / exchtime).
    _ORDER_TIME_FIELDS = ("updatetime", "exchtime", "exchorderupdate")
    _ORDER_TIME_FORMATS = ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S")

    @classmethod
    def _order_day(cls, order: dict) -> date | None:
        """The trading day an order-book row belongs to, if it carries a readable timestamp."""
        for field_name in cls._ORDER_TIME_FIELDS:
            raw = str(order.get(field_name) or "").strip()
            if not raw:
                continue
            for fmt in cls._ORDER_TIME_FORMATS:
                try:
                    return datetime.strptime(raw, fmt).date()
                except ValueError:
                    continue
        return None

    # Marker set on an order-book row dated before today whose current status
    # could not be verified with the broker. Such a row can neither be trusted
    # nor ignored: it blocks as UNCLEAR.
    _STALE_UNVERIFIED = "_stale_unverified"

    @classmethod
    def _read_order_book(cls, api: Any, today: date) -> list[dict] | None:
        """
        The broker's order book, resolved to what matters today, or None if
        it cannot be read.

        SmartAPI documents the TRADE book as the current day's trades; the
        ORDER book carries no such guarantee, so rows may be older. A row
        whose timestamp is from an earlier day is handled by status:
            terminal (complete/cancelled/rejected) → dropped: neither
                today's outstanding exit nor reconciled against today's
                position;
            open → it might still execute, so its timestamp alone is no
                reason to ignore it. Its CURRENT status is verified with
                the broker (individual order details): terminal → dropped;
                still open → kept as a live order; unverifiable → kept
                and marked, which makes the matching position UNCLEAR.
        Rows without a readable timestamp are treated as today's.
        """
        try:
            response = api.orderBook()
        except Exception as e:
            logger.error(f"Live square-off: order book query failed: {e}")
            return None
        if not isinstance(response, dict) or not response.get("status"):
            logger.error(f"Live square-off: order book query rejected: {response}")
            return None
        data = response.get("data")
        if data is None:
            return []  # SmartAPI returns data=None when there are no orders today
        if not isinstance(data, list):
            logger.error(f"Live square-off: malformed order book: {response}")
            return None

        resolved: list[dict] = []
        for order in data:
            order_day = cls._order_day(order)
            if order_day is None or order_day >= today:
                resolved.append(order)
                continue
            label = (
                f"order {order.get('orderid')} dated {order_day} ({order.get('transactiontype')} "
                f"{order.get('quantity')} {order.get('tradingsymbol')}, book status '{cls._order_status(order)}')"
            )
            if cls._order_status(order) in _ORDER_TERMINAL_STATUSES:
                logger.info(f"Live square-off: ignoring {label} — terminal, not today's")
                continue
            current = cls._verify_order_status(api, order)
            if current is None:
                logger.error(
                    f"Live square-off: {label} may still execute and its current status could "
                    f"NOT be verified — treating its instrument as unclear"
                )
                resolved.append({**order, cls._STALE_UNVERIFIED: True})
                continue
            verified_status = cls._order_status(current)
            if verified_status in _ORDER_TERMINAL_STATUSES:
                logger.info(f"Live square-off: ignoring {label} — broker confirms '{verified_status}'")
                continue
            logger.warning(
                f"Live square-off: {label} — broker confirms it is still '{verified_status}'; "
                f"treating it as a live order"
            )
            resolved.append({**order, **current})
        return resolved

    @staticmethod
    def _verify_order_status(api: Any, order: dict) -> dict | None:
        """
        The broker's current view of one order (SmartAPI individual order
        details, by uniqueorderid), or None if it cannot be obtained.
        """
        unique_id = str(order.get("uniqueorderid") or "").strip()
        if not unique_id:
            logger.error(f"Live square-off: order {order.get('orderid')} has no uniqueorderid to verify by")
            return None
        lookup = getattr(api, "individual_order_details", None)
        if lookup is None:
            logger.error("Live square-off: broker API has no individual_order_details — cannot verify")
            return None
        try:
            response = lookup(unique_id)
        except Exception as e:
            logger.error(f"Live square-off: status lookup for order {order.get('orderid')} failed: {e}")
            return None
        if not isinstance(response, dict) or not response.get("status"):
            logger.error(f"Live square-off: status lookup for order {order.get('orderid')} rejected: {response}")
            return None
        current = response.get("data")
        if not isinstance(current, dict) or not OrderManager._order_status(current):
            logger.error(f"Live square-off: status lookup for order {order.get('orderid')} unreadable: {response}")
            return None
        return current

    def _existing_exit_for(
        self, row: dict, net_qty: int, orders: list[dict], api: Any = None
    ) -> LiveExit | None:
        """
        Decide whether an exit for this position row may be placed.

        Returns an OUTSTANDING or UNCLEAR LiveExit when it must NOT be
        placed, or None when a fresh exit is safe. Only orders for the
        same instrument, INTRADAY product and exit side are considered.
        """
        side = TransactionType.SELL.value if net_qty > 0 else TransactionType.BUY.value
        trading_symbol = str(row.get("tradingsymbol", ""))
        token = str(row.get("symboltoken", ""))
        exchange = str(row.get("exchange", ""))

        def unclear(detail: str) -> LiveExit:
            logger.error(f"Live square-off: {trading_symbol}: {detail} — no order sent")
            return LiveExit(
                trading_symbol=trading_symbol, token=token, exchange=exchange,
                net_qty=net_qty, side=side, state=ExitState.UNCLEAR, detail=detail,
            )

        same_exit: list[dict] = []
        for order in orders:
            if str(order.get("producttype", "")).upper() != ProductType.INTRADAY.value:
                continue
            if str(order.get("transactiontype", "")).upper() != side:
                continue
            match = self._same_instrument(order, exchange, token, trading_symbol)
            if match is False:
                continue
            if order.get(self._STALE_UNVERIFIED):
                return unclear(
                    f"order {order.get('orderid', '?')} ({side} {order.get('quantity')}) is dated "
                    f"{self._order_day(order)} and still marked '{self._order_status(order)}' but its "
                    f"current status could not be verified — it may still execute"
                )
            if match is None:
                order_id = str(order.get("orderid", "?"))
                status = self._order_status(order)
                try:
                    _qty, filled, _unfilled = self._order_quantities(order)
                except (TypeError, ValueError):
                    filled = 1  # unreadable — cannot rule it out
                if status in _ORDER_OPEN_STATUSES or filled > 0 or status not in _ORDER_TERMINAL_STATUSES:
                    return unclear(
                        f"order {order_id} ({side} {order.get('quantity')}, status '{status}') matches "
                        f"by token/symbol but its exchange cannot be verified — it may or may not be "
                        f"this position's exit"
                    )
                continue  # terminal with nothing filled on an unverifiable exchange: irrelevant
            same_exit.append(order)

        # An unresolved exit intent (this process, a previous one, or another
        # instance) means an exit may already be on its way. It must be
        # accounted for in the order book before anything new is sent.
        assert self._exit_intents is not None
        key = intent_key(datetime.now(IST).date(), exchange, token, side)
        try:
            intent = self._exit_intents.get(key)
        except ExitIntentStoreError as e:
            return unclear(f"exit-intent record could not be read ({e})")
        listed_ids = {str(o.get("orderid", "")) for o in same_exit}
        if intent is not None and not intent.acknowledged:
            # Recorded, then the process died before the broker's ack was
            # stored: the order may or may not exist. Adopt it if the broker
            # lists an order carrying the intent's tag; otherwise leave it
            # to the operator.
            tagged = [o for o in same_exit if str(o.get("ordertag", "")) == intent.order_tag]
            if tagged:
                adopted_id = str(tagged[0].get("orderid", ""))
                try:
                    intent = self._exit_intents.mark_placed(
                        key, adopted_id, str(tagged[0].get("uniqueorderid", "")),
                    )
                except ExitIntentStoreError as e:
                    return unclear(f"exit-intent record could not be updated ({e})")
                logger.warning(
                    f"Live square-off: {trading_symbol}: adopted broker order {adopted_id} "
                    f"(tag {intent.order_tag}) for intent recorded at {intent.created_at} by {intent.owner}"
                )
            else:
                return unclear(
                    f"an exit intent was recorded at {intent.created_at} by {intent.owner} without a "
                    f"broker order id (process stopped before the broker's ack was stored) and the "
                    f"broker lists no order tagged {intent.order_tag} — the order may or may not have "
                    f"reached the broker; check the broker, then remove "
                    f"{self._exit_intents.path_for(key)} if no such order exists"
                )
        if intent is not None and intent.broker_order_id not in listed_ids:
            current = self._verify_order_status(api, {
                "orderid": intent.broker_order_id, "uniqueorderid": intent.unique_order_id,
            }) if api is not None else None
            if current is None:
                return unclear(
                    f"exit {intent.broker_order_id} (recorded at {intent.placed_at or intent.created_at} "
                    f"by {intent.owner}) is not in the broker order book yet and its status could not "
                    f"be verified"
                )
            same_exit.append({**current, "orderid": intent.broker_order_id})
            logger.info(
                f"Live square-off: {trading_symbol}: exit {intent.broker_order_id} not in the order "
                f"book; broker reports status '{self._order_status(current)}'"
            )

        filled_terminal = 0
        for order in same_exit:
            status = str(order.get("status") or order.get("orderstatus") or "").strip().lower()
            order_id = str(order.get("orderid", "?"))
            try:
                qty = int(float(order.get("quantity", 0)))
                filled = int(float(order.get("filledshares", 0)))
                unfilled_raw = order.get("unfilledshares")
                unfilled = int(float(unfilled_raw)) if unfilled_raw not in (None, "") else qty - filled
            except (TypeError, ValueError) as e:
                return unclear(f"unreadable order book entry {order} ({e})")

            if status in _ORDER_OPEN_STATUSES:
                if unfilled < abs(net_qty):
                    # Cancelling it was attempted (or was not possible); it is
                    # still open, so the rest of the position cannot be exited
                    # without risking a double exit. No speculative order.
                    return unclear(
                        f"working exit {order_id} ({status}) covers only {unfilled} of the {abs(net_qty)} "
                        f"held and its cancellation could not be confirmed — {abs(net_qty) - unfilled} "
                        f"remain exposed; cancel it at the broker, then exit what is held"
                    )
                logger.warning(
                    f"Live square-off: {trading_symbol}: exit {order_id} already at broker "
                    f"({status}, {filled}/{qty} filled) — not sending another"
                )
                self._audit.log(
                    AuditEventType.SCHEDULER_TASK,
                    {
                        "action": "intraday_square_off_exit_skipped",
                        "reason": "exit already outstanding at broker",
                        "tradingsymbol": trading_symbol, "token": token, "side": side,
                        "broker_net_qty": net_qty, "existing_order_id": order_id,
                        "existing_status": status, "filled_qty": filled, "open_qty": unfilled,
                    },
                )
                return LiveExit(
                    trading_symbol=trading_symbol, token=token, exchange=exchange,
                    net_qty=net_qty, side=side, state=ExitState.OUTSTANDING,
                    existing_order_id=order_id, existing_qty=qty,
                    filled_qty=filled, open_qty=unfilled,
                )
            if status in _ORDER_TERMINAL_STATUSES:
                filled_terminal += filled
                if intent is not None and order_id == intent.broker_order_id:
                    try:
                        self._exit_intents.resolve(key, f"broker order {order_id} {status}")
                    except ExitIntentStoreError as e:
                        return unclear(f"exit-intent record could not be resolved ({e})")
                    logger.info(f"Live square-off: exit intent {key} resolved — order {order_id} {status}")
                    intent = None
                continue
            return unclear(f"exit {order_id} has unrecognised status '{status}'")

        # Completed/cancelled exits today have filled `filled_terminal`. The
        # position row must already reflect those fills (its sellqty for a
        # long, buyqty for a short); if it does not, the position book is
        # lagging and another exit would be a guess.
        if filled_terminal:
            reflected_key = "sellqty" if net_qty > 0 else "buyqty"
            try:
                reflected = int(float(row.get(reflected_key)))
            except (TypeError, ValueError):
                return unclear(
                    f"{filled_terminal} already filled by earlier exit(s) but the position row "
                    f"has no readable {reflected_key} to confirm it is reflected"
                )
            if reflected < filled_terminal:
                ids = ", ".join(str(o.get("orderid", "?")) for o in same_exit)
                return unclear(
                    f"exit(s) {ids} show {filled_terminal} filled but the position row shows "
                    f"{reflected_key}={reflected} — position book has not caught up"
                )
        return None

    def _place_live_exit(self, api: Any, row: dict, net_qty: int, today: date) -> LiveExit:
        """
        Send one MARKET exit for a broker INTRADAY row, using the broker's
        own instrument details and exact net quantity. Long → SELL, short → BUY.

        The exit intent is recorded durably BEFORE placeOrder; the broker's
        ids are stored after the ack. A claim that fails because the record
        already exists means another call or instance got there first.
        """
        side = TransactionType.SELL.value if net_qty > 0 else TransactionType.BUY.value
        trading_symbol = str(row.get("tradingsymbol", ""))
        token = str(row.get("symboltoken", ""))
        exchange = str(row.get("exchange", ""))  # rows without one never reach here
        quantity = abs(net_qty)

        key = intent_key(today, exchange, token, side)
        tag = order_tag(today, token, side)
        intent = ExitIntent(
            key=key, day=f"{today:%Y%m%d}", exchange=exchange.upper(), token=token, side=side,
            trading_symbol=trading_symbol, quantity=quantity, order_tag=tag,
        )
        detail = self._claim_exit_intent(intent)
        if detail is not None:
            return LiveExit(
                trading_symbol=trading_symbol, token=token, exchange=exchange,
                net_qty=net_qty, side=side, state=ExitState.UNCLEAR, detail=detail,
            )

        params = {
            "variety": OrderVariety.NORMAL.value,
            "tradingsymbol": trading_symbol,
            "symboltoken": token,
            "transactiontype": side,
            "exchange": exchange,
            "ordertype": OrderType.MARKET.value,
            "producttype": ProductType.INTRADAY.value,
            "duration": "DAY",
            "quantity": str(quantity),
            "price": "0",
            "triggerprice": "0",
            "ordertag": tag,
        }
        response = None
        try:
            response = api.placeOrder(params)
            result = self._live_placement_result(response, quantity)
        except Exception as e:
            result = OrderResult(
                success=False, status=OrderStatus.REJECTED, message=f"Live execution error: {e}",
            )

        exit_ = LiveExit(
            trading_symbol=trading_symbol, token=token, exchange=exchange,
            net_qty=net_qty, side=side,
            state=ExitState.PLACED if result.success else ExitState.REJECTED,
            result=result,
        )
        self._record_exit_placement(key, trading_symbol, result, response)
        if exit_.placed:
            logger.info(f"Live square-off: {exit_.describe()}")
            self._audit.log(
                AuditEventType.ORDER_PLACED,
                {
                    "action": "intraday_square_off_exit",
                    "tradingsymbol": trading_symbol, "token": token, "exchange": exchange,
                    "side": side, "placed_qty": quantity, "broker_net_qty": net_qty,
                    "broker_order_id": result.broker_order_id, "status": result.status.value,
                    "note": "broker acknowledged placement — fill not confirmed",
                },
            )
        else:
            logger.error(f"Live square-off: {exit_.describe()}")
            self._audit.log(
                AuditEventType.ORDER_REJECTED,
                {
                    "action": "intraday_square_off_exit",
                    "tradingsymbol": trading_symbol, "token": token, "exchange": exchange,
                    "side": side, "quantity": quantity, "broker_net_qty": net_qty,
                    "reason": result.message,
                },
            )
        return exit_

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
