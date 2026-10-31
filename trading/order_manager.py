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
from contextlib import AbstractContextManager, nullcontext
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
from trading.entry_reservation_store import (
    EntryHold,
    EntryReservation,
    EntryReservationStore,
    EntryReservationStoreError,
    entry_order_tag,
    reservation_key,
)
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
    UNKNOWN = "UNKNOWN"          # exit SUBMITTED but the broker's answer was lost — may be working


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
        if self.state == ExitState.UNKNOWN:
            return (
                f"{head} (SUBMITTED — outcome UNKNOWN: {self.detail}; it may be working at the "
                f"broker, so it was not resent; its exit intent is kept until the broker shows it)"
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


@dataclass(frozen=True)
class UnreconciledEntry:
    """
    An entry reservation whose submission the broker's books did not show
    when square-off looked: it may still appear (and fill) afterwards.
    Nothing here is settled; the reservation is kept and reported until
    the broker shows the order terminal or someone checks.
    """

    trading_symbol: str
    token: str
    exchange: str
    quantity: int
    reservation_key: str
    status: str            # reserved / placed / unclear
    broker_order_id: str   # "" when the acknowledgement was never stored
    owner: str
    detail: str
    cancel_requested: bool = False

    def describe(self) -> str:
        ident = f"order {self.broker_order_id}" if self.broker_order_id else "no broker order id stored"
        cancel = "; cancel requested by id, outcome unknown" if self.cancel_requested else ""
        return (
            f"BUY {self.quantity} {self.trading_symbol} submitted by {self.owner} ({ident}, reservation "
            f"'{self.status}') — {self.detail}{cancel}; NOT settled, may still appear or fill"
        )


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
    # every row was readable. None until positions were read. A flat
    # snapshot is NOT settled while entries are unreconciled (see `settled`).
    broker_flat: bool | None = None
    # LIVE/DUAL: square-off could not obtain the cross-process entry lock
    # and ran without it — another instance may have been mid-decision.
    live_lock_skipped: str | None = None
    # LIVE/DUAL: entry reservations the broker's books did not show when
    # square-off looked — submissions that may still appear or fill.
    live_unreconciled: list[UnreconciledEntry] = field(default_factory=list)

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
    def live_unknown(self) -> list[LiveExit]:
        """Exits that WERE submitted but whose outcome the broker never confirmed."""
        return self._live_in(ExitState.UNKNOWN)

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
    def settled(self) -> bool:
        """
        The broker showed a flat intraday book AND nothing is in flight: no
        unreconciled entry submission, and the entry lock was held (no
        other instance could have been mid-decision). A flat snapshot
        alone is not settlement.
        """
        return bool(self.broker_flat) and not self.live_unreconciled and not self.live_lock_skipped

    @property
    def needs_attention(self) -> bool:
        """
        The operator must look: the broker could not be read or reconciled,
        an exit was rejected or its outcome is unknown, a previously placed
        exit is still working (position not closed), a paper close failed,
        an entry submission is unreconciled, or square-off ran without the
        entry lock.
        """
        return bool(
            self.live_error or self.live_rejected or self.live_unclear or self.live_unknown
            or self.live_outstanding or self.cancels_unclear or self.paper_failed
            or self.live_unreconciled or self.live_lock_skipped
        )


class BrokerBookUnverifiable(Exception):
    """The broker's books could not be read or reconciled; the reason is the message."""


@dataclass(frozen=True)
class BrokerBook:
    """
    What the broker shows for a signal order's product type, read from the
    position book and (for a BUY) the order book.

    positions: one row per instrument for the risk gates — quantity held
        plus any BUY quantity still working for it, valued together. The
        filled part of a working order is already in the position book, so
        only its UNFILLED remainder is added: nothing is counted twice.
    held: net quantity actually held in the order's own instrument
        (position book only — a working BUY is not a holding).
    pending_entry_qty: unfilled BUY quantity working for the order's own
        instrument.
    pending_entry_ids: the broker order ids behind pending_entry_qty.
    """

    positions: list[Position]
    held: int
    pending_entry_qty: int = 0
    pending_entry_ids: tuple[str, ...] = ()
    reserved_qty: int = 0     # unresolved entry reservation the books do not show (counted in positions)
    reserved_note: str = ""   # … and why it counts


@dataclass
class _Leg:
    """
    One side of a signal order, judged and placed against its own book.

    PAPER and LIVE have one leg; DUAL has a paper leg (paper book) and a
    live leg (broker's verified position book), decided independently.
    """

    name: str                           # "paper" | "live"
    positions: list[Position] | None    # the book it is judged against; None = unverifiable
    order: OrderRequest                 # this leg's own copy: sized here, not shared
    result: OrderResult | None = None   # rejection before placement, or the placement result

    @property
    def is_paper(self) -> bool:
        return self.name == "paper"

    @property
    def approved(self) -> bool:
        return self.result is None


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
        entry_reservations: EntryReservationStore | None = None,
    ):
        self._risk_manager = risk_manager
        self._circuit_breaker = circuit_breaker
        self._paper_trader = paper_trader
        self._audit = audit
        self._mode = mode
        self._live_session = live_session
        self._exit_intents = exit_intents
        # Durable record of live BUYs submitted today (see
        # EntryReservationStore). Without it no live BUY is submitted: an
        # acknowledged-but-unlisted order, or one whose reply was lost,
        # could otherwise be sent again by this or a fresh process.
        self._entry_reservations = entry_reservations

        # Stats. "rejected" is a CONFIRMED non-placement (risk gate, guard,
        # or the broker's explicit rejection). "unknown" is a submission
        # whose outcome the broker never confirmed — it may be working —
        # and is never counted as rejected.
        self._orders_placed: int = 0
        self._orders_filled: int = 0
        self._orders_rejected: int = 0
        self._orders_unknown: int = 0

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
            3. Per leg (paper and/or live), against that leg's own book:
               risk manager evaluation (7 gates) and position sizing
               (circuit breaker multiplier)
            4. Per approved leg: entry gate re-check and placement
            5. Record each leg's result; DUAL returns the paper result
               with the live result in .live_leg

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
            indicators = signal.metadata.get("precomputed_indicators")

            # Gate 3: Risk manager evaluation — one leg per book.
            #
            # The paper book is this process's belief, not the broker's
            # ledger: after a restart it is empty even when the broker is
            # long. So each leg is judged, sized and placed against the
            # book it actually trades on:
            #
            #   PAPER: one leg, the paper book.
            #   LIVE:  one leg, the broker's verified position book. If it
            #          cannot be verified nothing is judged — an unreadable
            #          book is not an empty one.
            #   DUAL:  both legs, independently. A paper rejection does not
            #          stop a live leg the broker's book approves, and a
            #          live rejection does not hide or undo a valid paper
            #          result. The broker's book is read once here and
            #          reused at placement.
            # A live BUY decision — read the broker's books, check, size,
            # reserve — is one operation across processes: the entry lock is
            # held from here until the reservation is written (and, for
            # simplicity, until the order is placed).
            entry_lock, lock_error = self._entry_lock(order)
            with entry_lock:
                broker_book: BrokerBook | None = None
                unverifiable: str | None = None
                blocked: OrderResult | None = None
                if self._mode in (TradingMode.LIVE, TradingMode.DUAL):
                    if lock_error:
                        unverifiable = lock_error
                    elif order.transaction_type == TransactionType.BUY:
                        # Another instance's square-off (now or earlier today)
                        # closes entries for this one too.
                        hold, hold_error = self._shared_entry_hold()
                        if hold_error:
                            unverifiable = f"shared entry hold could not be read ({hold_error})"
                        elif hold is not None:
                            blocked = self._entries_closed_rejection(order, hold)
                    if unverifiable is None and blocked is None:
                        broker_book, unverifiable = self._query_broker_book(order)

                legs: list[_Leg] = []
                if self._mode in (TradingMode.PAPER, TradingMode.DUAL):
                    legs.append(self._prepare_leg(
                        "paper", order, self._paper_trader.positions, sector, indicators,
                    ))
                if self._mode in (TradingMode.LIVE, TradingMode.DUAL):
                    legs.append(self._prepare_leg(
                        "live", order, broker_book.positions if broker_book else None,
                        sector, indicators, unverifiable=unverifiable, blocked=blocked,
                        note=self._pending_entry_note(broker_book, order),
                    ))

                # Execute every approved leg (entry gate re-checked under the shared lock)
                self._execute_legs(legs, broker_book)

            for leg in legs:
                self._record_leg(leg, signal, analysis)
            return self._combine_legs(legs)

        except Exception as e:
            logger.error(f"Order execution error for {signal.symbol}: {e}")
            self._circuit_breaker.record_order_failure()
            return OrderResult(
                success=False,
                status=OrderStatus.REJECTED,
                message=f"Execution error: {e}",
                is_paper=(self._mode != TradingMode.LIVE),
            )

    def _entry_lock(self, order: OrderRequest) -> tuple[AbstractContextManager, str | None]:
        """
        The cross-process entry-decision lock for a live BUY, acquired now:
        (lock, None). When it cannot be obtained: (no-op, reason) — the live
        leg is then refused for that reason. Not needed for SELLs (judged
        against the broker's holding, not a shared budget) or PAPER.
        """
        if (
            order.transaction_type != TransactionType.BUY
            or self._mode == TradingMode.PAPER
            or self._entry_reservations is None
        ):
            return nullcontext(), None
        try:
            return self._entry_reservations.lock(), None
        except EntryReservationStoreError as e:
            logger.error(f"{order.symbol}: {e}")
            return nullcontext(), f"entry-reservation lock could not be obtained ({e}) — not submitting"

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

    def _prepare_leg(
        self,
        name: str,
        order: OrderRequest,
        positions: list[Position] | None,
        sector: str,
        indicators: Any,
        unverifiable: str | None = None,
        blocked: OrderResult | None = None,
        note: str = "",
    ) -> _Leg:
        """
        Judge and size one leg against its own book. The leg comes back
        either approved (its own sized copy of the order, nothing placed
        yet) or rejected (result set, nothing will be placed). `note` is
        appended to a risk rejection to say what in the book caused it
        (e.g. a BUY still working at the broker).
        """
        leg = _Leg(name=name, positions=positions, order=replace(order))
        if blocked is not None:
            leg.result = blocked
            logger.warning(f"{name} leg: {blocked.message}")
            return leg
        if positions is None:
            # Only the live leg can have no verifiable book.
            leg.result = self._unverifiable_broker_book_rejection(order, unverifiable or "")
            logger.warning(f"{name} leg: {leg.result.message}")
            return leg

        decision = self._risk_manager.evaluate(order, positions, sector, indicators=indicators)
        if not decision.approved:
            logger.info(f"{name} leg rejected by risk manager: {decision.reason}{note}")
            leg.result = OrderResult(
                success=False, status=OrderStatus.REJECTED, message=decision.reason + note,
                is_paper=leg.is_paper,
            )
            return leg

        # Adjust quantity for circuit breaker state
        cb_multiplier = self._circuit_breaker.get_position_size_multiplier()
        leg.order.quantity = max(1, int(decision.quantity * cb_multiplier))
        # Set stop loss from risk decision
        if decision.stop_loss_price > 0:
            leg.order.trigger_price = decision.stop_loss_price
        return leg

    def _execute_legs(self, legs: list[_Leg], broker_book: BrokerBook | None) -> None:
        """
        Place every approved leg through its own channel.

        Runs under the same lock as square-off, so a signal order and the
        square-off never decide against the broker at the same time. The
        entry gate is re-checked here, immediately before placement: an
        order approved by the risk gates a moment before 15:15 must not
        reach the broker (or the paper book) after square-off closed entries.
        """
        with self._square_off_lock:
            for leg in legs:
                if not leg.approved:
                    continue
                assert leg.positions is not None
                gate = self._risk_manager.entry_gate(self._gate_order(leg), leg.positions)
                if gate is not None:
                    logger.warning(f"{leg.name} leg blocked at placement: {gate.reason}")
                    self._audit.log(
                        AuditEventType.ORDER_REJECTED,
                        {"symbol": leg.order.symbol, "side": leg.order.transaction_type.value,
                         "leg": leg.name, "reason": gate.reason,
                         "stage": "pre-placement entry gate"},
                    )
                    leg.result = OrderResult(
                        success=False, status=OrderStatus.REJECTED, message=gate.reason,
                        is_paper=leg.is_paper,
                    )
                    continue
                if leg.is_paper:
                    leg.result = self._paper_trader.place_order(leg.order)
                else:
                    assert broker_book is not None  # an approved live leg has a verified book
                    try:
                        leg.result = self._execute_live(
                            leg.order, verified_held=broker_book.held, broker_book=broker_book,
                        )
                    except Exception as e:
                        logger.error(f"Live execution failed: {e}")
                        leg.result = OrderResult(
                            success=False, status=OrderStatus.REJECTED,
                            message=f"Live execution error: {e}",
                        )

    @staticmethod
    def _gate_order(leg: _Leg) -> OrderRequest:
        """
        The order as the entry gate should judge it. A SELL is judged by
        what will actually be sent: the paper trader and the live path both
        cap a SELL to the holding, so a capital-sized quantity must not make
        a genuine exit look like a flip to short.
        """
        order = leg.order
        if order.transaction_type == TransactionType.SELL and leg.positions is not None:
            held = sum(p.quantity for p in leg.positions if p.symbol == order.symbol)
            if 0 < held < order.quantity:
                return replace(order, quantity=held)
        return order

    def _record_leg(self, leg: _Leg, signal: Signal, analysis: AnalysisResult) -> None:
        """
        Stats, circuit breaker and audit for one leg. Only a FILLED result
        is audited as a fill; a live order that the broker merely accepted
        is PLACED and is audited as such (no fill quantity or price is
        known yet). In DUAL each leg is audited on its own, so a paper fill
        never stands in for a broker fill.
        """
        result = leg.result
        assert result is not None
        if result.status == OrderStatus.UNKNOWN:
            # Submitted; outcome not confirmed either way. Not a rejection.
            self._orders_unknown += 1
            self._circuit_breaker.record_order_unknown()
            self._audit.log(
                AuditEventType.ERROR,
                {
                    "action": "order_outcome_unknown",
                    "symbol": leg.order.symbol, "side": leg.order.transaction_type.value,
                    "mode": self._mode.value, "leg": leg.name, "quantity": leg.order.quantity,
                    "detail": result.message,
                    "note": "submitted to the broker; neither placement nor rejection confirmed — "
                            "reservation/intent kept and reconciled against the broker's books",
                },
            )
            return
        if not result.success:
            self._orders_rejected += 1
            self._circuit_breaker.record_order_failure()
            return

        self._orders_placed += 1
        ctx = {
            "symbol": leg.order.symbol,
            "side": leg.order.transaction_type.value,
            "mode": self._mode.value,
            "order_id": result.order_id,
            "signal_score": signal.score,
            "analysis_confidence": analysis.confidence,
            "analyzer": analysis.analyzer_name,
        }
        if self._mode == TradingMode.DUAL:
            ctx["leg"] = leg.name
        if result.status == OrderStatus.FILLED:
            self._orders_filled += 1
            self._audit.log(
                AuditEventType.ORDER_FILLED,
                {**ctx, "quantity": result.filled_qty, "fill_price": result.avg_fill_price},
            )
        else:
            self._audit.log(
                AuditEventType.ORDER_PLACED,
                {
                    **ctx,
                    "status": result.status.value,
                    "placed_qty": result.placed_qty,
                    "broker_order_id": result.broker_order_id,
                    "note": "broker acknowledged placement — fill not confirmed",
                },
            )

    def _combine_legs(self, legs: list[_Leg]) -> OrderResult:
        """
        PAPER/LIVE: the single leg's result. DUAL: the paper leg's result
        with the live leg's in .live_leg — each honest about its own
        outcome, neither standing in for the other.
        """
        if len(legs) == 1:
            assert legs[0].result is not None
            return legs[0].result
        paper, live = legs
        assert paper.result is not None and live.result is not None
        paper_result = paper.result
        live_result = live.result
        logger.info(
            f"DUAL mode: paper={paper_result.status.value}, live={live_result.status.value}"
        )
        paper_result.live_leg = live_result
        if live_result.success:
            live_note = f"LIVE leg placed: {live_result.broker_order_id}"
        elif live_result.status == OrderStatus.UNKNOWN:
            live_note = (
                f"LIVE leg SUBMITTED, outcome UNKNOWN (not confirmed placed or rejected): "
                f"{live_result.message}"
            )
        else:
            live_note = f"LIVE leg REJECTED: {live_result.message}"
        paper_result.message = f"Paper: {paper_result.message} | {live_note}"
        return paper_result

    def _broker_book(self, api: Any, order: OrderRequest) -> BrokerBook:
        """
        The broker's exposure in the order's product type, as rows the risk
        gates can judge a new order against. Raises BrokerBookUnverifiable
        (with the reason) when the books cannot be read or reconciled —
        that is NOT an empty book, and callers must not treat it as one.

        The paper position book is deliberately not consulted: it is this
        process's belief, not the broker's ledger, and after a restart it
        knows nothing about positions or orders the previous process left.

        Position book (always): rows with non-zero net quantity in the
        order's product type. The order's own instrument is matched on
        exchange AND token/trading symbol AND product type (tokens are
        exchange-specific; a DELIVERY holding is not an INTRADAY one) and
        is given the order's symbol so the per-stock gate recognises it.
        Every other row counts towards the total-position and
        deployed-capital gates under the broker's trading symbol.

        Order book (BUY only): a BUY still working at the broker is
        exposure the position book does not show yet. Its UNFILLED
        quantity is added to the same instrument's row — the filled part
        is already in the position book, so nothing is counted twice — at
        its limit price, or its average fill price for a partial fill, or
        (for the order's own instrument) the signal price. A working BUY
        that cannot be valued, matched or sized makes the book
        unverifiable: the capital it commits is unknown, not free.

        A position row with no readable average price is still a position
        but its avg_buy_price is 0.0 (unknown cost); _guard_live_entry
        refuses a BUY while any such row is open.
        """
        try:
            response = api.position()
        except Exception as e:
            raise BrokerBookUnverifiable(f"broker position query failed: {e}")
        if not isinstance(response, dict) or not response.get("status"):
            raise BrokerBookUnverifiable(f"broker position query rejected: {response}")

        # (exchange, token) -> [symbol, quantity, value, priced]; the order's
        # own instrument under a fixed key so rows that match by token and
        # rows that match by trading symbol merge.
        OURS = ("*", order.symbol)
        acc: dict[tuple[str, str], list] = {}

        def add(key: tuple[str, str], symbol: str, qty: int, price: float) -> None:
            slot = acc.setdefault(key, [symbol, 0, 0.0, True])
            slot[1] += qty
            slot[2] += qty * price
            if price <= 0:
                slot[3] = False

        # Per instrument: the day's BUY quantity the position book reports
        # (None = a row's buyqty was unreadable) and the day's filled BUY
        # quantity the order book reports. Fills the position book has not
        # caught up with are exposure it does not show yet.
        bought: dict[tuple[str, str], int | None] = {}
        filled_in_book: dict[tuple[str, str], list] = {}  # key -> [qty, value]

        held = 0
        for row in response.get("data") or []:  # SmartAPI returns data=None when flat
            try:
                if str(row.get("producttype", "")).upper() != order.product_type.value:
                    continue
                net = int(float(row.get("netqty", 0)))
                same = self._same_instrument(
                    row, order.exchange.value, str(order.token), order.trading_symbol,
                )
                if same is None:
                    raise BrokerBookUnverifiable(
                        f"broker position row for {row.get('tradingsymbol')} carries no exchange — "
                        f"cannot tell whether it is {order.symbol}"
                    )
                price = self._row_average_price(row, net) if net else 0.0
            except (TypeError, ValueError) as e:
                raise BrokerBookUnverifiable(f"unreadable broker position row {row} ({e})")
            if same:
                key, symbol = OURS, order.symbol
            else:
                exchange = str(row.get("exchange") or "").strip().upper()
                token = str(row.get("symboltoken", ""))
                key, symbol = (exchange, token), str(row.get("tradingsymbol") or token)
            try:
                raw = row.get("buyqty")
                day_buys = int(float(raw)) if raw not in (None, "") else None
            except (TypeError, ValueError):
                day_buys = None
            if day_buys is None or bought.get(key, 0) is None:
                bought[key] = None
            else:
                bought[key] = bought.get(key, 0) + day_buys
            if net == 0:
                continue  # closed earlier today — not a position (its buyqty still counts above)
            if same:
                held += net
            add(key, symbol, net, price)

        pending_qty = 0
        pending_ids: list[str] = []
        reserved_qty = 0
        reserved_note = ""
        lag_note = ""
        if order.transaction_type == TransactionType.BUY:
            orders = self._read_order_book(api, datetime.now(IST).date())
            if orders is None:
                raise BrokerBookUnverifiable(
                    "broker order book could not be read — cannot tell whether a BUY is already "
                    "working (or partly filled) for this or any other instrument"
                )

            # Every unresolved entry reservation — any instrument, any day —
            # draws on the same budget. Loaded once: to value identifiable
            # working orders at their saved sizing price, and to count what
            # the broker's books do not show yet (below).
            reservations = self._unresolved_reservations()
            res_by_order_id = {r.broker_order_id: r for r in reservations if r.broker_order_id}
            res_by_tag = {r.order_tag: r for r in reservations}

            for o in orders:
                if str(o.get("producttype", "")).upper() != order.product_type.value:
                    continue
                if str(o.get("transactiontype", "")).upper() != TransactionType.BUY.value:
                    continue  # a working SELL reduces exposure; never counted as an entry
                status = self._order_status(o)
                order_id = str(o.get("orderid", "?"))
                label = f"BUY order {order_id} for {o.get('tradingsymbol') or o.get('symboltoken')}"
                same = self._same_instrument(o, order.exchange.value, str(order.token), order.trading_symbol)
                exchange = str(o.get("exchange") or "").strip().upper()
                if same is None or not exchange:
                    raise BrokerBookUnverifiable(
                        f"{label} carries no exchange — cannot tell which instrument it is for"
                    )
                try:
                    _qty, filled, unfilled = self._order_quantities(o)
                except (TypeError, ValueError) as e:
                    raise BrokerBookUnverifiable(f"{label} has unreadable quantities ({e})")
                token = str(o.get("symboltoken", ""))
                key = OURS if same else (exchange, token)
                symbol = order.symbol if same else str(o.get("tradingsymbol") or token)
                if filled > 0:
                    # Executed today (fully, partly, or before a cancel): the
                    # position book must show it; checked below.
                    fill_price = self._pending_entry_price({"averageprice": o.get("averageprice")}, 0.0)
                    slot = filled_in_book.setdefault(key, [0, 0.0, symbol])
                    slot[0] += filled
                    slot[1] += filled * fill_price
                if status not in _ORDER_OPEN_STATUSES or unfilled <= 0:
                    continue
                # Value the unfilled part: limit price / partial fill price,
                # else the signal price for our own instrument, else the
                # sizing estimate saved when this process reserved it.
                fallback = order.price if same else 0.0
                identified = res_by_order_id.get(order_id) or res_by_tag.get(str(o.get("ordertag") or ""))
                if identified is not None and self._order_matches_reservation(o, identified) is not True:
                    identified = None  # same id/tag but not the reservation's instrument: no estimate for it
                if fallback <= 0 and identified is not None:
                    fallback = identified.price_estimate
                price = self._pending_entry_price(o, fallback)
                if price <= 0:
                    raise BrokerBookUnverifiable(
                        f"working {label} ({unfilled} unfilled, {str(o.get('ordertype') or '?').upper()}) "
                        f"has no price to value the capital it commits and no saved sizing estimate"
                    )
                if same:
                    pending_qty += unfilled
                    pending_ids.append(order_id)
                add(key, symbol, unfilled, price)

            # Fills the order book reports that the position book does not
            # reflect yet (an earlier snapshot): count the shortfall.
            lagging: set[tuple[str, str]] = set()
            for key, (filled_qty, filled_value, symbol) in filled_in_book.items():
                reflected = bought.get(key, 0)
                if reflected is None:
                    raise BrokerBookUnverifiable(
                        f"the order book shows {filled_qty} {symbol} bought today but the position "
                        f"row's buyqty is unreadable — cannot confirm the fills are reflected"
                    )
                missing = filled_qty - reflected
                if missing <= 0:
                    continue
                price = (filled_value / filled_qty) if filled_value > 0 else (order.price if key == OURS else 0.0)
                if price <= 0:
                    raise BrokerBookUnverifiable(
                        f"{missing} {symbol} filled today are not in the position book yet and "
                        f"have no fill price to value them"
                    )
                lagging.add(key)
                add(key, symbol, missing, price)
                if key == OURS:
                    lag_note = (
                        f" — the order book shows {filled_qty} {order.symbol} bought today but the "
                        f"position book shows only {reflected}; the position book has not caught up, "
                        f"so the {missing} not yet reflected are counted"
                    )

            # Reservations the broker's books do not show yet (accepted but
            # not listed, reply lost, or from an earlier day and never
            # resolved) are still exposure — for every instrument. Counted
            # only where the order book does not already show them.
            for res in reservations:
                same_res = (
                    res.exchange.upper() == order.exchange.value.upper()
                    and str(res.token) == str(order.token)
                    and res.product_type.upper() == order.product_type.value
                )
                key = OURS if same_res else (res.exchange.upper(), str(res.token))
                counted = self._reconcile_reservation(
                    api, order, res, same_res, orders,
                    bought.get(key, 0), filled_in_book.get(key, [0, 0.0, ""])[0], key in lagging,
                )
                if counted is None:
                    continue
                qty, price, note = counted
                if same_res:
                    reserved_qty += qty
                    reserved_note += note
                if qty:
                    add(key, order.symbol if same_res else res.trading_symbol, qty, price)

        positions: list[Position] = []
        for key, (symbol, qty, value, priced) in acc.items():
            if qty == 0:
                continue
            avg = (value / qty) if priced else 0.0
            positions.append(Position(
                symbol=symbol,
                token=order.token if key == OURS else key[1],
                quantity=qty,  # signed as the broker reports it (+long / -short)
                avg_buy_price=avg,
                current_price=order.price if key == OURS else avg,
                product_type=order.product_type,
                is_paper=False,
            ))
        return BrokerBook(
            positions=positions, held=held,
            pending_entry_qty=pending_qty, pending_entry_ids=tuple(pending_ids),
            reserved_qty=reserved_qty, reserved_note=lag_note + reserved_note,
        )

    def _order_matches_reservation(self, o: dict, res: EntryReservation) -> bool | None:
        """
        Does this order-book row belong to the reservation's instrument
        (exchange AND token/trading symbol), product type and side (BUY)?
        None when the row carries no exchange: tokens are exchange-specific,
        so the match cannot be verified.
        """
        if str(o.get("transactiontype", "")).upper() != TransactionType.BUY.value:
            return False
        if str(o.get("producttype", "")).upper() != res.product_type.upper():
            return False
        return self._same_instrument(o, res.exchange, str(res.token), res.trading_symbol)

    def _tagged_order_for(
        self, res: EntryReservation, orders: list[dict]
    ) -> tuple[dict | None, str | None]:
        """
        The ONE order carrying the reservation's tag that is verifiably for
        its instrument, product and side — or (None, why not). A matching
        tag alone proves nothing: the tag embeds the token, and tokens are
        exchange-specific. Look-alikes (another exchange, product or side),
        an unverifiable row (no exchange) or more than one genuine match
        all mean: do not adopt, keep the reservation, say why.
        """
        tagged = [o for o in orders if str(o.get("ordertag", "")) == res.order_tag]
        if not tagged:
            return None, None
        matches: list[dict] = []
        unverifiable: list[str] = []
        look_alikes: list[str] = []
        for o in tagged:
            verdict = self._order_matches_reservation(o, res)
            oid = str(o.get("orderid", "?"))
            if verdict is True:
                matches.append(o)
            elif verdict is None:
                unverifiable.append(oid)
            else:
                look_alikes.append(
                    f"{oid} ({o.get('exchange') or '?'}/{o.get('symboltoken')}/{o.get('producttype')}/"
                    f"{o.get('transactiontype')}, status '{self._order_status(o)}')"
                )
        want = f"{res.exchange}/{res.token}/{res.product_type}/BUY"
        if len(matches) == 1 and not unverifiable:
            return matches[0], None
        if len(matches) > 1:
            ids = ", ".join(str(o.get("orderid", "?")) for o in matches)
            return None, (
                f"orders {ids} all carry tag {res.order_tag} and match {want} — ambiguous, none adopted"
            )
        if unverifiable:
            return None, (
                f"order(s) {', '.join(unverifiable)} carry tag {res.order_tag} but have no exchange — "
                f"cannot verify they are {want}; not adopted"
            )
        return None, (
            f"order(s) {'; '.join(look_alikes)} carry tag {res.order_tag} but are not {want} — not adopted"
        )

    def _unresolved_reservations(self) -> list[EntryReservation]:
        if self._entry_reservations is None:
            return []
        try:
            return self._entry_reservations.unresolved()
        except EntryReservationStoreError as e:
            raise BrokerBookUnverifiable(f"entry reservation records could not be read ({e})")

    def _reconcile_reservation(
        self,
        api: Any,
        order: OrderRequest | None,
        res: EntryReservation,
        ours: bool,
        orders: list[dict],
        bought_today: int | None,
        filled_in_book: int,
        lagging: bool,
    ) -> tuple[int, float, str] | None:
        """
        Reconcile one entry reservation against the broker. Returns
        (quantity, price, note) when it is exposure the books do not show
        and must be counted (quantity may be 0 when the note only explains
        why it is kept); None when it was released or the order book
        already counts its order. Raises BrokerBookUnverifiable when it is
        for the order's own instrument, from an earlier day, and the broker
        cannot confirm its fate — a new day proves nothing, and that
        instrument must not be entered until someone checks. For another
        instrument the same case is counted rather than blocking.

        Released ONLY when:
          - the broker lists its order terminal with nothing filled; or
          - terminal with fills AND the position book reflects the day's
            fills for that instrument (no lag); or
          - it is from an earlier day and the broker confirms its order
            terminal.
        Kept and counted otherwise (a missing row is not proof of
        rejection): unlisted → the broker's own status lookup if it
        answers (open → its unfilled quantity; terminal with fills not yet
        reflected → those fills), else the reserved quantity; never
        acknowledged → the reserved quantity unless an order carrying its
        tag is listed, which is adopted.
        """
        assert self._entry_reservations is not None
        key = res.key
        today = f"{datetime.now(IST).date():%Y%m%d}"
        stale = res.day != today
        name = order.symbol if (ours and order is not None) else res.trading_symbol
        listed = {str(o.get("orderid", "")): o for o in orders}

        def release(reason: str) -> None:
            try:
                self._entry_reservations.resolve(key, reason)
            except EntryReservationStoreError as e:
                raise BrokerBookUnverifiable(f"entry reservation could not be released ({e})")
            logger.info(f"{name}: entry reservation {key} released — {reason}")

        def stale_detail(detail: str) -> str:
            return (
                f"entry reservation from {res.day[:4]}-{res.day[4:6]}-{res.day[6:]} for {name} "
                f"(BUY {res.quantity}{', order ' + res.broker_order_id if res.broker_order_id else ''}, "
                f"status '{res.status}', recorded by {res.owner}) was never resolved and {detail}. "
                f"A new day is not proof of rejection — check the broker, then remove "
                f"{self._entry_reservations.path_for(key)} if nothing came of it"
            )

        def unresolved(qty: int, detail: str) -> tuple[int, float, str]:
            if stale:
                if ours:
                    raise BrokerBookUnverifiable(stale_detail(detail))
                logger.warning(f"{name}: {stale_detail(detail)} — counted as committed capital")
            return qty, res.price_estimate, f" — entry reservation for {name}: {detail}"

        if not res.acknowledged:
            adopted, why_not = self._tagged_order_for(res, orders)
            if adopted is not None:
                try:
                    res = self._entry_reservations.mark_placed(
                        key, str(adopted.get("orderid", "")), str(adopted.get("uniqueorderid", "")),
                    )
                except EntryReservationStoreError as e:
                    raise BrokerBookUnverifiable(f"entry reservation record could not be updated ({e})")
                logger.warning(
                    f"{name}: adopted broker order {res.broker_order_id} (tag {res.order_tag}, verified "
                    f"{res.exchange}/{res.token}/{res.product_type}/BUY) for the entry reserved at "
                    f"{res.created_at} by {res.owner} ({res.submission})"
                )
            else:
                if why_not:
                    logger.error(f"{name}: {why_not}")
                return unresolved(
                    res.quantity,
                    f"BUY {res.quantity} was submitted at {res.created_at} by {res.owner} but its outcome is "
                    f"unknown ({res.submission or 'no ack stored'}); "
                    + (why_not if why_not else f"the broker lists no order tagged {res.order_tag}")
                    + f". A missing or unverifiable row is not proof of rejection — check the broker, then "
                    f"remove {self._entry_reservations.path_for(key)} if no such order exists",
                )

        row = listed.get(res.broker_order_id)
        verified = False
        if row is None and api is not None:
            current = self._verify_order_status(api, {
                "orderid": res.broker_order_id, "uniqueorderid": res.unique_order_id,
            })
            if current is not None:
                row = {**current, "orderid": res.broker_order_id}
                verified = True
        if row is not None and self._order_matches_reservation(row, res) is not True:
            # The broker answers for our order id with a row that is not our
            # instrument/product/side (or cannot be verified): its status
            # proves nothing about our submission.
            verdict = self._order_matches_reservation(row, res)
            return unresolved(
                res.quantity,
                f"the broker lists order {res.broker_order_id} as "
                f"{row.get('exchange') or '?'}/{row.get('symboltoken')}/{row.get('producttype')}/"
                f"{row.get('transactiontype')} (status '{self._order_status(row)}'), which "
                f"{'cannot be verified as' if verdict is None else 'is not'} "
                f"{res.exchange}/{res.token}/{res.product_type}/BUY — its status is not used; the "
                f"reservation is kept",
            )
        if row is None:
            return unresolved(
                res.quantity,
                f"BUY {res.quantity} accepted by the broker as order {res.broker_order_id} (recorded "
                f"{res.placed_at or res.created_at} by {res.owner}) is not in the broker order book yet and "
                f"its status could not be verified. A missing row is not proof of rejection — it is counted "
                f"until the broker shows it terminal",
            )

        status = self._order_status(row)
        try:
            _qty, filled, unfilled = self._order_quantities(row)
        except (TypeError, ValueError):
            filled, unfilled = res.quantity, 0  # unreadable: assume it may all have executed
        if status in _ORDER_TERMINAL_STATUSES:
            if stale:
                release(f"broker confirms order {res.broker_order_id} {status} (from {res.day})")
                return None
            if filled <= 0:
                release(f"broker order {res.broker_order_id} {status}, nothing filled")
                return None
            if verified:
                # Not in the order book, so its fill is not in filled_in_book: the
                # position book must show the day's listed fills plus this one.
                if bought_today is not None and bought_today >= filled_in_book + filled:
                    release(f"broker confirms order {res.broker_order_id} {status}; fill reflected in position book")
                    return None
                return (
                    filled, res.price_estimate,
                    f" — entry reservation for {name}: order {res.broker_order_id} is not in the order book "
                    f"but the broker reports it '{status}' with {filled} filled, and the position book "
                    f"(bought today: {bought_today if bought_today is not None else 'unreadable'}) does not "
                    f"reflect that yet — counted until it does",
                )
            if lagging:
                return (
                    0, 0.0,
                    f" — entry reservation for {name}: order {res.broker_order_id} is '{status}' with "
                    f"{filled} filled but the position book has not caught up; the reservation is kept "
                    f"until it does",
                )
            release(f"broker order {res.broker_order_id} {status}; {filled} filled and reflected in position book")
            return None
        if status not in _ORDER_OPEN_STATUSES:
            return unresolved(
                res.quantity,
                f"order {res.broker_order_id} has unrecognised status '{status}' — counted until the "
                f"broker shows it terminal",
            )
        # open
        if res.broker_order_id in listed:
            return None  # the order-book scan already counts it
        return unresolved(
            unfilled,
            f"BUY {res.quantity} accepted as order {res.broker_order_id} is not in the broker order book "
            f"yet but the broker reports it '{status}' ({unfilled} unfilled)",
        )

    @staticmethod
    def _row_average_price(row: dict, net: int) -> float:
        """
        Average price of a position row, or 0.0 when the row carries none
        (unknown — not zero cost). Raises ValueError on a value that is
        present but not a number.
        """
        side_key = "buyavgprice" if net > 0 else "sellavgprice"
        for key in ("averageprice", "avgnetprice", side_key, "netprice"):
            raw = row.get(key)
            if raw in (None, ""):
                continue
            price = float(raw)
            if price > 0:
                return price
        return 0.0

    @staticmethod
    def _pending_entry_price(order_row: dict, fallback: float) -> float:
        """
        Price to value a working BUY's unfilled quantity at: its limit
        price, else its average fill price (partial fill), else `fallback`
        (the signal price for the order's own instrument, 0 otherwise).
        """
        for key in ("price", "averageprice"):
            raw = order_row.get(key)
            if raw in (None, ""):
                continue
            try:
                price = float(raw)
            except (TypeError, ValueError):
                continue
            if price > 0:
                return price
        return fallback

    def _verified_broker_holding(self, api: Any, order: OrderRequest) -> int | None:
        """Net broker quantity for the order's instrument; None if unverifiable."""
        try:
            return self._broker_book(api, order).held
        except BrokerBookUnverifiable as e:
            logger.error(f"{order.symbol}: {e}")
            return None

    def _read_broker_book(self, api: Any, order: OrderRequest) -> tuple[BrokerBook | None, str | None]:
        """(book, None) or (None, reason)."""
        try:
            return self._broker_book(api, order), None
        except BrokerBookUnverifiable as e:
            logger.error(f"{order.symbol}: broker book unverifiable — {e}")
            return None, str(e)

    def _query_broker_book(self, order: OrderRequest) -> tuple[BrokerBook | None, str | None]:
        """Broker book via the live session: (book, None) or (None, reason)."""
        if not self._live_session:
            return None, "no live session — SmartAPI not connected"
        try:
            api = self._live_session.get_api()
        except Exception as e:
            logger.error(f"Broker session unavailable for {order.symbol}: {e}")
            return None, f"broker session unavailable: {e}"
        return self._read_broker_book(api, order)

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

    @staticmethod
    def _unverifiable_buy_rejection(order: OrderRequest, reason: str = "") -> OrderResult:
        why = f" ({reason})" if reason else ""
        return OrderResult(
            success=False,
            status=OrderStatus.REJECTED,
            message=(
                f"BUY {order.symbol} blocked: broker books could not be verified{why}. An "
                f"unreadable book is not an empty one — the broker may already hold "
                f"{order.symbol}, have a BUY working for it, or have the capital committed. "
                f"Check the broker position and order books."
            ),
        )

    def _unverifiable_broker_book_rejection(self, order: OrderRequest, reason: str = "") -> OrderResult:
        if order.transaction_type == TransactionType.SELL:
            return self._unverifiable_sell_rejection(order)
        return self._unverifiable_buy_rejection(order, reason)

    def _guard_live_entry(
        self, api: Any, order: OrderRequest, broker_book: BrokerBook | None
    ) -> OrderResult | None:
        """
        LIVE/DUAL guard: a BUY is judged against the broker's verified
        books — positions held plus BUYs still working — not the paper
        book, before it is sent. Rejects when the broker already holds or
        is buying the stock at its per-stock limit, the total-position
        limit is reached, or the capital the broker has deployed and
        committed leaves no room. Rejects when the books cannot be
        verified: they are not treated as empty.
        """
        hold, hold_error = self._shared_entry_hold()
        if hold_error:
            return self._unverifiable_buy_rejection(order, f"shared entry hold could not be read ({hold_error})")
        if hold is not None:
            return self._entries_closed_rejection(order, hold)
        if broker_book is None:
            broker_book, reason = self._read_broker_book(api, order)
            if broker_book is None:
                return self._unverifiable_buy_rejection(order, reason or "")
        unpriced = [p for p in broker_book.positions if p.avg_buy_price <= 0]
        if unpriced:
            names = ", ".join(f"{p.symbol} x{p.quantity}" for p in unpriced)
            return OrderResult(
                success=False,
                status=OrderStatus.REJECTED,
                message=(
                    f"BUY {order.quantity} {order.symbol} blocked: the capital the broker has "
                    f"deployed could not be verified — position row(s) without a readable "
                    f"average price: {names}. Unknown capital is not free capital."
                ),
            )
        decision = self._risk_manager.entry_limits(order, broker_book.positions)
        if decision is None:
            return None
        return OrderResult(
            success=False,
            status=OrderStatus.REJECTED,
            message=(
                f"BUY {order.quantity} {order.symbol} blocked by broker books: {decision.reason}"
                f"{self._pending_entry_note(broker_book, order)}"
            ),
        )

    @staticmethod
    def _pending_entry_note(book: BrokerBook | None, order: OrderRequest) -> str:
        """Suffix naming the BUY(s) still working for the order's instrument, if any."""
        if book is None:
            return ""
        note = ""
        if book.pending_entry_qty:
            ids = ", ".join(book.pending_entry_ids)
            held = f"{book.held} held plus " if book.held else ""
            note += (
                f" — counts BUY {book.pending_entry_qty} {order.symbol} still working at the broker "
                f"(order {ids}; {held}{book.pending_entry_qty} unfilled)"
            )
        return note + book.reserved_note

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
        self,
        order: OrderRequest,
        verified_held: int | None = None,
        broker_book: BrokerBook | None = None,
    ) -> OrderResult:
        """
        Execute order through SmartAPI (real broker).

        Both sides are judged against the broker's own books before
        anything is sent: a BUY against the positions it holds and the
        BUYs it already has working (_guard_live_entry), a SELL against its
        verified holding (_size_live_sell_to_verified_holding).
        `broker_book` / `verified_held` are the answers execute_signal
        already read; when absent the broker is asked here.
        """
        if not self._live_session:
            return OrderResult(
                success=False,
                status=OrderStatus.REJECTED,
                message="No live session — SmartAPI not connected",
            )

        try:
            api = self._live_session.get_api()

            exit_key: str | None = None
            entry_key: str | None = None
            if order.transaction_type == TransactionType.BUY:
                blocked = self._guard_live_entry(api, order, broker_book)
                if blocked is None:
                    # Reserve the entry durably BEFORE the broker is called:
                    # if the reply is lost or the broker lists the order
                    # late, the reservation is what stops a repeat.
                    blocked, entry_key = self._reserve_entry(order)
                if blocked:
                    logger.warning(blocked.message)
                    self._audit.log(
                        AuditEventType.ORDER_REJECTED,
                        {"symbol": order.symbol, "side": "BUY", "reason": blocked.message},
                    )
                    return blocked
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
            if entry_key is not None:
                order_params["ordertag"] = entry_order_tag(datetime.now(IST).date(), order.token)
            response, error = self._place_with_broker(api, order_params)
            result = self._live_placement_result(response, order.quantity, error)
            if exit_key is not None:
                self._record_exit_placement(exit_key, order.symbol, result, response)
            if entry_key is not None:
                self._record_entry_placement(entry_key, order, result, response)
            return result

        except Exception as e:
            logger.error(f"Live order execution failed: {e}")
            return OrderResult(
                success=False,
                status=OrderStatus.REJECTED,
                message=f"Live execution error: {e}",
            )

    def _reserve_entry(self, order: OrderRequest) -> tuple[OrderResult | None, str | None]:
        """
        Record the entry reservation durably. Returns (None, key) once it is
        saved, or (rejection, None) when the order must not be submitted:
        no store, a reservation for this instrument already unresolved
        today, or the store could not be written.
        """
        def reject(detail: str) -> tuple[OrderResult, None]:
            return OrderResult(
                success=False, status=OrderStatus.REJECTED,
                message=f"BUY {order.quantity} {order.symbol} blocked: {detail}",
            ), None

        if self._entry_reservations is None:
            return reject(
                "no entry-reservation store configured — a live entry cannot be recorded durably, "
                "so it is not submitted (a lost reply or a late listing could otherwise repeat it)"
            )
        today = datetime.now(IST).date()
        reservation = EntryReservation(
            key=reservation_key(today, order.exchange.value, order.token, order.product_type.value),
            day=f"{today:%Y%m%d}", exchange=order.exchange.value.upper(), token=str(order.token),
            trading_symbol=order.trading_symbol, product_type=order.product_type.value,
            side=TransactionType.BUY.value, quantity=order.quantity, price_estimate=order.price,
            order_tag=entry_order_tag(today, order.token),
        )
        try:
            reserved = self._entry_reservations.reserve(reservation)
        except EntryReservationStoreError as e:
            detail = f"entry reservation could not be saved ({e}) — not submitting an order that could not be tracked"
            logger.error(f"{order.symbol}: {detail}")
            return reject(detail)
        if not reserved:
            try:
                existing = self._entry_reservations.get(reservation.key)
            except EntryReservationStoreError:
                existing = None
            who = (
                f"recorded at {existing.created_at} by {existing.owner}, status '{existing.status}'"
                f"{', order ' + existing.broker_order_id if existing and existing.broker_order_id else ''}"
                if existing else "details unreadable"
            )
            detail = (
                f"an entry for {order.symbol} is already reserved today ({who}) — another call or "
                f"instance got there first; not sending another"
            )
            logger.warning(f"{order.symbol}: {detail}")
            return reject(detail)
        return None, reservation.key

    def _record_entry_placement(
        self, key: str, order: OrderRequest, result: OrderResult, response: Any
    ) -> None:
        """
        After the broker was called: store the broker's ids on an
        acknowledgement (PLACED — not filled); release the reservation on
        an explicit rejection (nothing is on its way); keep it, marked
        unclear, when the outcome is unknown — the order may be working.
        """
        assert self._entry_reservations is not None
        if result.success:
            unique_id = ""
            if isinstance(response, dict):
                unique_id = str(((response.get("data") or {}).get("uniqueorderid", "")))
            try:
                self._entry_reservations.mark_placed(key, result.broker_order_id, unique_id)
            except EntryReservationStoreError as e:
                # The order is out but the record still says "reserved". The
                # next check adopts it from the order book by its tag, or
                # keeps counting it — no duplicate, but the operator must know.
                logger.error(
                    f"{order.symbol}: entry {result.broker_order_id} placed but its reservation could "
                    f"not be updated ({e})"
                )
                self._audit.log(
                    AuditEventType.ERROR,
                    {"action": "entry_reservation_update_failed", "key": key,
                     "broker_order_id": result.broker_order_id, "error": str(e)},
                )
            return
        if result.status == OrderStatus.REJECTED:
            try:
                self._entry_reservations.resolve(key, f"rejected: {result.message}")
            except EntryReservationStoreError as e:
                logger.error(f"{order.symbol}: could not release reservation {key} after rejection: {e}")
            return
        try:
            self._entry_reservations.mark_unclear(key, result.message)
        except EntryReservationStoreError as e:
            logger.error(f"{order.symbol}: could not mark reservation {key} unclear: {e}")
        self._audit.log(
            AuditEventType.ERROR,
            {"action": "entry_submission_unclear", "key": key, "symbol": order.symbol,
             "quantity": order.quantity, "detail": result.message,
             "note": "reservation kept — counted as exposure until the broker shows the order terminal"},
        )

    @staticmethod
    def _place_with_broker(api: Any, params: dict) -> tuple[Any, Exception | None]:
        """
        Submit one order. Prefers SmartConnect.placeOrderFullResponse (the
        full dict, including uniqueorderid) and falls back to placeOrder
        (which returns only the order id string). Returns (response, None)
        or (None, exception) — a transport error is NOT a rejection: the
        request may have reached the broker.
        """
        place = getattr(api, "placeOrderFullResponse", None)
        if not callable(place):
            place = api.placeOrder
        try:
            return place(params), None
        except Exception as e:
            logger.error(f"Live order submission raised: {e!r}")
            return None, e

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
        """
        After placeOrder: store the broker's ids on an acknowledgement;
        release the intent on an explicit rejection; keep it (unacknowledged)
        when the outcome is unknown — the next square-off adopts the order
        from the book by its tag or reports the position UNCLEAR.
        """
        assert self._exit_intents is not None
        if result.success:
            unique_id = ""
            if isinstance(response, dict):
                unique_id = str(((response.get("data") or {}).get("uniqueorderid", "")))
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
        elif result.status == OrderStatus.REJECTED:
            # Nothing is on its way: release the intent so a later exit may try again.
            try:
                self._exit_intents.resolve(key, f"rejected: {result.message}")
            except ExitIntentStoreError as e:
                logger.error(f"could not resolve intent {key} after rejection: {e}")
        else:
            logger.error(f"{symbol}: exit submission outcome unknown — intent {key} kept: {result.message}")

    @staticmethod
    def _live_placement_result(response: Any, quantity: int, error: Exception | None = None) -> OrderResult:
        """
        Turn what the SmartAPI SDK gave back into an OrderResult, in the
        SDK's real shapes:

            placeOrderFullResponse → {"status": True, "data": {"orderid", "uniqueorderid", …}}
            placeOrder             → the order id as a STRING
            either, on failure     → None (the SDK logs the broker's reason and swallows it:
                                     a rejection OR a reply it could not parse)
            transport error        → raises (handled by _place_with_broker → `error`)
            {"status": False, "message": …} → an explicit rejection (older SDKs / wrappers)

        An acknowledgement only means the broker ACCEPTED the order into
        its book: the result is PLACED with placed_qty set and
        filled_qty/avg_fill_price at 0. Only an explicit status False is
        REJECTED. None, a raised error, an empty dict, a reply without a
        status — anything else — is UNKNOWN: the order may be working at
        the broker, and the reservation/intent is kept.
        """
        def placed(broker_order_id: str) -> OrderResult:
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

        def unknown(detail: str) -> OrderResult:
            return OrderResult(
                success=False,
                status=OrderStatus.UNKNOWN,
                message=(
                    f"Live submission outcome unknown: {detail} — the order may be working at the "
                    f"broker; its reservation is kept and counted until the broker shows it terminal"
                ),
            )

        if error is not None:
            return unknown(f"{type(error).__name__}: {error}")
        if isinstance(response, str):
            if response.strip():
                return placed(response.strip())
            return unknown("the SDK returned an empty order id")
        if isinstance(response, dict):
            status = response.get("status")
            if status is True or (isinstance(status, str) and status.strip().lower() == "true"):
                broker_order_id = str((response.get("data") or {}).get("orderid") or "").strip()
                if broker_order_id:
                    return placed(broker_order_id)
                return unknown(f"the broker reported success without an order id: {response}")
            if status is False or (isinstance(status, str) and status.strip().lower() == "false"):
                # The only reply that proves the broker did NOT accept the order.
                return OrderResult(
                    success=False,
                    status=OrderStatus.REJECTED,
                    message=f"Broker rejected: {response.get('message') or 'no reason given'}",
                )
            # {} / no "status" / status None: malformed — not a rejection, not an ack.
            return unknown(f"malformed reply without a status: {response!r}")
        if response is None:
            return unknown(
                "the SDK returned None (it swallows both a broker rejection and a reply it could "
                "not parse — not proof of rejection)"
            )
        return unknown(f"unrecognised SDK reply {response!r}")

    def _get_current_positions(self) -> list[Position]:
        """
        The paper book: what this process believes it holds.

        In LIVE mode execute_signal replaces this with the positions
        verified at the broker (see _broker_book); in DUAL mode this
        is the paper leg's book and the live leg is judged separately.
        """
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
        # 1. Close entries FIRST — in this process and, through the shared
        #    hold, in every instance sharing the storage. A BUY decision
        #    re-checks the hold immediately before placeOrder, so from this
        #    moment only a decision already inside its broker call can
        #    still produce an entry; those are reconciled below.
        self._risk_manager.close_new_entries("intraday square-off done", key="square_off")
        self._close_shared_entries("intraday square-off done")
        # 2. Lock order, everywhere: cross-process entry lock FIRST, then
        #    the in-process square-off lock. A live BUY decision takes them
        #    in the same order (execute_signal → _execute_legs), so the two
        #    can wait on each other but never deadlock. If the entry lock
        #    cannot be obtained, exits are not held back: square-off runs
        #    without it and SAYS so — another instance may be mid-decision.
        entry_lock, lock_skipped = self._square_off_entry_lock()
        with entry_lock:
            if not self._square_off_lock.acquire(blocking=False):
                logger.warning("Square-off already running in this process — waiting for it to finish")
                self._square_off_lock.acquire()
            try:
                return self._square_off_locked(lock_skipped)
            finally:
                self._square_off_lock.release()

    def _square_off_entry_lock(self) -> tuple[AbstractContextManager, str | None]:
        """(lock, None) or, when it could not be obtained, (no-op, reason)."""
        if self._mode == TradingMode.PAPER or self._entry_reservations is None:
            return nullcontext(), None
        try:
            return self._entry_reservations.lock(), None
        except EntryReservationStoreError as e:
            reason = (
                f"square-off ran WITHOUT the entry lock ({e}) — an instance sharing the storage "
                f"may have been mid-decision; any submission it completed is reconciled from its "
                f"entry reservation below, and a flat snapshot is not settled until it is"
            )
            logger.error(f"Square-off: {reason}")
            self._audit.log(
                AuditEventType.ERROR,
                {"action": "square_off_without_entry_lock", "error": str(e)},
            )
            return nullcontext(), reason

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

        orders = self._read_order_book(api, now.date())
        conf.unreconciled_entries = self._unreconciled_entries_now(api, rows, orders)

        if not open_rows and not conf.rows_unreadable:
            conf.flat = True
            return conf
        conf.flat = False

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

    def _unreconciled_entries_now(
        self, api: Any, rows: list[dict], orders: list[dict] | None
    ) -> list[str]:
        """
        Entry reservations still unresolved after square-off, described —
        each one a submission that may be working or filled without the
        position book showing it. Reconciled with the broker first
        (releasing those the broker shows terminal and reflected), so the
        list shrinks as the broker catches up and is empty once settled.
        """
        if self._entry_reservations is None:
            return []
        try:
            reservations = self._entry_reservations.unresolved()
        except EntryReservationStoreError as e:
            return [f"entry reservation records could not be read ({e})"]
        if not reservations:
            return []
        if orders is None:
            return [
                f"BUY {r.quantity} {r.trading_symbol} ({r.key}, '{r.status}'"
                f"{', order ' + r.broker_order_id if r.broker_order_id else ''}) — order book unreadable, "
                f"cannot reconcile"
                for r in reservations
            ]
        out: list[str] = []
        for res in reservations:
            bought: int | None = 0
            filled = 0
            for row in rows:
                if (
                    str(row.get("symboltoken", "")) == str(res.token)
                    and str(row.get("exchange") or "").upper() == res.exchange.upper()
                    and str(row.get("producttype", "")).upper() == res.product_type.upper()
                ):
                    try:
                        raw = row.get("buyqty")
                        bought = (bought or 0) + int(float(raw)) if raw not in (None, "") and bought is not None else None
                    except (TypeError, ValueError):
                        bought = None
            for o in orders:
                if (
                    str(o.get("transactiontype", "")).upper() == TransactionType.BUY.value
                    and str(o.get("symboltoken", "")) == str(res.token)
                    and str(o.get("exchange") or "").upper() == res.exchange.upper()
                    and str(o.get("producttype", "")).upper() == res.product_type.upper()
                ):
                    try:
                        filled += self._order_quantities(o)[1]
                    except (TypeError, ValueError):
                        pass
            lagging = bought is None or filled > bought
            try:
                counted = self._reconcile_reservation(api, None, res, False, orders, bought, filled, lagging)
            except BrokerBookUnverifiable as e:
                out.append(f"BUY {res.quantity} {res.trading_symbol}: {e}")
                continue
            if counted is not None:
                qty, _price, note = counted
                out.append(f"BUY {res.quantity} {res.trading_symbol} ({res.key}, '{res.status}'){note}")
                continue
            # Nothing to add in a BUY decision (the order book shows it) —
            # but after square-off a LISTED, still-open entry is exactly the
            # thing that must be reported until it is cancelled or filled.
            try:
                current = self._entry_reservations.get(res.key)
            except EntryReservationStoreError:
                current = None
            if current is None:
                continue  # released by the reconciliation above
            row = next((o for o in orders if str(o.get("orderid", "")) == current.broker_order_id), None)
            if row is not None and self._order_status(row) in _ORDER_OPEN_STATUSES:
                try:
                    _q, filled_now, unfilled = self._order_quantities(row)
                except (TypeError, ValueError):
                    filled_now, unfilled = 0, current.quantity
                out.append(
                    f"BUY {current.quantity} {current.trading_symbol} ({current.key}): order "
                    f"{current.broker_order_id} is still WORKING at the broker after square-off "
                    f"('{self._order_status(row)}', {filled_now} filled, {unfilled} open) — cancel it or "
                    f"it may fill"
                )
        return out

    def _square_off_locked(self, lock_skipped: str | None = None) -> SquareOffReport:
        report = SquareOffReport(mode=self._mode, live_lock_skipped=lock_skipped)

        if self._mode in (TradingMode.PAPER, TradingMode.DUAL):
            report.paper = self._paper_trader.square_off_intraday()

        if self._mode in (TradingMode.LIVE, TradingMode.DUAL):
            self._square_off_live(report)

        # Entries were closed before anything was read (square_off_intraday);
        # closing again is harmless and covers direct callers of this method.
        self._risk_manager.close_new_entries("intraday square-off done", key="square_off")
        self._close_shared_entries("intraday square-off done")

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
                "live_exits_submitted_outcome_unknown": len(report.live_unknown),
                "live_entries_cancelled": len(report.live_cancelled),
                "live_entries_unreconciled": len(report.live_unreconciled),
                "live_lock_skipped": bool(report.live_lock_skipped),
                "settled": report.settled,
                "live_cancels_filled_then_exited": len(report.cancels_filled),
                "live_cancels_unclear": len(report.cancels_unclear),
                "live_rows_left_alone": report.live_rows_left_alone,
                "live_error": report.live_error,
            },
        )

        return report

    def _close_shared_entries(self, reason: str) -> None:
        """
        Close new entries for today for EVERY instance sharing the
        reservation store — those running now and those started later.
        Failure to write it is audited; the in-process hold still applies.
        """
        if self._mode == TradingMode.PAPER or self._entry_reservations is None:
            return
        today = datetime.now(IST).date()
        try:
            hold = self._entry_reservations.close_entries(today, reason)
        except EntryReservationStoreError as e:
            logger.error(f"Square-off: shared entry hold could NOT be written — {e}")
            self._audit.log(
                AuditEventType.ERROR,
                {"action": "shared_entry_hold_failed", "error": str(e),
                 "note": "other instances sharing the storage are not protected by this square-off"},
            )
            return
        logger.warning(f"🔒 Shared entry hold for {hold.day}: {hold.reason} (by {hold.owner} at {hold.closed_at})")

    def _shared_entry_hold(self) -> tuple[EntryHold | None, str | None]:
        """(today's shared hold or None, error reading it or None)."""
        if self._entry_reservations is None:
            return None, None
        try:
            return self._entry_reservations.entries_closed(datetime.now(IST).date()), None
        except EntryReservationStoreError as e:
            return None, str(e)

    def _entries_closed_rejection(self, order: OrderRequest, hold: EntryHold) -> OrderResult:
        return OrderResult(
            success=False,
            status=OrderStatus.REJECTED,
            message=(
                f"BUY {order.symbol} blocked: new entries closed for today — {hold.reason} "
                f"(square-off by {hold.owner} at {hold.closed_at}; shared entry hold "
                f"{self._entry_reservations.hold_path_for(hold.day) if self._entry_reservations else ''})"
            ),
        )

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

        # Entry submissions the snapshot does not show: reservations with
        # no listed order. They may still appear (and fill) — a flat
        # snapshot is not settled while any exist.
        self._reconcile_entry_reservations(api, orders, report)

        report.broker_flat = not to_exit and not report.live_unclear
        if to_exit:
            for row, net_qty in to_exit:
                blocked = self._existing_exit_for(row, net_qty, orders, api)
                if blocked is not None:
                    report.live.append(blocked)
                    continue
                report.live.append(self._place_live_exit(api, row, net_qty, today))
        else:
            logger.info("Live square-off: broker reports no open intraday quantity")

        # A submission that was in flight at the snapshot may be listed by
        # now: look once more and cancel what appeared. Whatever is still
        # not listed stays reported as unreconciled.
        if report.live_unreconciled:
            self._cancel_late_entries(api, today, report)

    def _reconcile_entry_reservations(self, api: Any, orders: list[dict], report: SquareOffReport) -> None:
        """
        Compare today's (and any earlier day's) unresolved entry
        reservations with the order-book snapshot. A reservation whose
        order is listed is handled by the cancel pass like any other open
        entry. One the snapshot does not show is reported as unreconciled;
        if it carries a broker order id, a cancel is requested by id (best
        effort — the outcome cannot be read back until the broker lists it).
        """
        if self._entry_reservations is None:
            return
        try:
            reservations = self._entry_reservations.unresolved()
        except EntryReservationStoreError as e:
            report.live_unreconciled.append(UnreconciledEntry(
                trading_symbol="?", token="?", exchange="?", quantity=0, reservation_key="?",
                status="?", broker_order_id="", owner="?",
                detail=f"entry reservation records could not be read ({e})",
            ))
            logger.error(f"Live square-off: entry reservation records could not be read: {e}")
            return
        listed = {str(o.get("orderid", "")): o for o in orders}
        for res in reservations:
            if res.acknowledged and res.broker_order_id in listed:
                if self._order_matches_reservation(listed[res.broker_order_id], res) is True:
                    continue  # visible: the cancel pass / exit logic sees it
            why_not: str | None = None
            if not res.acknowledged:
                adopted, why_not = self._tagged_order_for(res, orders)
                if adopted is not None:
                    continue  # visible by a verified tag match: the cancel pass sees it
            cancel_requested = False
            if res.acknowledged and res.broker_order_id in listed:
                row = listed[res.broker_order_id]
                detail = (
                    f"the broker lists order {res.broker_order_id} as {row.get('exchange') or '?'}/"
                    f"{row.get('symboltoken')}/{row.get('producttype')}/{row.get('transactiontype')}, "
                    f"not verifiably {res.exchange}/{res.token}/{res.product_type}/BUY — not trusted"
                )
            elif res.acknowledged:
                detail = (
                    f"acknowledged by the broker but not in the order book at the square-off snapshot"
                )
                try:
                    api.cancelOrder(res.broker_order_id, OrderVariety.NORMAL.value)
                    cancel_requested = True
                except Exception as e:
                    detail += f"; cancel by id failed ({e})"
            else:
                detail = (
                    f"submitted at {res.created_at} but no acknowledgement was stored "
                    f"({res.submission or 'still inside its broker call, or the reply was lost'}) and "
                    + (why_not or f"the order book at the snapshot lists nothing tagged {res.order_tag}")
                )
            entry = UnreconciledEntry(
                trading_symbol=res.trading_symbol, token=res.token, exchange=res.exchange,
                quantity=res.quantity, reservation_key=res.key, status=res.status,
                broker_order_id=res.broker_order_id, owner=res.owner, detail=detail,
                cancel_requested=cancel_requested,
            )
            report.live_unreconciled.append(entry)
            logger.error(f"Live square-off: unreconciled entry — {entry.describe()}")
            self._audit.log(
                AuditEventType.ERROR,
                {"action": "intraday_square_off_entry_unreconciled", "reservation": res.key,
                 "tradingsymbol": res.trading_symbol, "quantity": res.quantity,
                 "broker_order_id": res.broker_order_id, "status": res.status, "owner": res.owner,
                 "detail": detail, "cancel_requested": cancel_requested},
            )

    def _cancel_late_entries(self, api: Any, today: date, report: SquareOffReport) -> None:
        """Re-read the order book; cancel unreconciled entries that are listed open by now."""
        orders = self._read_order_book(api, today)
        if orders is None:
            logger.error("Live square-off: order book could not be re-read for late entries")
            return
        by_id = {str(o.get("orderid", "")): o for o in orders}
        still: list[UnreconciledEntry] = []
        for entry in report.live_unreconciled:
            try:
                res = self._entry_reservations.get(entry.reservation_key) if self._entry_reservations else None
            except EntryReservationStoreError:
                res = None
            row = None
            if res is not None:
                if res.acknowledged:
                    row = by_id.get(res.broker_order_id)
                    if row is not None and self._order_matches_reservation(row, res) is not True:
                        row = None
                else:
                    row, _why = self._tagged_order_for(res, orders)
            if row is None:
                still.append(entry)
                continue
            reason = "entry submitted during square-off appeared after the snapshot"
            if self._order_status(row) in _ORDER_OPEN_STATUSES:
                self._request_cancel(api, row, reason)
                after = self._read_order_book(api, today)
                current = next(
                    (o for o in (after or []) if str(o.get("orderid", "")) == str(row.get("orderid", ""))), None,
                )
                report.live_cancelled.append(self._cancel_outcome(row, current, api, reason))
                logger.warning(
                    f"Live square-off: late entry {row.get('orderid')} ({entry.trading_symbol}) appeared "
                    f"after the snapshot — cancel requested"
                )
            else:
                # terminal already (filled / cancelled): the reservation rules
                # decide its release on the next reconciliation; report it.
                still.append(replace(
                    entry, detail=f"{entry.detail}; now listed '{self._order_status(row)}' — "
                    f"its fill, if any, is exposure after square-off",
                ))
        report.live_unreconciled = still

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
        response, error = self._place_with_broker(api, params)
        result = self._live_placement_result(response, quantity, error)

        if result.success:
            state = ExitState.PLACED
        elif result.status == OrderStatus.UNKNOWN:
            state = ExitState.UNKNOWN  # submitted; may be working: intent kept, nothing else sent
        else:
            state = ExitState.REJECTED
        exit_ = LiveExit(
            trading_symbol=trading_symbol, token=token, exchange=exchange,
            net_qty=net_qty, side=side, state=state, result=result,
            detail=result.message if state == ExitState.UNKNOWN else "",
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
        elif exit_.state == ExitState.UNKNOWN:
            logger.error(f"Live square-off: {exit_.describe()}")
            self._audit.log(
                AuditEventType.ERROR,
                {
                    "action": "intraday_square_off_exit_outcome_unknown",
                    "tradingsymbol": trading_symbol, "token": token, "exchange": exchange,
                    "side": side, "quantity": quantity, "broker_net_qty": net_qty,
                    "detail": result.message,
                    "note": "exit submitted; neither placement nor rejection confirmed — intent kept",
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
            "orders_unknown": self._orders_unknown,
            "fill_rate": f"{(self._orders_filled / max(self._orders_placed, 1)) * 100:.0f}%",
            "paper_stats": self._paper_trader.stats,
            "circuit_breaker": self._circuit_breaker.stats,
        }
