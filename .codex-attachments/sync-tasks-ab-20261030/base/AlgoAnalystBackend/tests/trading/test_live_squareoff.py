"""
LIVE / DUAL mode 15:15 square-off: driven by the broker's position book.

Path: scheduler 15:15 → main.intraday_squareoff() → OrderManager.square_off_intraday()

    PAPER → PaperTrader.square_off_intraday()            (simulated fills)
    LIVE  → broker.position() → one MARKET exit per INTRADAY row with a
            non-zero net quantity, using the broker's own instrument
            details and exact net quantity: SELL closes a long, BUY closes
            a short. Non-INTRADAY rows (DELIVERY) are left alone. The paper
            book is never consulted — after a restart it is empty while the
            broker may still be long.
    DUAL  → both, reported separately in SquareOffReport.

A broker placeOrder() acknowledgement means "accepted into the order book",
not "executed". Live exits are reported as PLACED with placed_qty set and
filled_qty/avg_fill_price 0, audited as ORDER_PLACED — never as a fill.

Restart detection, retries and duplicate-order protection are out of scope
here (next pass).
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, time, timedelta
from pathlib import Path

import pytest
from freezegun import freeze_time

from core.constants import IST, AnalysisAction, OrderStatus, SignalType, TransactionType
from core.security.audit import AuditTrail
from tests.factories import make_analysis_result, make_order_request, make_signal
from trading.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
from trading.entry_reservation_store import EntryReservationStore
from trading.exit_intent_store import ExitIntent, ExitIntentStore, intent_key, order_tag
from trading.order_manager import CancelState, OrderManager, SquareOffReport, TradingMode
from trading.paper_trader import PaperTrader
from trading.risk_manager import RiskConfig, RiskManager

# 15:15 IST on a Wednesday (UTC+5:30) — the square-off slot, inside market hours.
SQUAREOFF_TIME_UTC = "2026-03-04 09:45:00"
CAPITAL = 1_00_00_000.0

RELIANCE = {"tradingsymbol": "RELIANCE-EQ", "symboltoken": "2885", "exchange": "NSE"}
TCS = {"tradingsymbol": "TCS-EQ", "symboltoken": "11536", "exchange": "NSE"}
INFY_BSE = {"tradingsymbol": "INFY", "symboltoken": "500209", "exchange": "BSE"}


def broker_row(instrument: dict, net_qty: int, product: str = "INTRADAY",
               buy_qty: int | None = None, sell_qty: int | None = None) -> dict:
    """
    A SmartAPI position() row (the fields the square-off must use). By
    default the day's buy/sell quantities are consistent with a fresh
    position of net_qty (long: bought net_qty; short: sold |net_qty|).
    """
    if buy_qty is None:
        buy_qty = max(net_qty, 0)
    if sell_qty is None:
        sell_qty = max(-net_qty, 0)
    return {
        **instrument, "producttype": product, "netqty": str(net_qty),
        "buyqty": str(buy_qty), "sellqty": str(sell_qty), "symbolname": "x",
    }


def broker_order(instrument: dict, side: str, qty: int, status: str, filled: int = 0,
                 order_id: str = "OLD1", product: str = "INTRADAY",
                 updatetime: str | None = None) -> dict:
    """A SmartAPI orderBook() row (updatetime as the API formats it: 04-Mar-2026 15:15:02)."""
    row = {
        **instrument, "orderid": order_id, "uniqueorderid": f"U-{order_id}",
        "transactiontype": side, "producttype": product,
        "ordertype": "MARKET", "status": status, "orderstatus": status,
        "quantity": str(qty), "filledshares": str(filled), "unfilledshares": str(qty - filled),
        "averageprice": "2830.0" if filled else "0",
    }
    if updatetime is not None:
        row["updatetime"] = updatetime
    return row


class FakeBroker:
    """
    Stand-in for SmartConnect.

    position()    — the broker's position book (list of rows, or None when flat);
                    rows move when an order fills.
    placeOrder()  — acknowledges with an order id, or rejects symbols in `reject`.
                    An ack means "accepted into the order book", not "executed":
                    the order appears in orderBook() as "open", nothing filled.
    orderBook()   — seeded `orders` plus everything placed; fails when
                    `orderbook_fail`, or from call number `orderbook_fail_after`.
    cancelOrder() — by default the order becomes "cancelled". Per order id:
                    `fill_on_cancel`  — it fills instead (complete, position moves),
                    `cancel_pending`  — it stays "cancel pending",
                    `cancel_fail`     — the API call raises; order stays open.
    """

    def __init__(self, rows: list[dict] | None = None, *, positions_fail: bool = False,
                 reject: set[str] = frozenset(), orders: list[dict] | None = None,
                 orderbook_fail: bool = False, orderbook_fail_after: int | None = None,
                 fill_on_cancel: set[str] = frozenset(), cancel_pending: set[str] = frozenset(),
                 cancel_fail: set[str] = frozenset(), verified_status: dict[str, str] | None = None,
                 verify_fail: bool = False, hide_from_book: set[str] = frozenset(),
                 rendezvous: "tuple[str, threading.Barrier] | None" = None,
                 partial_fill_on_cancel: dict[str, int] | None = None):
        self.rows = [dict(r) for r in rows] if rows else None
        self.positions_fail = positions_fail
        self.reject = set(reject)
        self.orders: list[dict] = [dict(o) for o in (orders or [])]
        self.orderbook_fail = orderbook_fail
        self.orderbook_fail_after = orderbook_fail_after
        self.fill_on_cancel = set(fill_on_cancel)
        # order id -> shares that trade just before the cancel lands (rest cancelled)
        self.partial_fill_on_cancel = dict(partial_fill_on_cancel or {})
        self.cancel_pending = set(cancel_pending)
        self.cancel_fail = set(cancel_fail)
        # individual_order_details(): the broker's CURRENT status per order id,
        # which may differ from a stale order-book row.
        self.verified_status: dict[str, str] = dict(verified_status or {})
        self.verify_fail = verify_fail
        # Orders the broker has accepted but does not list in the order book yet.
        self.hide_from_book = set(hide_from_book)
        # Concurrency probe ("placeOrder", barrier): a caller entering that
        # API waits until a second caller arrives there too, or the barrier
        # times out. Two square-offs that both get past the same point have
        # both decided to act on the same broker state.
        self.rendezvous = rendezvous
        self._lock = threading.Lock()
        self.placed: list[dict] = []
        self.cancel_requests: list[str] = []
        self.verify_requests: list[str] = []
        self.position_calls = 0
        self.orderbook_calls = 0

    def _meet(self, api_name: str) -> None:
        if self.rendezvous is not None and self.rendezvous[0] == api_name:
            try:
                self.rendezvous[1].wait()
            except threading.BrokenBarrierError:
                pass  # nobody else arrived in time: the calls did not overlap here

    # ── books ──
    def position(self):
        self.position_calls += 1
        if self.positions_fail:
            raise RuntimeError("position API unavailable")
        return {"status": True, "message": "SUCCESS", "data": [dict(r) for r in self.rows] if self.rows else None}

    def orderBook(self):
        with self._lock:
            self.orderbook_calls += 1
            calls = self.orderbook_calls
            if self.orderbook_fail or (
                self.orderbook_fail_after is not None and calls > self.orderbook_fail_after
            ):
                raise RuntimeError("order book API unavailable")
            listed = [dict(o) for o in self.orders if o["orderid"] not in self.hide_from_book]
        self._meet("orderBook")
        return {"status": True, "message": "SUCCESS", "data": listed or None}

    # ── orders ──
    def placeOrder(self, params):
        if params["tradingsymbol"] in self.reject:
            return {"status": False, "message": "RMS: order blocked", "data": None}
        self._meet("placeOrder")  # both callers have decided to place before either is recorded
        with self._lock:
            self.placed.append(dict(params))
            order_id = f"BRK{len(self.placed)}"
            return self._record_placed(params, order_id)

    def _record_placed(self, params, order_id):
        self.orders.append({
            "orderid": order_id, "uniqueorderid": f"U-{order_id}", "status": "open", "orderstatus": "open",
            "updatetime": datetime.now(IST).strftime("%d-%b-%Y %H:%M:%S"),  # as SmartAPI formats it
            "transactiontype": params["transactiontype"], "producttype": params["producttype"],
            "tradingsymbol": params["tradingsymbol"], "symboltoken": params["symboltoken"],
            "exchange": params["exchange"], "ordertype": params["ordertype"],
            "quantity": params["quantity"], "filledshares": "0",
            "unfilledshares": params["quantity"], "averageprice": "0",
            "ordertag": params.get("ordertag", ""),  # listed as SmartAPI lists it
        })
        return {"status": True, "data": {"orderid": order_id, "uniqueorderid": f"U-{order_id}"}}

    def cancelOrder(self, order_id, variety="NORMAL"):
        self.cancel_requests.append(order_id)
        if order_id in self.cancel_fail:
            raise RuntimeError("cancel API unavailable")
        order = next((o for o in self.orders if o["orderid"] == order_id), None)
        if order is None:
            return {"status": False, "message": "order not found", "data": None}
        if order_id in self.fill_on_cancel:
            # The cancel request is accepted, but the order fills first: only
            # the order book / position book show what really happened.
            self.fill(order_id)
            return {"status": True, "data": {"orderid": order_id}}
        if order_id in self.partial_fill_on_cancel:
            self.partial_fill(order_id, self.partial_fill_on_cancel[order_id])
        new_status = "cancel pending" if order_id in self.cancel_pending else "cancelled"
        order["status"] = order["orderstatus"] = new_status
        if order_id in self.verified_status:
            self.verified_status[order_id] = new_status  # the broker's current view moves too
        return {"status": True, "data": {"orderid": order_id}}

    def individual_order_details(self, unique_order_id):
        """SmartAPI order-status lookup by uniqueorderid: the current state of one order."""
        self.verify_requests.append(unique_order_id)
        if self.verify_fail:
            raise RuntimeError("order details API unavailable")
        order = next((o for o in self.orders if o.get("uniqueorderid") == unique_order_id), None)
        if order is None or order["orderid"] in self.hide_from_book:
            return {"status": False, "message": "order not found", "data": None}
        current = dict(order)
        override = self.verified_status.get(order["orderid"])
        if override is not None:
            current["status"] = current["orderstatus"] = override
            if override == "complete":
                current["filledshares"], current["unfilledshares"] = current["quantity"], "0"
        return {"status": True, "message": "SUCCESS", "data": current}

    # ── test-side market simulation ──
    def fill(self, order_id: str) -> None:
        """The whole remaining quantity of an order executes; the position book moves."""
        order = next(o for o in self.orders if o["orderid"] == order_id)
        self.partial_fill(order_id, int(order["quantity"]) - int(order["filledshares"]))

    def partial_fill(self, order_id: str, executed: int) -> None:
        """`executed` more shares of an open order trade; complete if nothing is left."""
        order = next(o for o in self.orders if o["orderid"] == order_id)
        qty = int(order["quantity"])
        filled = int(order["filledshares"]) + executed
        assert filled <= qty
        order["filledshares"], order["unfilledshares"], order["averageprice"] = str(filled), str(qty - filled), "2830.0"
        order["status"] = order["orderstatus"] = "complete" if filled == qty else "open"
        self.rows = self.rows or []
        row = next(
            (r for r in self.rows
             if r["symboltoken"] == order["symboltoken"] and r["producttype"] == order["producttype"]),
            None,
        )
        if row is None:
            row = {
                "tradingsymbol": order["tradingsymbol"], "symboltoken": order["symboltoken"],
                "exchange": order["exchange"], "producttype": order["producttype"],
                "netqty": "0", "buyqty": "0", "sellqty": "0", "symbolname": "x",
            }
            self.rows.append(row)
        sign = 1 if order["transactiontype"] == "BUY" else -1
        row["netqty"] = str(int(row["netqty"]) + sign * executed)
        key = "buyqty" if sign > 0 else "sellqty"
        row[key] = str(int(row[key]) + executed)


class FakeLiveSession:
    def __init__(self, broker: FakeBroker | None):
        self._broker = broker

    def get_api(self):
        if self._broker is None:
            raise RuntimeError("not connected")
        return self._broker


# Test hook: production waits up to 30s for the cross-process entry lock; a
# square-off started INSIDE a BUY decision (the overlap tests below) would
# otherwise sit out that whole wait before falling back. The fallback path
# itself is what those tests then exercise.
ENTRY_LOCK_TIMEOUT_S = 0.2


def build(tmp_path, mode: TradingMode, broker: FakeBroker | None, paper_holding: int = 0,
          intents: ExitIntentStore | None = None):
    """
    OrderManager as a freshly restarted process sees it: empty paper book
    unless seeded, in-memory state empty. The exit-intent store and the
    entry-reservation store are on disk (under tmp_path) and survive across
    OrderManagers built on the same tmp_path — that is the "same storage" a
    restarted or second process would share, configured as in production.
    """
    audit = AuditTrail(log_dir=tmp_path / "audit")
    risk = RiskManager(config=RiskConfig(total_capital=CAPITAL), audit=audit)
    cb = CircuitBreaker(config=CircuitBreakerConfig(), audit=audit, total_capital=CAPITAL)
    paper = PaperTrader(capital=CAPITAL, slippage_pct=0.0, audit=audit)
    om = OrderManager(
        risk_manager=risk, circuit_breaker=cb, paper_trader=paper, audit=audit,
        mode=mode, live_session=FakeLiveSession(broker),
        exit_intents=intents if intents is not None else ExitIntentStore(tmp_path / "intents"),
        entry_reservations=EntryReservationStore(
            tmp_path / "entry_reservations", lock_timeout=ENTRY_LOCK_TIMEOUT_S,
        ),
    )
    if paper_holding:
        seeded = paper.place_order(make_order_request(
            transaction_type=TransactionType.BUY, quantity=paper_holding, price=2830.0,
        ))
        assert seeded.success
    else:
        assert paper.positions == []
    return om, paper


def sent(broker: FakeBroker) -> list[tuple[str, str, str, str, str, str]]:
    """(side, tradingsymbol, token, exchange, product, qty) per order the broker received."""
    return [
        (p["transactiontype"], p["tradingsymbol"], p["symboltoken"], p["exchange"],
         p["producttype"], p["quantity"])
        for p in broker.placed
    ]


def audit_events(tmp_path: Path) -> list[dict]:
    events = []
    for f in sorted(Path(tmp_path / "audit").glob("*.jsonl")):
        for line in f.read_text().splitlines():
            line = line.strip()
            if line.startswith("{"):
                events.append(json.loads(line))
    return events


def assert_not_reported_as_fill(result) -> None:
    assert result.success
    assert result.status == OrderStatus.PLACED
    assert result.filled_qty == 0, f"claims filled_qty={result.filled_qty} on a placement ack"
    assert result.avg_fill_price == 0.0, f"claims avg_fill_price={result.avg_fill_price} on a placement ack"
    assert "not confirmed" in result.message.lower(), result.message


# ── LIVE: the broker book drives the square-off ──────────────


@freeze_time(SQUAREOFF_TIME_UTC)
def test_live_squareoff_exits_broker_long_missing_from_paper_book(tmp_path):
    """Broker long 10 RELIANCE MIS, paper book empty (restart): SELL 10 goes to the broker."""
    broker = FakeBroker(rows=[broker_row(RELIANCE, 10)])
    om, paper = build(tmp_path, TradingMode.LIVE, broker)

    report = om.square_off_intraday()

    assert sent(broker) == [("SELL", "RELIANCE-EQ", "2885", "NSE", "INTRADAY", "10")]
    assert isinstance(report, SquareOffReport) and report.mode == TradingMode.LIVE
    assert report.live_error is None
    assert [x.describe() for x in report.live_placed] == [
        "SELL 10 RELIANCE-EQ (placed BRK1 — fill NOT confirmed)"
    ]
    assert report.paper == [] and paper.positions == []  # paper book untouched in LIVE
    assert not report.needs_attention


@freeze_time(SQUAREOFF_TIME_UTC)
def test_live_squareoff_covers_short_with_buy_for_exact_net_quantity(tmp_path):
    """Broker short 25 TCS MIS: the exit is BUY 25."""
    broker = FakeBroker(rows=[broker_row(TCS, -25)])
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    report = om.square_off_intraday()

    assert sent(broker) == [("BUY", "TCS-EQ", "11536", "NSE", "INTRADAY", "25")]
    [x] = report.live_placed
    assert (x.side, x.net_qty, x.result.placed_qty) == ("BUY", -25, 25)


@freeze_time(SQUAREOFF_TIME_UTC)
def test_live_squareoff_uses_broker_instrument_details_and_leaves_delivery_alone(tmp_path):
    """
    Mixed book: RELIANCE long 10 MIS, RELIANCE 300 DELIVERY, INFY (BSE)
    short 5 MIS, TCS MIS net 0. Only the two open MIS rows are exited, each
    with the broker's own symbol/token/exchange; DELIVERY is untouched.
    """
    broker = FakeBroker(rows=[
        broker_row(RELIANCE, 10),
        broker_row(RELIANCE, 300, product="DELIVERY"),
        broker_row(INFY_BSE, -5),
        broker_row(TCS, 0),
    ])
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    report = om.square_off_intraday()

    assert sent(broker) == [
        ("SELL", "RELIANCE-EQ", "2885", "NSE", "INTRADAY", "10"),
        ("BUY", "INFY", "500209", "BSE", "INTRADAY", "5"),
    ]
    assert report.live_rows_left_alone == 1
    assert len(report.live) == 2 and not report.live_rejected


@freeze_time(SQUAREOFF_TIME_UTC)
def test_live_squareoff_with_flat_broker_book_places_nothing(tmp_path):
    broker = FakeBroker(rows=None)  # SmartAPI returns data=None when flat
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    report = om.square_off_intraday()

    assert broker.placed == [] and report.live == [] and report.live_error is None
    assert not report.needs_attention


@freeze_time(SQUAREOFF_TIME_UTC)
def test_live_squareoff_reports_unreadable_broker_book_and_places_nothing(tmp_path):
    broker = FakeBroker(rows=[broker_row(RELIANCE, 10)], positions_fail=True)
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    report = om.square_off_intraday()

    assert broker.placed == []
    assert report.live == []
    assert report.live_error and "position" in report.live_error.lower()
    assert report.needs_attention


@freeze_time(SQUAREOFF_TIME_UTC)
def test_live_squareoff_broker_rejection_is_reported_not_counted_as_closed(tmp_path):
    broker = FakeBroker(rows=[broker_row(RELIANCE, 10), broker_row(TCS, 4)], reject={"TCS-EQ"})
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    report = om.square_off_intraday()

    assert sent(broker) == [("SELL", "RELIANCE-EQ", "2885", "NSE", "INTRADAY", "10")]
    assert [x.trading_symbol for x in report.live_placed] == ["RELIANCE-EQ"]
    [rejected] = report.live_rejected
    assert rejected.trading_symbol == "TCS-EQ" and "RMS" in rejected.result.message
    assert report.needs_attention


@freeze_time(SQUAREOFF_TIME_UTC)
def test_live_squareoff_still_closes_new_entries(tmp_path):
    broker = FakeBroker(rows=None)
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    om.square_off_intraday()

    decision = om._risk_manager.evaluate(
        make_order_request(transaction_type=TransactionType.BUY, quantity=1, price=2830.0), [], "",
    )
    assert not decision.approved and "square" in decision.reason.lower()


# ── Accepted but still open: placement is not a fill ─────────


@freeze_time(SQUAREOFF_TIME_UTC)
def test_squareoff_exit_accepted_but_open_is_not_reported_or_audited_as_filled(tmp_path):
    broker = FakeBroker(rows=[broker_row(RELIANCE, 10)])
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    report = om.square_off_intraday()

    [x] = report.live_placed
    [book_entry] = broker.orderBook()["data"]
    assert (book_entry["orderid"], book_entry["status"], book_entry["filledshares"]) == ("BRK1", "open", "0")
    assert_not_reported_as_fill(x.result)
    assert x.result.placed_qty == 10 and x.result.broker_order_id == "BRK1"

    events = audit_events(tmp_path)
    assert not [e for e in events if e["event_type"] == "ORDER_FILLED"], "placement audited as a fill"
    [placed] = [e for e in events if e["event_type"] == "ORDER_PLACED"]
    assert placed["details"]["placed_qty"] == 10
    assert "fill_price" not in placed["details"] and "quantity" not in placed["details"]
    assert "not confirmed" in placed["details"]["note"]
    [task] = [e for e in events if e["event_type"] == "SCHEDULER_TASK"]
    assert task["details"]["live_exits_placed_unconfirmed"] == 1
    assert "positions_closed" not in task["details"]


@freeze_time(SQUAREOFF_TIME_UTC)
def test_signal_path_live_exit_accepted_but_open_is_not_reported_or_audited_as_filled(tmp_path):
    """Same rule on the ordinary signal path (execute_signal → _execute_live)."""
    broker = FakeBroker(rows=[broker_row(RELIANCE, 10)])
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    result = om.execute_signal(
        make_signal(signal_type=SignalType.SELL, price=2830.0),
        make_analysis_result(action=AnalysisAction.SELL),
    )

    assert sent(broker) == [("SELL", "RELIANCE-EQ", "2885", "NSE", "INTRADAY", "10")]
    assert result is not None
    assert_not_reported_as_fill(result)
    assert result.placed_qty == 10 and not result.is_paper

    events = audit_events(tmp_path)
    assert not [e for e in events if e["event_type"] == "ORDER_FILLED"], "placement audited as a fill"
    placed = [e for e in events if e["event_type"] == "ORDER_PLACED" and e["details"].get("order_id")]
    assert len(placed) == 1 and placed[0]["details"]["placed_qty"] == 10
    assert om.stats["orders_placed"] == 1 and om.stats["orders_filled"] == 0


# ── DUAL: paper and live outcomes stay separate ──────────────


@freeze_time(SQUAREOFF_TIME_UTC)
def test_dual_squareoff_keeps_paper_close_and_live_placement_separate(tmp_path):
    """
    Paper book long 10 (this process's belief), broker long 7 MIS (the
    ledger). Paper closes 10 as a simulated fill; the broker gets SELL 7,
    placed but not confirmed. Neither side is reported in terms of the other.
    """
    broker = FakeBroker(rows=[broker_row(RELIANCE, 7)])
    om, paper = build(tmp_path, TradingMode.DUAL, broker, paper_holding=10)

    report = om.square_off_intraday()

    # Paper side: a genuine simulated fill of what the paper book held.
    [paper_exit] = report.paper_closed
    assert paper_exit.is_paper and paper_exit.status == OrderStatus.FILLED and paper_exit.filled_qty == 10
    assert paper.positions == []

    # Live side: sized from the broker's net quantity, not the paper book; placed only.
    assert sent(broker) == [("SELL", "RELIANCE-EQ", "2885", "NSE", "INTRADAY", "7")]
    [live_exit] = report.live_placed
    assert live_exit.net_qty == 7 and live_exit.result.placed_qty == 7
    assert_not_reported_as_fill(live_exit.result)
    assert not live_exit.result.is_paper

    events = audit_events(tmp_path)
    [task] = [e for e in events if e["event_type"] == "SCHEDULER_TASK"]
    assert task["details"]["paper_positions_closed"] == 1
    assert task["details"]["live_exits_placed_unconfirmed"] == 1


@freeze_time(SQUAREOFF_TIME_UTC)
def test_dual_squareoff_after_restart_paper_empty_live_still_exits_broker_position(tmp_path):
    broker = FakeBroker(rows=[broker_row(RELIANCE, 10)])
    om, _ = build(tmp_path, TradingMode.DUAL, broker, paper_holding=0)

    report = om.square_off_intraday()

    assert report.paper == []
    assert sent(broker) == [("SELL", "RELIANCE-EQ", "2885", "NSE", "INTRADAY", "10")]
    assert len(report.live_placed) == 1


@freeze_time(SQUAREOFF_TIME_UTC)
def test_dual_squareoff_live_failure_does_not_hide_or_undo_the_paper_close(tmp_path):
    broker = FakeBroker(rows=[broker_row(RELIANCE, 10)], positions_fail=True)
    om, paper = build(tmp_path, TradingMode.DUAL, broker, paper_holding=10)

    report = om.square_off_intraday()

    assert len(report.paper_closed) == 1 and paper.positions == []
    assert broker.placed == [] and report.live_error
    assert report.needs_attention


# ── PAPER: unchanged ─────────────────────────────────────────


@freeze_time(SQUAREOFF_TIME_UTC)
def test_paper_squareoff_never_touches_the_broker(tmp_path):
    broker = FakeBroker(rows=[broker_row(RELIANCE, 10)])
    om, paper = build(tmp_path, TradingMode.PAPER, broker, paper_holding=10)

    report = om.square_off_intraday()

    assert broker.position_calls == 0 and broker.placed == []
    assert len(report.paper_closed) == 1 and paper.positions == []
    assert report.live == [] and report.live_error is None and not report.live_attempted


# ── Duplicate exits: the broker's order book is checked first ─
#
# A second square-off call, or a fresh process at 15:16, must not send
# another exit while the broker already has one outstanding for the same
# instrument, product and exit side. If the order book cannot be read, or
# its state is unclear, no exit is sent and the operator is alerted. A
# position only counts as closed when the broker shows no open quantity.


def open_sell_for(instrument: dict, qty: int, filled: int = 0, order_id: str = "OLD1") -> dict:
    return broker_order(instrument, "SELL", qty, "open", filled=filled, order_id=order_id)


@freeze_time(SQUAREOFF_TIME_UTC)
def test_repeated_squareoff_call_does_not_send_a_second_exit_while_first_is_open(tmp_path):
    """Same process, square-off twice: the first SELL 10 is still open at the broker."""
    broker = FakeBroker(rows=[broker_row(RELIANCE, 10)])
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    first = om.square_off_intraday()
    second = om.square_off_intraday()

    assert sent(broker) == [("SELL", "RELIANCE-EQ", "2885", "NSE", "INTRADAY", "10")], (
        f"duplicate exit sent: {sent(broker)}"
    )
    assert len(first.live_placed) == 1
    assert second.live_placed == []
    [outstanding] = second.live_outstanding
    assert outstanding.trading_symbol == "RELIANCE-EQ" and outstanding.existing_order_id == "BRK1"
    assert second.live_error is None
    assert second.needs_attention  # position still open — operator must see it


@freeze_time(SQUAREOFF_TIME_UTC)
def test_restart_at_1516_with_outstanding_exit_sends_nothing(tmp_path):
    """
    Fresh process (empty memory) at 15:16: the broker is still long 10 and
    already has SELL 10 OLD1 open from the previous process's square-off.
    """
    broker = FakeBroker(
        rows=[broker_row(RELIANCE, 10)],
        orders=[open_sell_for(RELIANCE, 10, order_id="OLD1")],
    )
    om, _ = build(tmp_path, TradingMode.LIVE, broker)  # knows nothing about OLD1

    report = om.square_off_intraday()

    assert broker.placed == [], f"duplicate exit sent: {sent(broker)}"
    assert broker.orderbook_calls >= 1
    [outstanding] = report.live_outstanding
    assert outstanding.existing_order_id == "OLD1"
    assert outstanding.open_qty == 10 and outstanding.filled_qty == 0
    assert "OLD1" in outstanding.describe() and "not closed" in outstanding.describe().lower()
    assert report.live_placed == [] and report.live_error is None


@freeze_time(SQUAREOFF_TIME_UTC)
def test_partially_filled_exit_is_not_duplicated_and_position_is_not_counted_closed(tmp_path):
    """SELL 10 filled 4 (6 still open at the broker, net 6 remaining): no new order."""
    broker = FakeBroker(
        rows=[broker_row(RELIANCE, 6, buy_qty=10, sell_qty=4)],
        orders=[open_sell_for(RELIANCE, 10, filled=4, order_id="OLD1")],
    )
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    report = om.square_off_intraday()

    assert broker.placed == [], f"duplicate exit sent: {sent(broker)}"
    [outstanding] = report.live_outstanding
    assert outstanding.net_qty == 6
    assert outstanding.filled_qty == 4 and outstanding.open_qty == 6
    desc = outstanding.describe()
    assert "4/10" in desc and "6" in desc and "not closed" in desc.lower(), desc
    assert report.needs_attention


@freeze_time(SQUAREOFF_TIME_UTC)
def test_unavailable_order_book_sends_no_exit_and_alerts(tmp_path):
    broker = FakeBroker(rows=[broker_row(RELIANCE, 10)], orderbook_fail=True)
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    report = om.square_off_intraday()

    assert broker.placed == [], f"exit sent without a readable order book: {sent(broker)}"
    assert report.live_error and "order book" in report.live_error.lower(), report.live_error
    assert report.live == [] and report.needs_attention


@freeze_time(SQUAREOFF_TIME_UTC)
def test_rejected_and_cancelled_prior_exits_do_not_block_a_fresh_exit(tmp_path):
    broker = FakeBroker(
        rows=[broker_row(RELIANCE, 10)],
        orders=[
            broker_order(RELIANCE, "SELL", 10, "rejected", order_id="OLD1"),
            broker_order(RELIANCE, "SELL", 10, "cancelled", order_id="OLD2"),
        ],
    )
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    report = om.square_off_intraday()

    assert sent(broker) == [("SELL", "RELIANCE-EQ", "2885", "NSE", "INTRADAY", "10")]
    assert len(report.live_placed) == 1 and report.live_outstanding == []


@freeze_time(SQUAREOFF_TIME_UTC)
def test_completed_exit_already_reflected_in_position_does_not_block_a_fresh_exit(tmp_path):
    """
    Earlier today: bought 20, a signal-path SELL 10 completed. The broker's
    position (buy 20 / sell 10 / net 10) already reflects that fill, so the
    remaining 10 needs a fresh exit.
    """
    broker = FakeBroker(
        rows=[broker_row(RELIANCE, 10, buy_qty=20, sell_qty=10)],
        orders=[broker_order(RELIANCE, "SELL", 10, "complete", filled=10, order_id="OLD1")],
    )
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    report = om.square_off_intraday()

    assert sent(broker) == [("SELL", "RELIANCE-EQ", "2885", "NSE", "INTRADAY", "10")]
    assert len(report.live_placed) == 1


@freeze_time(SQUAREOFF_TIME_UTC)
def test_completed_exit_not_yet_reflected_in_position_is_unclear_and_sends_nothing(tmp_path):
    """
    Order book: SELL 10 complete. Position book: buy 10 / sell 0 / net 10 —
    it has not caught up with the fill. Sending another SELL 10 would be a
    guess that could leave a short; send nothing and alert.
    """
    broker = FakeBroker(
        rows=[broker_row(RELIANCE, 10, buy_qty=10, sell_qty=0)],
        orders=[broker_order(RELIANCE, "SELL", 10, "complete", filled=10, order_id="OLD1")],
    )
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    report = om.square_off_intraday()

    assert broker.placed == [], f"guess order sent: {sent(broker)}"
    [unclear] = report.live_unclear
    assert "OLD1" in unclear.describe()
    assert report.needs_attention


@freeze_time(SQUAREOFF_TIME_UTC)
def test_unrecognised_order_status_is_unclear_and_sends_nothing(tmp_path):
    broker = FakeBroker(
        rows=[broker_row(RELIANCE, 10)],
        orders=[broker_order(RELIANCE, "SELL", 10, "something new", order_id="OLD1")],
    )
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    report = om.square_off_intraday()

    assert broker.placed == []
    [unclear] = report.live_unclear
    assert "something new" in unclear.describe()
    assert report.needs_attention


@freeze_time(SQUAREOFF_TIME_UTC)
def test_outstanding_order_must_match_instrument_product_and_exit_side(tmp_path):
    """
    Open orders that are NOT an exit for this position must not block it:
    an open BUY on RELIANCE, an open SELL on TCS, an open DELIVERY SELL on
    RELIANCE. The INTRADAY SELL 10 still goes out; the short TCS gets its
    BUY 5 (an open SELL on TCS is not its exit).
    """
    broker = FakeBroker(
        rows=[broker_row(RELIANCE, 10), broker_row(TCS, -5)],
        orders=[
            broker_order(RELIANCE, "BUY", 5, "open", order_id="OLD1"),
            broker_order(TCS, "SELL", 5, "open", order_id="OLD2"),
            broker_order(RELIANCE, "SELL", 10, "open", order_id="OLD3", product="DELIVERY"),
        ],
    )
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    report = om.square_off_intraday()

    assert sent(broker) == [
        ("SELL", "RELIANCE-EQ", "2885", "NSE", "INTRADAY", "10"),
        ("BUY", "TCS-EQ", "11536", "NSE", "INTRADAY", "5"),
    ]
    assert report.live_outstanding == [] and report.live_unclear == []


@freeze_time(SQUAREOFF_TIME_UTC)
def test_outstanding_exit_blocks_only_its_own_instrument(tmp_path):
    broker = FakeBroker(
        rows=[broker_row(RELIANCE, 10), broker_row(TCS, 4)],
        orders=[open_sell_for(RELIANCE, 10, order_id="OLD1")],
    )
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    report = om.square_off_intraday()

    assert sent(broker) == [("SELL", "TCS-EQ", "11536", "NSE", "INTRADAY", "4")]
    assert [x.trading_symbol for x in report.live_outstanding] == ["RELIANCE-EQ"]
    assert [x.trading_symbol for x in report.live_placed] == ["TCS-EQ"]


@freeze_time(SQUAREOFF_TIME_UTC)
def test_order_book_is_read_even_when_broker_is_flat(tmp_path):
    """A flat position book is not a safe square-off: a pending entry could still fill."""
    broker = FakeBroker(rows=None, orderbook_fail=True)
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    report = om.square_off_intraday()

    assert broker.orderbook_calls == 1
    assert report.live_error and "order book" in report.live_error.lower()
    assert broker.placed == [] and report.needs_attention


@freeze_time(SQUAREOFF_TIME_UTC)
def test_outstanding_exit_is_audited_as_skipped_not_as_placed_or_filled(tmp_path):
    broker = FakeBroker(
        rows=[broker_row(RELIANCE, 10)],
        orders=[open_sell_for(RELIANCE, 10, order_id="OLD1")],
    )
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    om.square_off_intraday()

    events = audit_events(tmp_path)
    assert not [e for e in events if e["event_type"] in ("ORDER_FILLED", "ORDER_PLACED")]
    by_action = {e["details"].get("action"): e["details"] for e in events if e["event_type"] == "SCHEDULER_TASK"}
    skipped = by_action["intraday_square_off_exit_skipped"]
    assert skipped["existing_order_id"] == "OLD1" and skipped["broker_net_qty"] == 10
    summary = by_action["intraday_square_off"]
    assert summary["live_exits_placed_unconfirmed"] == 0
    assert summary["live_exits_outstanding"] == 1


# ── Pending entry orders: a flat book is not a safe square-off ─
#
# An early intraday BUY (or short-opening SELL) can still be open at 15:15
# and fill afterwards. Square-off must inspect the order book even when
# positions are flat, cancel open INTRADAY orders that could add exposure,
# check what happened to each, refresh positions, and exit anything that
# filled meanwhile. Unclear cancellation state → alert, not "complete".


def open_buy(instrument: dict, qty: int, order_id: str = "OLD1") -> dict:
    return broker_order(instrument, "BUY", qty, "open", order_id=order_id)


@freeze_time(SQUAREOFF_TIME_UTC)
def test_pending_entry_buy_is_cancelled_even_when_positions_are_flat(tmp_path):
    broker = FakeBroker(rows=None, orders=[open_buy(RELIANCE, 5, "OLD1")])
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    report = om.square_off_intraday()

    assert broker.cancel_requests == ["OLD1"], f"pending entry left open: {broker.orders}"
    assert broker.placed == []
    [cancelled] = report.live_cancelled
    assert cancelled.order_id == "OLD1" and cancelled.state == CancelState.CANCELLED
    assert cancelled.side == "BUY" and cancelled.qty == 5
    assert report.live_error is None and not report.needs_attention


@freeze_time(SQUAREOFF_TIME_UTC)
def test_pending_short_opening_sell_is_cancelled_when_flat(tmp_path):
    broker = FakeBroker(rows=None, orders=[broker_order(TCS, "SELL", 5, "open", order_id="OLD1")])
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    report = om.square_off_intraday()

    assert broker.cancel_requests == ["OLD1"] and broker.placed == []
    assert [c.state for c in report.live_cancelled] == [CancelState.CANCELLED]


@freeze_time(SQUAREOFF_TIME_UTC)
def test_pending_add_to_long_is_cancelled_and_the_long_is_exited(tmp_path):
    broker = FakeBroker(rows=[broker_row(RELIANCE, 10)], orders=[open_buy(RELIANCE, 5, "OLD1")])
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    report = om.square_off_intraday()

    assert broker.cancel_requests == ["OLD1"]
    assert sent(broker) == [("SELL", "RELIANCE-EQ", "2885", "NSE", "INTRADAY", "10")]
    assert [c.state for c in report.live_cancelled] == [CancelState.CANCELLED]
    assert len(report.live_placed) == 1


@freeze_time(SQUAREOFF_TIME_UTC)
def test_oversized_exit_order_that_would_flip_the_position_is_cancelled_and_replaced(tmp_path):
    """Long 10 with an open SELL 15: 5 of it would open a short. Cancel it, exit exactly 10."""
    broker = FakeBroker(
        rows=[broker_row(RELIANCE, 10)],
        orders=[broker_order(RELIANCE, "SELL", 15, "open", order_id="OLD1")],
    )
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    report = om.square_off_intraday()

    assert broker.cancel_requests == ["OLD1"]
    assert sent(broker) == [("SELL", "RELIANCE-EQ", "2885", "NSE", "INTRADAY", "10")]
    assert report.live_outstanding == []


@freeze_time(SQUAREOFF_TIME_UTC)
def test_exit_sized_open_order_is_left_working_not_cancelled(tmp_path):
    broker = FakeBroker(rows=[broker_row(RELIANCE, 10)], orders=[open_sell_for(RELIANCE, 10, order_id="OLD1")])
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    report = om.square_off_intraday()

    assert broker.cancel_requests == [] and broker.placed == []
    assert [x.existing_order_id for x in report.live_outstanding] == ["OLD1"]


@freeze_time(SQUAREOFF_TIME_UTC)
def test_entry_that_fills_during_cancellation_is_detected_and_exited(tmp_path):
    """
    Flat book, open BUY 5. The cancel request is accepted but the order
    fills first. Square-off must see that in the order book, refresh the
    position book (now long 5) and exit the 5 — not trust the cancel ack.
    """
    broker = FakeBroker(rows=None, orders=[open_buy(RELIANCE, 5, "OLD1")], fill_on_cancel={"OLD1"})
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    report = om.square_off_intraday()

    assert broker.cancel_requests == ["OLD1"]
    assert broker.position_calls >= 2, "positions not refreshed after cancelling"
    assert sent(broker) == [("SELL", "RELIANCE-EQ", "2885", "NSE", "INTRADAY", "5")], (
        f"exposure from the filled entry not exited: {sent(broker)}"
    )
    [cancelled] = report.live_cancelled
    assert cancelled.state == CancelState.FILLED and cancelled.filled_qty == 5
    assert "filled" in cancelled.describe().lower()
    [x] = report.live_placed
    assert x.net_qty == 5 and x.result.placed_qty == 5
    assert report.live_error is None


@freeze_time(SQUAREOFF_TIME_UTC)
def test_cancel_that_stays_pending_is_unclear_and_square_off_is_not_reported_complete(tmp_path):
    broker = FakeBroker(
        rows=[broker_row(RELIANCE, 10)], orders=[open_buy(RELIANCE, 5, "OLD1")], cancel_pending={"OLD1"},
    )
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    report = om.square_off_intraday()

    assert broker.cancel_requests == ["OLD1"]
    [cancelled] = report.live_cancelled
    assert cancelled.state == CancelState.UNCLEAR and "cancel pending" in cancelled.describe()
    assert report.needs_attention
    # The known exposure is still exited — that can only reduce exposure.
    assert sent(broker) == [("SELL", "RELIANCE-EQ", "2885", "NSE", "INTRADAY", "10")]


@freeze_time(SQUAREOFF_TIME_UTC)
def test_cancel_api_failure_leaves_order_unclear_and_alerts(tmp_path):
    broker = FakeBroker(rows=None, orders=[open_buy(RELIANCE, 5, "OLD1")], cancel_fail={"OLD1"})
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    report = om.square_off_intraday()

    assert broker.cancel_requests == ["OLD1"]
    [cancelled] = report.live_cancelled
    assert cancelled.state == CancelState.UNCLEAR
    assert "open" in cancelled.describe().lower()
    assert report.needs_attention and broker.placed == []


@freeze_time(SQUAREOFF_TIME_UTC)
def test_order_book_unreadable_after_cancelling_sends_no_exit_and_alerts(tmp_path):
    broker = FakeBroker(
        rows=[broker_row(RELIANCE, 10)], orders=[open_buy(RELIANCE, 5, "OLD1")], orderbook_fail_after=1,
    )
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    report = om.square_off_intraday()

    assert broker.cancel_requests == ["OLD1"]
    assert broker.placed == [], f"exit placed without re-reading the order book: {sent(broker)}"
    assert report.live_error and "order book" in report.live_error.lower()
    assert report.needs_attention


@freeze_time(SQUAREOFF_TIME_UTC)
def test_cancellations_do_not_touch_delivery_or_terminal_orders(tmp_path):
    broker = FakeBroker(
        rows=None,
        orders=[
            broker_order(RELIANCE, "BUY", 5, "open", order_id="OLD1", product="DELIVERY"),
            broker_order(RELIANCE, "BUY", 5, "complete", filled=5, order_id="OLD2", product="DELIVERY"),
            broker_order(TCS, "BUY", 5, "rejected", order_id="OLD3"),
            broker_order(TCS, "BUY", 5, "cancelled", order_id="OLD4"),
        ],
    )
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    report = om.square_off_intraday()

    assert broker.cancel_requests == [] and broker.placed == []
    assert report.live_cancelled == [] and not report.needs_attention


@freeze_time(SQUAREOFF_TIME_UTC)
def test_cancelled_entry_is_audited_as_cancelled(tmp_path):
    broker = FakeBroker(rows=None, orders=[open_buy(RELIANCE, 5, "OLD1")])
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    om.square_off_intraday()

    events = audit_events(tmp_path)
    [cancelled] = [e for e in events if e["event_type"] == "ORDER_CANCELLED"]
    assert cancelled["details"]["broker_order_id"] == "OLD1"
    summary = next(
        e["details"] for e in events
        if e["event_type"] == "SCHEDULER_TASK" and e["details"].get("action") == "intraday_square_off"
    )
    assert summary["live_entries_cancelled"] == 1 and summary["live_cancels_unclear"] == 0


# ── The real restart path (main.py) with a fake LIVE broker ──
#
# main() is booted against the restart harness with trading_mode=LIVE and
# the broker session handing out a FakeBroker. Every broker call is fake.

from tests.test_mid_session_restart import (  # noqa: E402
    FakeAppConfigStore, FakeSession, at, boot_main_at,
)


SCHEDULED_FOLLOWUPS: list = []  # SquareOffFollowUp instances main() tried to start (thread not spawned)


def boot_live_main(monkeypatch, tmp_path, when, broker: FakeBroker):
    import trading.square_off_followup as followup_module

    monkeypatch.setattr(FakeAppConfigStore, "get", lambda self, key, default=None: (
        "LIVE" if key == "trading_mode" else default
    ))
    monkeypatch.setattr(FakeSession, "get_api", lambda self: broker, raising=False)
    # Never spawn the real follow-up thread from a test; record it so tests can run it synchronously.
    monkeypatch.setattr(
        followup_module.SquareOffFollowUp, "start",
        lambda self: (SCHEDULED_FOLLOWUPS.append(self), None)[1],
    )
    return boot_main_at(monkeypatch, tmp_path, when)


def test_restart_at_1516_in_live_mode_runs_live_squareoff_cancels_pending_entry_and_exits(monkeypatch, tmp_path):
    """
    Fresh process at 15:16, LIVE: broker long 10 RELIANCE MIS with a
    pending BUY 5 still open. The replayed square-off must cancel the BUY,
    exit the 10 at the broker, and report placement (not a fill).
    """
    broker = FakeBroker(rows=[broker_row(RELIANCE, 10)], orders=[open_buy(RELIANCE, 5, "OLD1")])
    h = boot_live_main(monkeypatch, tmp_path, at(15, 16), broker)

    assert h.order_manager.mode == TradingMode.LIVE
    assert h.scheduler.errors == []
    assert h.events.count("scheduler.fire:intraday_squareoff") == 1

    assert broker.cancel_requests == ["OLD1"]
    assert sent(broker) == [("SELL", "RELIANCE-EQ", "2885", "NSE", "INTRADAY", "10")]
    # The tick on socket start could not open a trade (entry gate shut before the feed).
    assert h.order_at_feed_start is not None and not h.order_at_feed_start.success

    squareoff_alerts = [(k, t, m) for k, t, m in h.alerts if "square off" in t.lower()]
    assert squareoff_alerts, h.alerts
    text = " ".join(m for _k, _t, m in squareoff_alerts).lower()
    assert "placed" in text and "not confirmed" in text and "brk1" in text
    assert "cancel" in text and "old1" in text
    assert not any("closed 1" in m.lower() for _k, _t, m in squareoff_alerts)

    # Through close and the evening: no further broker orders.
    h.scheduler.run_until(at(23, 59))
    assert h.scheduler.errors == []
    assert len(broker.placed) == 1 and broker.cancel_requests == ["OLD1"]


def test_restart_at_1530_in_live_mode_sends_no_order(monkeypatch, tmp_path):
    """At/after close nothing is replayed: no cancel, no exit, no feed."""
    broker = FakeBroker(rows=[broker_row(RELIANCE, 10)], orders=[open_buy(RELIANCE, 5, "OLD1")])
    h = boot_live_main(monkeypatch, tmp_path, at(15, 30, 0), broker)

    assert h.order_manager.mode == TradingMode.LIVE
    assert h.events.count("scheduler.fire:intraday_squareoff") == 0
    assert broker.placed == [] and broker.cancel_requests == []
    assert not h.ws.started

    h.scheduler.run_until(at(23, 59))
    assert h.scheduler.errors == []
    assert broker.placed == [] and broker.cancel_requests == []
    assert h.events.count("scheduler.fire:intraday_squareoff") == 0


# ── The exchange is part of the instrument match ─────────────
#
# Tokens are exchange-specific (NSE 2885 and BSE 2885 are different
# instruments) and a trading symbol can repeat across exchanges. An order
# on another exchange must neither block nor be cancelled on behalf of a
# position here. An order whose exchange cannot be verified is UNCLEAR.

BSE_SAME_TOKEN = {"tradingsymbol": "OTHERCO", "symboltoken": "2885", "exchange": "BSE"}
BSE_SAME_SYMBOL = {"tradingsymbol": "RELIANCE-EQ", "symboltoken": "500325", "exchange": "BSE"}
NO_EXCHANGE_SAME_TOKEN = {"tradingsymbol": "RELIANCE-EQ", "symboltoken": "2885", "exchange": ""}


@freeze_time(SQUAREOFF_TIME_UTC)
def test_outstanding_exit_on_another_exchange_with_same_token_or_symbol_does_not_block_this_one(tmp_path):
    """
    NSE RELIANCE long 10 with no exit yet. BSE has two other longs whose
    exits are already working: OTHERCO (BSE token 2885 — same token) and
    RELIANCE-EQ (BSE 500325 — same symbol). The NSE exit must go out; the
    BSE exits stay working, untouched.
    """
    broker = FakeBroker(
        rows=[broker_row(RELIANCE, 10), broker_row(BSE_SAME_TOKEN, 7), broker_row(BSE_SAME_SYMBOL, 3)],
        orders=[
            broker_order(BSE_SAME_TOKEN, "SELL", 7, "open", order_id="OLD1"),
            broker_order(BSE_SAME_SYMBOL, "SELL", 3, "open", order_id="OLD2"),
        ],
    )
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    report = om.square_off_intraday()

    assert sent(broker) == [("SELL", "RELIANCE-EQ", "2885", "NSE", "INTRADAY", "10")], (
        f"NSE exit blocked by an order on another exchange: {sent(broker)}"
    )
    assert broker.cancel_requests == []
    assert sorted(x.existing_order_id for x in report.live_outstanding) == ["OLD1", "OLD2"]
    assert [x.exchange for x in report.live_placed] == ["NSE"]
    assert report.live_unclear == []


@freeze_time(SQUAREOFF_TIME_UTC)
def test_open_order_on_another_exchange_is_judged_against_that_exchanges_position(tmp_path):
    """
    NSE RELIANCE long 10 with its exit OLD1 working. On BSE, token 2885
    (OTHERCO) is flat with an open BUY — an entry, cancelled. The NSE exit
    must not be cancelled as if it belonged to the BSE position, nor
    duplicated.
    """
    broker = FakeBroker(
        rows=[broker_row(RELIANCE, 10)],
        orders=[
            open_sell_for(RELIANCE, 10, order_id="OLD1"),
            broker_order(BSE_SAME_TOKEN, "BUY", 5, "open", order_id="OLD2"),
        ],
    )
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    report = om.square_off_intraday()

    assert broker.cancel_requests == ["OLD2"]
    assert broker.placed == []
    assert [x.existing_order_id for x in report.live_outstanding] == ["OLD1"]


@freeze_time(SQUAREOFF_TIME_UTC)
def test_exit_side_order_with_unverifiable_exchange_is_unclear_not_guessed(tmp_path):
    """Open SELL 10 on token 2885 with no exchange: is it our exit or not? Don't guess."""
    broker = FakeBroker(
        rows=[broker_row(RELIANCE, 10)],
        orders=[broker_order(NO_EXCHANGE_SAME_TOKEN, "SELL", 10, "open", order_id="OLD1")],
    )
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    report = om.square_off_intraday()

    assert broker.placed == [], f"exit guessed despite unverifiable match: {sent(broker)}"
    assert broker.cancel_requests == [], "cancelled an order whose instrument could not be verified"
    [unclear] = report.live_unclear
    assert unclear.trading_symbol == "RELIANCE-EQ" and "exchange" in unclear.detail.lower()
    assert "OLD1" in unclear.detail
    assert report.needs_attention


@freeze_time(SQUAREOFF_TIME_UTC)
def test_entry_side_order_with_unverifiable_exchange_is_cancelled_and_the_exit_proceeds(tmp_path):
    """An open BUY with no exchange is an entry whichever exchange it is on: safe to cancel."""
    broker = FakeBroker(
        rows=[broker_row(RELIANCE, 10)],
        orders=[broker_order(NO_EXCHANGE_SAME_TOKEN, "BUY", 5, "open", order_id="OLD1")],
    )
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    report = om.square_off_intraday()

    assert broker.cancel_requests == ["OLD1"]
    assert sent(broker) == [("SELL", "RELIANCE-EQ", "2885", "NSE", "INTRADAY", "10")]
    assert report.live_unclear == []


@freeze_time(SQUAREOFF_TIME_UTC)
def test_position_row_without_exchange_gets_no_guessed_exit(tmp_path):
    row = broker_row(RELIANCE, 10)
    row["exchange"] = ""
    broker = FakeBroker(rows=[row])
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    report = om.square_off_intraday()

    assert broker.placed == [], f"exit sent with a guessed exchange: {sent(broker)}"
    [unclear] = report.live_unclear
    assert "exchange" in unclear.detail.lower()
    assert report.needs_attention


# ── An exit that fills later: confirm flat on the next check ─


@freeze_time(SQUAREOFF_TIME_UTC)
def test_exit_that_fills_after_placement_is_confirmed_flat_on_next_call_with_no_new_exit(tmp_path):
    broker = FakeBroker(rows=[broker_row(RELIANCE, 10)])
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    first = om.square_off_intraday()
    assert [x.result.broker_order_id for x in first.live_placed] == ["BRK1"]
    assert first.broker_flat is False

    broker.fill("BRK1")  # the market fills it after square-off returned
    position_reads = broker.position_calls

    second = om.square_off_intraday()

    assert broker.position_calls == position_reads + 1, "positions not re-read from the broker"
    assert sent(broker) == [("SELL", "RELIANCE-EQ", "2885", "NSE", "INTRADAY", "10")]  # still just BRK1
    assert second.live == [] and second.live_error is None
    assert second.broker_flat is True
    assert not second.needs_attention


@freeze_time(SQUAREOFF_TIME_UTC)
def test_partially_filled_exit_stays_unresolved_until_the_remainder_fills(tmp_path):
    broker = FakeBroker(rows=[broker_row(RELIANCE, 10)])
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    om.square_off_intraday()            # SELL 10 placed as BRK1
    broker.partial_fill("BRK1", 4)      # 4 trade; 6 still working, broker net 6

    second = om.square_off_intraday()

    assert len(broker.placed) == 1, f"new exit sent on top of the working remainder: {sent(broker)}"
    [outstanding] = second.live_outstanding
    assert (outstanding.existing_order_id, outstanding.net_qty, outstanding.filled_qty, outstanding.open_qty) == ("BRK1", 6, 4, 6)
    assert second.broker_flat is False
    assert second.needs_attention      # unresolved: 6 still open at the broker

    broker.fill("BRK1")                 # the remainder trades

    third = om.square_off_intraday()

    assert len(broker.placed) == 1
    assert third.live == [] and third.broker_flat is True
    assert not third.needs_attention


# ── Overnight: yesterday's exit must not shadow today's ──────
#
# The process may stay up across days. An exit sent yesterday must not make
# today's fresh position in the same instrument look as if it already has
# an exit pending, whether the broker's order book resets overnight (as
# SmartAPI's does) or still lists older orders.

DAY1_1515_UTC = "2026-03-04 09:45:00"   # Wed 04-Mar-2026 15:15 IST
DAY2_1515_UTC = "2026-03-05 09:45:00"   # Thu 05-Mar-2026 15:15 IST


def test_yesterdays_exit_does_not_block_todays_exit_when_broker_book_resets_overnight(tmp_path):
    broker = FakeBroker(rows=[broker_row(RELIANCE, 10)])
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    with freeze_time(DAY1_1515_UTC):
        day1 = om.square_off_intraday()
        assert [x.result.broker_order_id for x in day1.live_placed] == ["BRK1"]
        broker.fill("BRK1")

    # Overnight: the broker's order book starts empty; a new intraday long of 8 opens.
    broker.orders.clear()
    broker.rows = [broker_row(RELIANCE, 8)]

    with freeze_time(DAY2_1515_UTC):
        day2 = om.square_off_intraday()

        assert day2.live_unclear == [], [x.describe() for x in day2.live_unclear]
        assert sent(broker)[-1] == ("SELL", "RELIANCE-EQ", "2885", "NSE", "INTRADAY", "8"), (
            f"today's exit not sent: {sent(broker)}"
        )
        assert [x.result.broker_order_id for x in day2.live_placed] == ["BRK2"]
        assert not day2.needs_attention

        again = om.square_off_intraday()  # same day: BRK2 is still working

        assert len(broker.placed) == 2, f"duplicate exit on day 2: {sent(broker)}"
        assert [x.existing_order_id for x in again.live_outstanding] == ["BRK2"]


def test_older_orders_confirmed_complete_or_cancelled_do_not_block_todays_exit(tmp_path):
    """
    Day 2 with a broker that still lists day-1 rows: yesterday's completed
    SELL 10 and a cancelled SELL 10. Terminal, so they are neither today's
    outstanding exit nor reconciled against today's long 8.
    """
    yesterday = "04-Mar-2026 15:15:02"
    broker = FakeBroker(
        rows=[broker_row(RELIANCE, 8)],
        orders=[
            broker_order(RELIANCE, "SELL", 10, "complete", filled=10, order_id="Y1", updatetime=yesterday),
            broker_order(RELIANCE, "SELL", 10, "cancelled", order_id="Y2", updatetime=yesterday),
        ],
    )
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    with freeze_time(DAY2_1515_UTC):
        report = om.square_off_intraday()

    assert sent(broker) == [("SELL", "RELIANCE-EQ", "2885", "NSE", "INTRADAY", "8")], (
        f"today's exit blocked by yesterday's terminal orders: {sent(broker)}; "
        f"unclear={[x.describe() for x in report.live_unclear]}"
    )
    assert report.live_outstanding == [] and report.live_unclear == []
    assert broker.cancel_requests == [] and broker.verify_requests == []


def test_older_order_still_marked_open_is_verified_and_blocks_while_the_broker_says_open(tmp_path):
    """
    An old-dated row marked open might still execute: its timestamp alone
    is no reason to send another exit. The broker confirms it is still
    open → no new exit, position not closed, operator alerted.
    """
    yesterday = "04-Mar-2026 15:15:02"
    broker = FakeBroker(
        rows=[broker_row(RELIANCE, 8)],
        orders=[broker_order(RELIANCE, "SELL", 8, "open", order_id="Y1", updatetime=yesterday)],
        verified_status={"Y1": "open"},
    )
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    with freeze_time(DAY2_1515_UTC):
        report = om.square_off_intraday()

    assert broker.placed == [], f"exit sent on top of an order that may still execute: {sent(broker)}"
    assert broker.verify_requests == ["U-Y1"]
    assert [x.existing_order_id for x in report.live_outstanding] == ["Y1"]
    assert report.needs_attention


def test_older_order_marked_open_but_verified_complete_does_not_block_todays_exit(tmp_path):
    """Stale book row says open; the broker's current status says complete → today's exit goes out."""
    yesterday = "04-Mar-2026 15:15:02"
    broker = FakeBroker(
        rows=[broker_row(RELIANCE, 8)],
        orders=[broker_order(RELIANCE, "SELL", 10, "open", order_id="Y1", updatetime=yesterday)],
        verified_status={"Y1": "complete"},
    )
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    with freeze_time(DAY2_1515_UTC):
        report = om.square_off_intraday()

    assert broker.verify_requests == ["U-Y1"]
    assert sent(broker) == [("SELL", "RELIANCE-EQ", "2885", "NSE", "INTRADAY", "8")]
    assert report.live_outstanding == [] and report.live_unclear == []


def test_older_order_marked_open_that_cannot_be_verified_is_unclear_no_exit_sent(tmp_path):
    yesterday = "04-Mar-2026 15:15:02"
    broker = FakeBroker(
        rows=[broker_row(RELIANCE, 8)],
        orders=[broker_order(RELIANCE, "SELL", 8, "open", order_id="Y1", updatetime=yesterday)],
        verify_fail=True,
    )
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    with freeze_time(DAY2_1515_UTC):
        report = om.square_off_intraday()

    assert broker.placed == [], f"exit sent without verifying a stale open order: {sent(broker)}"
    assert broker.verify_requests == ["U-Y1"]
    [unclear] = report.live_unclear
    assert "Y1" in unclear.detail and "verif" in unclear.detail.lower()
    assert report.needs_attention


def test_older_open_entry_order_is_verified_and_cancelled_if_still_open(tmp_path):
    """A stale open BUY (entry) is checked the same way; still open → cancelled, not ignored."""
    yesterday = "04-Mar-2026 15:15:02"
    broker = FakeBroker(
        rows=None,
        orders=[broker_order(RELIANCE, "BUY", 5, "open", order_id="Y1", updatetime=yesterday)],
        verified_status={"Y1": "open"},
    )
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    with freeze_time(DAY2_1515_UTC):
        report = om.square_off_intraday()

    assert broker.verify_requests[0] == "U-Y1"       # verified before acting on it...
    assert broker.cancel_requests == ["Y1"]
    assert broker.verify_requests.count("U-Y1") == 2  # ...and again to confirm the cancel outcome
    assert [c.state for c in report.live_cancelled] == [CancelState.CANCELLED]


def test_todays_dated_open_exit_is_still_outstanding(tmp_path):
    """The date filter must not drop today's orders: a dated open exit from 15:15 today blocks."""
    broker = FakeBroker(
        rows=[broker_row(RELIANCE, 10)],
        orders=[open_sell_for(RELIANCE, 10, order_id="T1") | {"updatetime": "05-Mar-2026 15:15:01"}],
    )
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    with freeze_time(DAY2_1515_UTC):
        report = om.square_off_intraday()

    assert broker.placed == []
    assert [x.existing_order_id for x in report.live_outstanding] == ["T1"]


@pytest.mark.parametrize(
    "timestamps",
    [
        {},                                                   # no time fields at all
        {"updatetime": "", "exchtime": ""},                   # empty, as the API sends before exchange ack
        {"updatetime": "not a date"},                         # unreadable
        {"updatetime": "05-Mar-2026 15:15:01"},               # documented format, today
        {"updatetime": "", "exchtime": "05-Mar-2026 15:15:01"},  # documented format via exchtime
    ],
    ids=["missing", "empty", "unreadable", "today-updatetime", "today-exchtime"],
)
def test_open_exit_with_missing_unreadable_or_todays_timestamp_blocks(tmp_path, timestamps):
    """Only a timestamp that clearly says 'an earlier day' changes anything; otherwise today's rules apply."""
    order = open_sell_for(RELIANCE, 10, order_id="T1")
    order.pop("updatetime", None)
    order.update(timestamps)
    broker = FakeBroker(rows=[broker_row(RELIANCE, 10)], orders=[order])
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    with freeze_time(DAY2_1515_UTC):
        report = om.square_off_intraday()

    assert broker.placed == [], f"duplicate exit: {sent(broker)}"
    assert broker.verify_requests == []  # not stale → no status lookup needed
    assert [x.existing_order_id for x in report.live_outstanding] == ["T1"]


# ── Two square-off calls in the same process at nearly the same time ─
#
# The scheduler's 15:15 job and a replayed/operator-triggered square-off
# could overlap. If both read the same position and the same (empty) order
# book before either places, each sends an exit. The broker read and the
# order placement must be one operation within this process.


@freeze_time(SQUAREOFF_TIME_UTC)
def test_two_overlapping_squareoff_calls_send_only_one_exit(tmp_path):
    """
    Both calls read the same long 10 and the same empty order book and both
    decide to exit; the broker holds each inside placeOrder() until the
    other arrives there too. Without serialising read+decide+place, the
    broker receives two exits.
    """
    broker = FakeBroker(
        rows=[broker_row(RELIANCE, 10)],
        rendezvous=("placeOrder", threading.Barrier(2, timeout=0.5)),
    )
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    reports: dict[str, SquareOffReport] = {}
    errors: list[BaseException] = []

    def run(name: str):
        try:
            reports[name] = om.square_off_intraday()
        except BaseException as e:  # pragma: no cover - surfaced below
            errors.append(e)

    threads = [threading.Thread(target=run, args=(n,)) for n in ("scheduler", "replay")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert not [t for t in threads if t.is_alive()], "a square-off call hung"
    assert errors == []

    assert sent(broker) == [("SELL", "RELIANCE-EQ", "2885", "NSE", "INTRADAY", "10")], (
        f"overlapping square-offs each sent an exit: {sent(broker)}"
    )
    placed = [r for r in reports.values() if r.live_placed]
    others = [r for r in reports.values() if not r.live_placed]
    assert len(placed) == 1 and len(others) == 1
    # The second call saw the first one's order and sent nothing new.
    [second] = others
    assert second.live_placed == [] and second.live_error is None
    assert [x.existing_order_id for x in second.live_outstanding] == ["BRK1"]


@freeze_time(SQUAREOFF_TIME_UTC)
def test_second_call_is_safe_while_broker_has_accepted_but_not_yet_listed_the_first_exit(tmp_path):
    """Broker acks BRK1 but its order book does not show it yet: no second exit, position UNCLEAR."""
    broker = FakeBroker(rows=[broker_row(RELIANCE, 10)], hide_from_book={"BRK1"})
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    first = om.square_off_intraday()
    assert [x.result.broker_order_id for x in first.live_placed] == ["BRK1"]

    second = om.square_off_intraday()

    assert len(broker.placed) == 1, f"duplicate exit while BRK1 unlisted: {sent(broker)}"
    [unclear] = second.live_unclear
    assert "BRK1" in unclear.detail and "not in the broker order book" in unclear.detail
    assert second.needs_attention

    broker.hide_from_book.clear()  # the broker catches up
    third = om.square_off_intraday()

    assert len(broker.placed) == 1
    assert [x.existing_order_id for x in third.live_outstanding] == ["BRK1"]


# ── Durable exit intents: restarts and second instances ──────
#
# The in-process lock and memory do not survive a restart and are not
# shared between instances. An exit intent is recorded in a shared on-disk
# store (ExitIntentStore, one file per trading day + instrument + side,
# created atomically) BEFORE placeOrder is called. A restarted or second
# instance finds the unresolved intent and sends nothing until the broker
# lists the order (and it ends) or the position is flat.

DAY2 = datetime(2026, 3, 5, tzinfo=IST).date()
RELIANCE_SELL_KEY = intent_key(DAY2, "NSE", "2885", "SELL")


def unresolved(store: ExitIntentStore) -> list[str]:
    return [i.key for i in store.unresolved_for_day(DAY2)]


def test_restart_before_broker_lists_the_exit_sends_no_duplicate_then_reconciles(tmp_path):
    """
    Process A places the exit; the broker acks BRK1 but does not list it
    yet; process A stops. Process B starts on the same store and must not
    send another exit. Once the broker lists BRK1 it is outstanding; once
    it fills and the position is flat, the intent is resolved.
    """
    store = ExitIntentStore(tmp_path / "intents")
    broker = FakeBroker(rows=[broker_row(RELIANCE, 10)], hide_from_book={"BRK1"})

    with freeze_time(DAY2_1515_UTC):
        process_a, _ = build(tmp_path / "a", TradingMode.LIVE, broker, intents=store)
        first = process_a.square_off_intraday()
        assert [x.result.broker_order_id for x in first.live_placed] == ["BRK1"]
        del process_a  # the process stops here; only the store and the broker survive

        process_b, _ = build(tmp_path / "b", TradingMode.LIVE, broker, intents=store)
        restarted = process_b.square_off_intraday()

        assert len(broker.placed) == 1, f"duplicate exit after restart: {sent(broker)}"
        assert unresolved(store) == [RELIANCE_SELL_KEY]
        [unclear] = restarted.live_unclear
        assert "BRK1" in unclear.detail and "not" in unclear.detail.lower(), unclear.detail
        assert restarted.needs_attention
        assert unresolved(store) == [RELIANCE_SELL_KEY]

        broker.hide_from_book.clear()  # the broker now lists BRK1 (open)
        listed = process_b.square_off_intraday()

        assert len(broker.placed) == 1
        assert [x.existing_order_id for x in listed.live_outstanding] == ["BRK1"]
        assert unresolved(store) == [RELIANCE_SELL_KEY]  # still working

        broker.fill("BRK1")  # executes; position flat
        flat = process_b.square_off_intraday()

        assert len(broker.placed) == 1
        assert flat.broker_flat is True and not flat.needs_attention
        assert unresolved(store) == [], "intent not resolved once the broker shows the position flat"
        history = list((tmp_path / "intents" / "resolved").glob("*.json"))
        assert len(history) == 1 and "flat" in history[0].read_text()


def test_restart_where_broker_lists_the_exit_as_complete_resolves_the_intent(tmp_path):
    store = ExitIntentStore(tmp_path / "intents")
    broker = FakeBroker(rows=[broker_row(RELIANCE, 10)])

    with freeze_time(DAY2_1515_UTC):
        process_a, _ = build(tmp_path / "a", TradingMode.LIVE, broker, intents=store)
        process_a.square_off_intraday()
        broker.fill("BRK1")
        del process_a

        process_b, _ = build(tmp_path / "b", TradingMode.LIVE, broker, intents=store)
        report = process_b.square_off_intraday()

    assert len(broker.placed) == 1
    assert report.broker_flat is True and not report.needs_attention
    assert unresolved(store) == []


@freeze_time(DAY2_1515_UTC)  # one process-wide frozen clock; never freeze per thread
def test_two_instances_at_once_send_only_one_exit(tmp_path):
    """Two OrderManagers (two processes) sharing the store and broker, both reading the same empty book."""
    store = ExitIntentStore(tmp_path / "intents")
    broker = FakeBroker(
        rows=[broker_row(RELIANCE, 10)],
        rendezvous=("orderBook", threading.Barrier(2, timeout=1.0)),
    )
    instances = {
        "one": build(tmp_path / "one", TradingMode.LIVE, broker, intents=store)[0],
        "two": build(tmp_path / "two", TradingMode.LIVE, broker, intents=store)[0],
    }
    reports: dict[str, SquareOffReport] = {}
    errors: list[BaseException] = []

    def run(name: str):
        try:
            reports[name] = instances[name].square_off_intraday()
        except BaseException as e:  # pragma: no cover - surfaced below
            errors.append(e)

    threads = [threading.Thread(target=run, args=(n,)) for n in instances]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert not [t for t in threads if t.is_alive()] and errors == []

    assert sent(broker) == [("SELL", "RELIANCE-EQ", "2885", "NSE", "INTRADAY", "10")], (
        f"two instances each sent an exit: {sent(broker)}"
    )
    winners = [r for r in reports.values() if r.live_placed]
    losers = [r for r in reports.values() if not r.live_placed]
    assert len(winners) == 1 and len(losers) == 1
    [loser] = losers
    assert loser.live_placed == [] and loser.live_error is None
    assert loser.live_unclear or loser.live_outstanding  # saw the other's intent/order; sent nothing
    assert unresolved(store) == [RELIANCE_SELL_KEY]


def test_intent_without_broker_order_id_after_crash_is_unclear_until_the_broker_lists_it(tmp_path):
    """
    Process A recorded the intent and crashed before the broker's ack was
    stored. Process B cannot know whether the order reached the broker:
    nothing is sent. When the broker lists an order carrying the intent's
    tag, it is adopted as the outstanding exit.
    """
    store = ExitIntentStore(tmp_path / "intents")
    with freeze_time(DAY2_1515_UTC):
        assert store.claim(ExitIntent(
            key=RELIANCE_SELL_KEY, day="20260305", exchange="NSE", token="2885", side="SELL",
            trading_symbol="RELIANCE-EQ", quantity=10, order_tag=order_tag(DAY2, "2885", "SELL"),
        ))
    broker = FakeBroker(rows=[broker_row(RELIANCE, 10)])
    om, _ = build(tmp_path / "b", TradingMode.LIVE, broker, intents=store)

    with freeze_time(DAY2_1515_UTC):
        report = om.square_off_intraday()

        assert broker.placed == [], f"guessed exit on top of an unresolved intent: {sent(broker)}"
        [unclear] = report.live_unclear
        assert "intent" in unclear.detail.lower() and "order id" in unclear.detail.lower(), unclear.detail
        assert str(store.path_for(RELIANCE_SELL_KEY)) in unclear.detail  # operator knows what to check
        assert report.needs_attention

        # The order had in fact reached the broker; it now appears, tagged.
        broker.orders.append(
            open_sell_for(RELIANCE, 10, order_id="LATE1") | {"ordertag": order_tag(DAY2, "2885", "SELL")}
        )
        adopted = om.square_off_intraday()

    assert broker.placed == []
    assert [x.existing_order_id for x in adopted.live_outstanding] == ["LATE1"]
    assert store.get(RELIANCE_SELL_KEY).broker_order_id == "LATE1"


def test_intent_without_order_id_is_resolved_when_the_position_is_flat(tmp_path):
    store = ExitIntentStore(tmp_path / "intents")
    with freeze_time(DAY2_1515_UTC):
        store.claim(ExitIntent(
            key=RELIANCE_SELL_KEY, day="20260305", exchange="NSE", token="2885", side="SELL",
            trading_symbol="RELIANCE-EQ", quantity=10, order_tag=order_tag(DAY2, "2885", "SELL"),
        ))
    broker = FakeBroker(rows=None)
    om, _ = build(tmp_path / "b", TradingMode.LIVE, broker, intents=store)

    with freeze_time(DAY2_1515_UTC):
        report = om.square_off_intraday()

    assert broker.placed == [] and report.broker_flat is True
    assert unresolved(store) == []


def test_rejected_exit_resolves_its_intent_so_it_does_not_block_forever(tmp_path):
    store = ExitIntentStore(tmp_path / "intents")
    broker = FakeBroker(rows=[broker_row(RELIANCE, 10)], reject={"RELIANCE-EQ"})
    om, _ = build(tmp_path, TradingMode.LIVE, broker, intents=store)

    with freeze_time(DAY2_1515_UTC):
        report = om.square_off_intraday()

    assert len(report.live_rejected) == 1
    assert unresolved(store) == []
    history = list((tmp_path / "intents" / "resolved").glob("*.json"))
    assert len(history) == 1 and "rejected" in history[0].read_text().lower()


def test_unwritable_intent_store_alerts_and_places_nothing(tmp_path):
    blocker = tmp_path / "intents"
    blocker.write_text("not a directory")  # the store root cannot be created
    broker = FakeBroker(rows=[broker_row(RELIANCE, 10)])
    om, _ = build(tmp_path, TradingMode.LIVE, broker, intents=ExitIntentStore(blocker))

    with freeze_time(DAY2_1515_UTC):
        report = om.square_off_intraday()

    assert broker.placed == [], f"exit placed without a durable intent record: {sent(broker)}"
    assert report.needs_attention
    assert report.live_error and "intent" in report.live_error.lower(), report.live_error


def test_unreadable_intent_record_alerts_and_places_nothing(tmp_path):
    store = ExitIntentStore(tmp_path / "intents")
    store.root.mkdir()
    store.path_for(RELIANCE_SELL_KEY).write_text("{corrupt")
    broker = FakeBroker(rows=[broker_row(RELIANCE, 10)])
    om, _ = build(tmp_path, TradingMode.LIVE, broker, intents=store)

    with freeze_time(DAY2_1515_UTC):
        report = om.square_off_intraday()

    assert broker.placed == []
    assert report.needs_attention
    assert report.live_error and "intent" in report.live_error.lower(), report.live_error


def test_live_squareoff_without_an_intent_store_places_nothing(tmp_path):
    broker = FakeBroker(rows=[broker_row(RELIANCE, 10)])
    audit = AuditTrail(log_dir=tmp_path / "audit")
    om = OrderManager(
        risk_manager=RiskManager(config=RiskConfig(total_capital=CAPITAL), audit=audit),
        circuit_breaker=CircuitBreaker(config=CircuitBreakerConfig(), audit=audit, total_capital=CAPITAL),
        paper_trader=PaperTrader(capital=CAPITAL, slippage_pct=0.0, audit=audit),
        audit=audit, mode=TradingMode.LIVE, live_session=FakeLiveSession(broker), exit_intents=None,
    )

    with freeze_time(DAY2_1515_UTC):
        report = om.square_off_intraday()

    assert broker.placed == [] and report.live_error and "intent" in report.live_error.lower()


def test_yesterdays_unresolved_intent_does_not_block_todays_exit(tmp_path):
    store = ExitIntentStore(tmp_path / "intents")
    day1 = datetime(2026, 3, 4, tzinfo=IST).date()
    with freeze_time(DAY1_1515_UTC):
        store.claim(ExitIntent(
            key=intent_key(day1, "NSE", "2885", "SELL"), day="20260304", exchange="NSE", token="2885",
            side="SELL", trading_symbol="RELIANCE-EQ", quantity=10, order_tag=order_tag(day1, "2885", "SELL"),
            broker_order_id="OLDBRK", status="placed",
        ))
    broker = FakeBroker(rows=[broker_row(RELIANCE, 8)])
    om, _ = build(tmp_path, TradingMode.LIVE, broker, intents=store)

    with freeze_time(DAY2_1515_UTC):
        report = om.square_off_intraday()

    assert sent(broker) == [("SELL", "RELIANCE-EQ", "2885", "NSE", "INTRADAY", "8")]
    assert len(report.live_placed) == 1
    assert unresolved(store) == [RELIANCE_SELL_KEY]


def test_placed_exit_carries_the_intent_tag_and_records_the_broker_ids(tmp_path):
    store = ExitIntentStore(tmp_path / "intents")
    broker = FakeBroker(rows=[broker_row(RELIANCE, 10)])
    om, _ = build(tmp_path, TradingMode.LIVE, broker, intents=store)

    with freeze_time(DAY2_1515_UTC):
        om.square_off_intraday()

    assert broker.placed[0]["ordertag"] == order_tag(DAY2, "2885", "SELL")
    intent = store.get(RELIANCE_SELL_KEY)
    assert intent.status == "placed" and intent.broker_order_id == "BRK1" and intent.unique_order_id == "U-BRK1"


def test_main_restart_on_same_data_dir_finds_the_intent_and_sends_no_duplicate(monkeypatch, tmp_path):
    """
    The real main() path: process A at 15:16 places BRK1 (broker acks, does
    not list it yet) and stops. Process B boots on the same data_dir — the
    store lives at data_dir/square_off_intents — and must send nothing.
    """
    broker = FakeBroker(rows=[broker_row(RELIANCE, 10)], hide_from_book={"BRK1"})
    process_a = boot_live_main(monkeypatch, tmp_path, at(15, 16), broker)
    assert sent(broker) == [("SELL", "RELIANCE-EQ", "2885", "NSE", "INTRADAY", "10")]
    assert broker.placed[0]["ordertag"].startswith("SQ")
    intent_dir = tmp_path / "data" / "square_off_intents"
    assert len(list(intent_dir.glob("*.json"))) == 1
    del process_a

    process_b = boot_live_main(monkeypatch, tmp_path, at(15, 17), broker)

    assert len(broker.placed) == 1, f"duplicate exit after restart: {sent(broker)}"
    assert process_b.scheduler.errors == []
    risk_alerts = [m for k, t, m in process_b.alerts if k == "risk" and "square off" in t.lower()]
    assert risk_alerts and "BRK1" in risk_alerts[0] and "not in the broker order book" in risk_alerts[0]
    assert not any(t == "Square Off" for _k, t, _m in process_b.alerts)  # not reported complete
    assert len(list(intent_dir.glob("*.json"))) == 1  # still unresolved


# ── Signal-path exits and entries vs. square-off ─────────────
#
# A normal LIVE/DUAL SELL (a signal exit) and the square-off are two paths
# to the same broker position. They must share the durable intent check
# and the in-process lock, so neither can blindly send an exit on top of
# the other's. And a BUY approved by the risk gates just before 15:15 must
# be re-checked against the entry gate immediately before placement.


def sell_signal(om: OrderManager):
    return om.execute_signal(
        make_signal(signal_type=SignalType.SELL, price=2830.0),
        make_analysis_result(action=AnalysisAction.SELL),
    )


def buy_signal(om: OrderManager):
    return om.execute_signal(
        make_signal(signal_type=SignalType.BUY, price=2830.0),
        make_analysis_result(action=AnalysisAction.BUY),
    )


@freeze_time(SQUAREOFF_TIME_UTC)
def test_signal_sell_just_before_squareoff_still_unlisted_is_not_duplicated_by_squareoff(tmp_path):
    """15:14:59 signal SELL 10 → BRK1 acked, not in the book yet; 15:15 square-off must not exit again."""
    broker = FakeBroker(rows=[broker_row(RELIANCE, 10)], hide_from_book={"BRK1"})
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    signal_exit = sell_signal(om)
    assert signal_exit is not None and signal_exit.success and signal_exit.broker_order_id == "BRK1"

    report = om.square_off_intraday()

    assert len(broker.placed) == 1, f"square-off duplicated the signal exit: {sent(broker)}"
    assert report.live_placed == [] and report.needs_attention
    assert report.live_unclear or report.live_outstanding

    broker.hide_from_book.clear()
    broker.fill("BRK1")
    after = om.square_off_intraday()
    assert len(broker.placed) == 1 and after.broker_flat is True and not after.needs_attention


@freeze_time(SQUAREOFF_TIME_UTC)
def test_signal_sell_after_squareoff_exit_still_unlisted_is_rejected(tmp_path):
    """Square-off placed BRK1 (unlisted); a SELL signal moments later must not send a second exit."""
    broker = FakeBroker(rows=[broker_row(RELIANCE, 10)], hide_from_book={"BRK1"})
    om, _ = build(tmp_path, TradingMode.LIVE, broker)
    om.square_off_intraday()
    assert len(broker.placed) == 1

    result = sell_signal(om)

    assert len(broker.placed) == 1, f"signal SELL duplicated the square-off exit: {sent(broker)}"
    assert result is not None and not result.success
    assert "BRK1" in result.message or "exit" in result.message.lower(), result.message


@freeze_time(SQUAREOFF_TIME_UTC)
def test_signal_sell_and_squareoff_overlapping_send_only_one_exit(tmp_path):
    broker = FakeBroker(
        rows=[broker_row(RELIANCE, 10)],
        rendezvous=("placeOrder", threading.Barrier(2, timeout=0.5)),
    )
    om, _ = build(tmp_path, TradingMode.LIVE, broker)
    outcomes: dict[str, object] = {}
    errors: list[BaseException] = []

    def run(name, fn):
        try:
            outcomes[name] = fn()
        except BaseException as e:  # pragma: no cover - surfaced below
            errors.append(e)

    threads = [
        threading.Thread(target=run, args=("signal", lambda: sell_signal(om))),
        threading.Thread(target=run, args=("squareoff", lambda: om.square_off_intraday())),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert not [t for t in threads if t.is_alive()] and errors == []

    assert sent(broker) == [("SELL", "RELIANCE-EQ", "2885", "NSE", "INTRADAY", "10")], (
        f"signal SELL and square-off both exited: {sent(broker)}"
    )


@freeze_time(SQUAREOFF_TIME_UTC)
def test_buy_approved_before_squareoff_but_placed_after_is_rejected_at_the_gate(tmp_path, monkeypatch):
    """
    The BUY passes every risk gate at 15:14:59; square-off closes entries
    before the order reaches placement. The gate must be re-checked right
    before the broker call, under the shared lock.
    """
    broker = FakeBroker(rows=None)
    om, _ = build(tmp_path, TradingMode.LIVE, broker)
    real_evaluate = om._risk_manager.evaluate

    reports: list = []

    def evaluate_then_squareoff(*args, **kwargs):
        decision = real_evaluate(*args, **kwargs)
        assert decision.approved, decision.reason
        # 15:15 fires between risk approval and placement. The BUY decision
        # holds the cross-process entry lock, so square-off waits for it,
        # times out (test hook: 0.2s), and runs WITHOUT it — saying so.
        reports.append(om.square_off_intraday())
        return decision

    monkeypatch.setattr(om._risk_manager, "evaluate", evaluate_then_squareoff)

    result = buy_signal(om)

    assert broker.placed == [], f"BUY reached the broker after entries closed: {sent(broker)}"
    assert result is not None and not result.success
    assert "closed" in result.message.lower(), result.message
    [report] = reports
    assert report.live_lock_skipped and "WITHOUT the entry lock" in report.live_lock_skipped
    assert report.needs_attention and not report.settled   # honest: it could not rule out an in-flight entry
    assert report.live_unreconciled == []                   # the BUY never reached reservation, so nothing is


@freeze_time(SQUAREOFF_TIME_UTC)
def test_dual_buy_placed_after_squareoff_is_rejected_on_both_legs(tmp_path, monkeypatch):
    broker = FakeBroker(rows=None)
    om, paper = build(tmp_path, TradingMode.DUAL, broker)
    real_evaluate = om._risk_manager.evaluate

    def evaluate_then_squareoff(*args, **kwargs):
        decision = real_evaluate(*args, **kwargs)
        om.square_off_intraday()
        return decision

    monkeypatch.setattr(om._risk_manager, "evaluate", evaluate_then_squareoff)

    result = buy_signal(om)

    assert broker.placed == []
    assert paper.positions == [], "paper leg opened a position after square-off"
    assert result is not None and not result.success and "closed" in result.message.lower()
    assert result.live_leg is None or not result.live_leg.success


@freeze_time(SQUAREOFF_TIME_UTC)
def test_dual_signal_sell_then_squareoff_keeps_paper_and_live_accurate(tmp_path):
    """Paper long 10, broker long 10. Signal SELL: paper fills, live BRK1 placed (unlisted). Square-off: no duplicate."""
    broker = FakeBroker(rows=[broker_row(RELIANCE, 10)], hide_from_book={"BRK1"})
    om, paper = build(tmp_path, TradingMode.DUAL, broker, paper_holding=10)

    result = sell_signal(om)

    assert result is not None and result.is_paper and result.success and result.filled_qty == 10
    assert paper.positions == []
    assert result.live_leg is not None and result.live_leg.success
    assert result.live_leg.placed_qty == 10 and result.live_leg.filled_qty == 0

    report = om.square_off_intraday()

    assert len(broker.placed) == 1, f"duplicate live exit: {sent(broker)}"
    assert report.paper == []                       # nothing left in the paper book
    assert report.live_placed == [] and report.needs_attention


# ── A signal exit for PART of a holding still working at 15:15 ───
#
# Broker long 500; a signal SELL 176 is still open at square-off. Neither
# "send SELL 500 on top" nor "leave 324 exposed" is acceptable. Square-off
# cancels the working exit, confirms its final status, re-reads the
# position and places one exit for exactly what is still held. If the
# cancel or the position cannot be verified: no speculative exit, alert.

PARTIAL_KEY = intent_key(datetime(2026, 3, 4, tzinfo=IST).date(), "NSE", "2885", "SELL")


def long_500_with_signal_exit_176(tmp_path, **broker_kwargs):
    """Signal SELL sized from capital = 176 against a verified long of 500; BRK1 stays open."""
    broker = FakeBroker(rows=[broker_row(RELIANCE, 500)], **broker_kwargs)
    om, _ = build(tmp_path, TradingMode.LIVE, broker)
    signal_exit = sell_signal(om)
    assert signal_exit is not None and signal_exit.success and signal_exit.placed_qty == 176
    assert sent(broker) == [("SELL", "RELIANCE-EQ", "2885", "NSE", "INTRADAY", "176")]
    return om, broker


@freeze_time(SQUAREOFF_TIME_UTC)
def test_partial_signal_exit_unfilled_is_cancelled_and_replaced_by_a_full_exit(tmp_path):
    om, broker = long_500_with_signal_exit_176(tmp_path)

    report = om.square_off_intraday()

    assert broker.cancel_requests == ["BRK1"], "working partial exit not cancelled"
    assert sent(broker) == [
        ("SELL", "RELIANCE-EQ", "2885", "NSE", "INTRADAY", "176"),
        ("SELL", "RELIANCE-EQ", "2885", "NSE", "INTRADAY", "500"),
    ], f"expected one exit for the full 500 still held: {sent(broker)}"
    [cancelled] = report.live_cancelled
    assert cancelled.order_id == "BRK1" and cancelled.state == CancelState.CANCELLED
    assert "176" in cancelled.describe() and "500" in cancelled.describe()
    [placed] = report.live_placed
    assert placed.net_qty == 500 and placed.result.broker_order_id == "BRK2"
    assert report.live_outstanding == [] and report.live_unclear == []
    intent = om._exit_intents.get(PARTIAL_KEY)
    assert intent is not None and intent.broker_order_id == "BRK2"  # the new exit owns the intent


@freeze_time(SQUAREOFF_TIME_UTC)
def test_partial_signal_exit_that_part_fills_during_cancellation_is_replaced_by_the_remainder(tmp_path):
    """50 of the 176 trade as the cancel lands; the broker is then long 450 → SELL 450."""
    om, broker = long_500_with_signal_exit_176(tmp_path, partial_fill_on_cancel={"BRK1": 50})

    report = om.square_off_intraday()

    assert broker.cancel_requests == ["BRK1"]
    assert sent(broker)[-1] == ("SELL", "RELIANCE-EQ", "2885", "NSE", "INTRADAY", "450"), sent(broker)
    assert len(broker.placed) == 2
    [cancelled] = report.live_cancelled
    assert cancelled.state == CancelState.CANCELLED and cancelled.filled_qty == 50
    [placed] = report.live_placed
    assert placed.net_qty == 450
    assert not report.live_unclear


@freeze_time(SQUAREOFF_TIME_UTC)
def test_partial_signal_exit_that_completes_during_cancellation_is_followed_by_an_exit_for_the_rest(tmp_path):
    """The 176 fill before the cancel lands; the broker is long 324 → SELL 324."""
    om, broker = long_500_with_signal_exit_176(tmp_path, fill_on_cancel={"BRK1"})

    report = om.square_off_intraday()

    assert broker.cancel_requests == ["BRK1"]
    assert sent(broker)[-1] == ("SELL", "RELIANCE-EQ", "2885", "NSE", "INTRADAY", "324"), sent(broker)
    assert len(broker.placed) == 2
    [cancelled] = report.live_cancelled
    assert cancelled.state == CancelState.FILLED and cancelled.filled_qty == 176
    [placed] = report.live_placed
    assert placed.net_qty == 324
    assert not report.live_unclear


@pytest.mark.parametrize("uncertain", ["cancel_pending", "cancel_fail"])
@freeze_time(SQUAREOFF_TIME_UTC)
def test_partial_signal_exit_whose_cancellation_stays_uncertain_gets_no_speculative_exit(tmp_path, uncertain):
    om, broker = long_500_with_signal_exit_176(tmp_path, **{uncertain: {"BRK1"}})

    report = om.square_off_intraday()

    assert broker.cancel_requests == ["BRK1"]
    assert len(broker.placed) == 1, f"speculative exit sent with BRK1 unresolved: {sent(broker)}"
    [cancelled] = report.live_cancelled
    assert cancelled.state == CancelState.UNCLEAR
    [unclear] = report.live_unclear
    assert "324" in unclear.detail and "176" in unclear.detail, unclear.detail
    assert "cancel" in unclear.detail.lower()
    assert report.live_outstanding == []
    assert report.needs_attention
    assert om._exit_intents.get(PARTIAL_KEY).broker_order_id == "BRK1"  # intent stays with the working order


@freeze_time(SQUAREOFF_TIME_UTC)
def test_exit_sized_working_order_is_still_left_alone_not_cancelled(tmp_path):
    """Long 10 with its own SELL 10 working: nothing to replace."""
    broker = FakeBroker(rows=[broker_row(RELIANCE, 10)])
    om, _ = build(tmp_path, TradingMode.LIVE, broker)
    assert sell_signal(om).success  # SELL capped to 10 = the whole holding

    report = om.square_off_intraday()

    assert broker.cancel_requests == [] and len(broker.placed) == 1
    assert [x.existing_order_id for x in report.live_outstanding] == ["BRK1"]


# ── Follow-up: did the broker position actually become flat? ─
#
# An exit is reported PLACED at 15:15, but nothing checked later whether
# the broker position became flat. A follow-up check runs after square-off
# without blocking the scheduler: read-only (it never places an order),
# reports "flat" only if the broker position confirms it, otherwise shows
# exactly what is still open / working / rejected / unreadable and alerts.


class RecordingNotifier:
    def __init__(self):
        self.alerts: list[tuple[str, str, str]] = []

    def system_alert(self, title, message, **kwargs):
        self.alerts.append(("system", title, message))

    def risk_alert(self, title, details):
        self.alerts.append(("risk", title, details))


@freeze_time(SQUAREOFF_TIME_UTC)
def test_followup_confirms_flat_only_when_the_broker_position_is_flat(tmp_path):
    broker = FakeBroker(rows=[broker_row(RELIANCE, 10)])
    om, _ = build(tmp_path, TradingMode.LIVE, broker)
    om.square_off_intraday()  # SELL 10 → BRK1, placed

    still_open = om.confirm_square_off()
    assert still_open.flat is False and not still_open.confirmed_flat
    [pos] = still_open.open_positions
    assert (pos.trading_symbol, pos.net_qty, pos.exit_order_id, pos.exit_status) == ("RELIANCE-EQ", 10, "BRK1", "open")
    assert pos.unfilled == 10 and "open" in pos.describe().lower()

    broker.fill("BRK1")
    confirmed = om.confirm_square_off()

    assert confirmed.flat is True and confirmed.confirmed_flat
    assert confirmed.open_positions == [] and confirmed.error is None
    assert len(broker.placed) == 1, f"follow-up placed an order: {sent(broker)}"
    assert unresolved_today(om) == []  # intent resolved on confirmed flat


def unresolved_today(om: OrderManager) -> list[str]:
    return [i.key for i in om._exit_intents.unresolved_for_day(datetime.now(IST).date())]


@freeze_time(SQUAREOFF_TIME_UTC)
def test_followup_reports_a_partially_filled_exit_as_still_open(tmp_path):
    broker = FakeBroker(rows=[broker_row(RELIANCE, 10)])
    om, _ = build(tmp_path, TradingMode.LIVE, broker)
    om.square_off_intraday()
    broker.partial_fill("BRK1", 4)

    check = om.confirm_square_off()

    assert check.flat is False
    [pos] = check.open_positions
    assert (pos.net_qty, pos.exit_order_id, pos.filled, pos.unfilled) == (6, "BRK1", 4, 6)
    assert "4/10" in pos.describe() and "6" in pos.describe()
    assert len(broker.placed) == 1


@freeze_time(SQUAREOFF_TIME_UTC)
def test_followup_reports_a_rejected_exit_with_the_position_still_open(tmp_path):
    broker = FakeBroker(rows=[broker_row(RELIANCE, 10)], reject={"RELIANCE-EQ"})
    om, _ = build(tmp_path, TradingMode.LIVE, broker)
    report = om.square_off_intraday()
    assert report.live_rejected

    check = om.confirm_square_off()

    assert check.flat is False
    [pos] = check.open_positions
    assert pos.exit_order_id == "" and "no exit" in pos.describe().lower()
    assert broker.placed == []  # the follow-up does not retry


@freeze_time(SQUAREOFF_TIME_UTC)
def test_followup_with_unreadable_broker_books_reports_uncertainty_not_flat(tmp_path):
    broker = FakeBroker(rows=[broker_row(RELIANCE, 10)])
    om, _ = build(tmp_path, TradingMode.LIVE, broker)
    om.square_off_intraday()

    broker.positions_fail = True
    check = om.confirm_square_off()
    assert check.flat is None and not check.confirmed_flat
    assert check.error and "position" in check.error.lower()

    broker.positions_fail = False
    broker.orderbook_fail = True
    check = om.confirm_square_off()
    assert check.flat is False and not check.confirmed_flat
    assert check.error and "order book" in check.error.lower()
    [pos] = check.open_positions
    assert pos.exit_order_id == "" and "unknown" in pos.describe().lower()
    assert len(broker.placed) == 1


@freeze_time(SQUAREOFF_TIME_UTC)
def test_followup_never_places_an_order_even_with_no_exit_working(tmp_path):
    broker = FakeBroker(rows=[broker_row(RELIANCE, 10)])
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    check = om.confirm_square_off()  # no square-off ran: position open, nothing working

    assert broker.placed == [] and broker.cancel_requests == []
    assert check.flat is False and check.open_positions[0].exit_order_id == ""


# ── The runner: retries, then one clear alert; never blocks the caller ─


def make_runner(om, notifier, *, now, wait):
    from trading.square_off_followup import SquareOffFollowUp
    return SquareOffFollowUp(
        om, notifier, delay_s=60, interval_s=60, max_attempts=3, now=now, wait=wait,
    )


@freeze_time(SQUAREOFF_TIME_UTC)
def test_followup_runner_alerts_flat_once_the_broker_confirms_it(tmp_path):
    broker = FakeBroker(rows=[broker_row(RELIANCE, 10)])
    om, _ = build(tmp_path, TradingMode.LIVE, broker)
    om.square_off_intraday()
    notifier = RecordingNotifier()
    waits: list[float] = []

    def wait(seconds):
        waits.append(seconds)
        if len(waits) == 2:
            broker.fill("BRK1")  # fills while the runner is waiting for the second attempt

    runner = make_runner(om, notifier, now=lambda: datetime.now(IST), wait=wait)
    outcome = runner.run()

    assert outcome.confirmed_flat
    assert waits == [60, 60]
    assert [(k, t) for k, t, _m in notifier.alerts] == [("system", "Square Off — broker confirmed flat")]
    assert len(broker.placed) == 1


@freeze_time(SQUAREOFF_TIME_UTC)
def test_followup_runner_alerts_not_flat_after_its_attempts(tmp_path):
    broker = FakeBroker(rows=[broker_row(RELIANCE, 10)])
    om, _ = build(tmp_path, TradingMode.LIVE, broker)
    om.square_off_intraday()
    broker.partial_fill("BRK1", 4)
    notifier = RecordingNotifier()

    runner = make_runner(om, notifier, now=lambda: datetime.now(IST), wait=lambda s: None)
    outcome = runner.run()

    assert not outcome.confirmed_flat
    assert len(broker.placed) == 1
    [(kind, title, message)] = notifier.alerts
    assert kind == "risk" and "NOT confirmed flat" in title
    assert "RELIANCE-EQ" in message and "BRK1" in message and "4/10" in message and "6" in message


@freeze_time(SQUAREOFF_TIME_UTC)
def test_followup_runner_stops_at_the_deadline_and_alerts(tmp_path):
    """Square-off replayed late (15:28): the runner checks but gives up by 15:29:30, never past close."""
    broker = FakeBroker(rows=[broker_row(RELIANCE, 10)])
    om, _ = build(tmp_path, TradingMode.LIVE, broker)
    om.square_off_intraday()
    notifier = RecordingNotifier()
    clock = {"now": datetime(2026, 3, 4, 15, 28, 0, tzinfo=IST)}

    def wait(seconds):
        clock["now"] = clock["now"] + timedelta(seconds=seconds)

    runner = make_runner(om, notifier, now=lambda: clock["now"], wait=wait)
    outcome = runner.run()

    assert not outcome.confirmed_flat
    assert clock["now"].time() <= time(15, 30)
    assert [k for k, _t, _m in notifier.alerts] == ["risk"]
    assert len(broker.placed) == 1


@freeze_time(SQUAREOFF_TIME_UTC)
def test_followup_runner_does_not_block_its_caller(tmp_path):
    broker = FakeBroker(rows=[broker_row(RELIANCE, 10)])
    om, _ = build(tmp_path, TradingMode.LIVE, broker)
    om.square_off_intraday()
    notifier = RecordingNotifier()
    started = threading.Event()
    release = threading.Event()

    def wait(_seconds):
        started.set()
        release.wait(timeout=5)

    runner = make_runner(om, notifier, now=lambda: datetime.now(IST), wait=wait)
    thread = runner.start()

    assert started.wait(timeout=5)         # the runner is waiting in the background...
    assert thread.is_alive() and thread.daemon
    broker.fill("BRK1")
    release.set()                          # ...while the caller carried on
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert [(k, t) for k, t, _m in notifier.alerts] == [("system", "Square Off — broker confirmed flat")]


# ── Through main(): normal day and restarts ──────────────────


def capture_followups(monkeypatch) -> list:
    """The follow-ups main() schedules from here on (boot_live_main stops the thread being spawned)."""
    SCHEDULED_FOLLOWUPS.clear()
    return SCHEDULED_FOLLOWUPS


def test_main_normal_day_schedules_followup_after_1515_and_reports_flat_only_when_confirmed(monkeypatch, tmp_path):
    scheduled = capture_followups(monkeypatch)
    broker = FakeBroker(rows=[broker_row(RELIANCE, 10)])
    h = boot_live_main(monkeypatch, tmp_path, at(11, 0), broker)
    assert scheduled == []

    h.scheduler.run_until(at(15, 20))

    assert h.events.count("scheduler.fire:intraday_squareoff") == 1
    assert len(scheduled) == 1, "no follow-up check scheduled after square-off"
    exits = [p for p in broker.placed if p["transactiontype"] == "SELL"]
    assert [(p["tradingsymbol"], p["quantity"]) for p in exits] == [("RELIANCE-EQ", "10")]
    placed_before = len(broker.placed)

    followup = scheduled[0]
    followup._wait = lambda s: None
    outcome = followup.run()                       # exit still open
    assert not outcome.confirmed_flat
    assert any(k == "risk" and "NOT confirmed flat" in t for k, t, _m in h.alerts), h.alerts
    assert not any("confirmed flat" in t and k == "system" for k, t, _m in h.alerts)

    broker.fill(exits[0] and [o for o in broker.orders if o["transactiontype"] == "SELL"][0]["orderid"])
    outcome = followup.run()
    assert outcome.confirmed_flat
    assert any(k == "system" and "broker confirmed flat" in t for k, t, _m in h.alerts)
    assert len(broker.placed) == placed_before     # the follow-up never placed anything

    h.scheduler.run_until(at(23, 59))
    assert h.scheduler.errors == [] and len(broker.placed) == placed_before


def test_main_restart_at_1516_schedules_followup_and_1530_does_not(monkeypatch, tmp_path):
    scheduled = capture_followups(monkeypatch)

    broker = FakeBroker(rows=[broker_row(RELIANCE, 10)])
    h = boot_live_main(monkeypatch, tmp_path / "a", at(15, 16), broker)
    assert len(scheduled) == 1 and sent(broker) == [("SELL", "RELIANCE-EQ", "2885", "NSE", "INTRADAY", "10")]
    followup = scheduled[0]
    followup._wait = lambda s: None
    outcome = followup.run()
    assert not outcome.confirmed_flat and len(broker.placed) == 1
    assert any(k == "risk" and "NOT confirmed flat" in t for k, t, _m in h.alerts)

    broker2 = FakeBroker(rows=[broker_row(RELIANCE, 10)])
    h2 = boot_live_main(monkeypatch, tmp_path / "b", at(15, 30, 0), broker2)
    h2.scheduler.run_until(at(23, 59))
    assert scheduled[1:] == [], "follow-up scheduled by a restart at/after close"
    assert broker2.placed == [] and broker2.cancel_requests == []
    assert h2.events.count("scheduler.fire:intraday_squareoff") == 0
