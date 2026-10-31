"""
LIVE / DUAL BUY: an order the broker accepted must not be sent again just
because it has not appeared in the order book yet.

The gap: the broker may acknowledge a BUY and only list it a moment later
(or the reply may be lost after the broker recorded it). A repeated signal
in that window — from this process or a fresh one after a restart — reads a
position book and an order book that show nothing, and would buy again.

Rule: before placeOrder, the entry is *reserved* durably (instrument,
quantity, and the price estimate used for sizing). The reservation is kept
after an acknowledgement, a timeout or an unclear reply, and it is recovered
by any later check that shares the storage. A reservation that cannot be
saved means the order is not submitted. A reservation with no broker row is
still exposure: a missing row is not proof of rejection.

Acknowledgements are recorded in the SDK's real shapes: SmartConnect
.placeOrder() returns the order id as a STRING on success and None on
failure; .placeOrderFullResponse() returns the full dict; transport
errors raise. Acknowledged orders are PLACED, never reported as filled.

No real orders are placed: FakeBroker stands in for SmartConnect.
"""

from __future__ import annotations

from freezegun import freeze_time

from core.constants import AnalysisAction, OrderStatus, SignalType
from core.security.audit import AuditTrail
from tests.factories import make_analysis_result, make_signal
from trading.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
from trading.entry_reservation_store import EntryReservationStore, reservation_key
from trading.exit_intent_store import ExitIntentStore
from trading.order_manager import OrderManager, TradingMode
from trading.paper_trader import PaperTrader
from trading.risk_manager import RiskConfig, RiskManager

SESSION_TIME_UTC = "2026-03-04 04:30:00"  # 10:00 IST, Wednesday
CAPITAL = 1_00_00_000.0
PRICE = 2830.0
KEY = reservation_key("20260304", "NSE", "2885", "INTRADAY")


class FakeBroker:
    """
    Stand-in for SmartConnect.

    placeOrder()/placeOrderFullResponse() record the order at the broker
    and answer in the shape selected by `reply`:
        "dict"       full-response dict (what placeOrderFullResponse returns)
        "str"        the order id string (what the real placeOrder returns)
        "none"       None (the SDK's answer to a rejection it swallowed)
        "lost"       the broker records the order, then the reply is lost (raises)
        "down"       the request never reaches the broker (raises, nothing recorded)
        "reject"     {"status": False, "message": ...}
    Orders in `hidden` are accepted but not listed by orderBook() or
    individual_order_details() yet.
    """

    def __init__(self, *, reply: str = "dict", hidden: set[str] = frozenset(),
                 has_full_response: bool = True):
        self.reply = reply
        self.hidden = set(hidden)
        self.rows: list[dict] = []
        self.orders: list[dict] = []
        self.placed: list[dict] = []
        if not has_full_response:
            self.placeOrderFullResponse = None  # an older SDK without it

    def position(self):
        return {"status": True, "message": "SUCCESS", "data": list(self.rows) or None}

    def orderBook(self):
        listed = [dict(o) for o in self.orders if o["orderid"] not in self.hidden]
        return {"status": True, "message": "SUCCESS", "data": listed or None}

    def individual_order_details(self, unique_id):
        order = next((o for o in self.orders if o["uniqueorderid"] == unique_id), None)
        if order is None or order["orderid"] in self.hidden:
            return {"status": False, "message": "order not found", "data": None}
        return {"status": True, "message": "SUCCESS", "data": dict(order)}

    # ── placing ──

    def _accept(self, params) -> str:
        self.placed.append(dict(params))
        order_id = f"BRK{len(self.placed)}"
        self.orders.append({
            "orderid": order_id, "uniqueorderid": f"U-{order_id}", "status": "open", "orderstatus": "open",
            "transactiontype": params["transactiontype"], "producttype": params["producttype"],
            "tradingsymbol": params["tradingsymbol"], "symboltoken": params["symboltoken"],
            "exchange": params["exchange"], "ordertype": params["ordertype"],
            "quantity": params["quantity"], "filledshares": "0", "unfilledshares": params["quantity"],
            # a MARKET order is listed with price 0 — only a LIMIT order carries its price
            "price": params["price"] if params["ordertype"] == "LIMIT" else "0",
            "averageprice": "0", "ordertag": params.get("ordertag", ""),
        })
        return order_id

    def placeOrder(self, params):
        if self.reply == "down":
            raise TimeoutError("read timed out")
        if self.reply == "reject":
            return {"status": False, "message": "AB1010 RMS: margin shortfall", "data": None}
        if self.reply == "none":
            return None  # the SDK logged the rejection and returned None
        order_id = self._accept(params)
        if self.reply == "lost":
            raise TimeoutError("read timed out")  # accepted at the broker, reply never arrived
        if self.reply == "str":
            return order_id
        return {"status": True, "message": "SUCCESS", "errorcode": "",
                "data": {"script": params["tradingsymbol"], "orderid": order_id, "uniqueorderid": f"U-{order_id}"}}

    def placeOrderFullResponse(self, params):
        return self.placeOrder(params)

    # ── test-side market simulation ──

    def fill(self, order_id: str) -> None:
        order = next(o for o in self.orders if o["orderid"] == order_id)
        order["status"] = order["orderstatus"] = "complete"
        order["filledshares"], order["unfilledshares"] = order["quantity"], "0"
        order["averageprice"] = str(PRICE)
        self.rows.append({
            "tradingsymbol": order["tradingsymbol"], "symboltoken": order["symboltoken"],
            "exchange": order["exchange"], "producttype": order["producttype"],
            "netqty": order["quantity"], "buyqty": order["quantity"], "sellqty": "0",
            "buyavgprice": str(PRICE),
        })

    def exit_all(self) -> None:
        for r in self.rows:
            r["netqty"] = "0"


class FakeLiveSession:
    def __init__(self, broker):
        self._broker = broker

    def get_api(self):
        return self._broker


def build(tmp_path, broker, mode=TradingMode.LIVE, reservations=None):
    audit = AuditTrail(log_dir=tmp_path / "audit")
    risk = RiskManager(config=RiskConfig(total_capital=CAPITAL), audit=audit)
    cb = CircuitBreaker(config=CircuitBreakerConfig(), audit=audit, total_capital=CAPITAL)
    paper = PaperTrader(capital=CAPITAL, slippage_pct=0.0, audit=audit)
    om = OrderManager(
        risk_manager=risk, circuit_breaker=cb, paper_trader=paper, audit=audit,
        mode=mode, live_session=FakeLiveSession(broker),
        exit_intents=ExitIntentStore(tmp_path / "exit_intents"),
        entry_reservations=reservations if reservations is not None
        else EntryReservationStore(tmp_path / "entry_reservations"),
    )
    return om


def buy(om):
    return om.execute_signal(
        make_signal(signal_type=SignalType.BUY, price=PRICE),
        make_analysis_result(action=AnalysisAction.BUY),
    )


def sent(broker):
    return [(p["transactiontype"], p["quantity"]) for p in broker.placed]


# ── The gap: accepted but not listed yet ─────────────────────


@freeze_time(SESSION_TIME_UTC)
def test_accepted_buy_hidden_from_the_order_book_is_not_sent_again(tmp_path):
    """BRK1 accepted; the broker has not listed it yet. A repeated signal must not buy again."""
    broker = FakeBroker(hidden={"BRK1"})
    om = build(tmp_path, broker)

    first = buy(om)
    assert first is not None and first.success and first.status == OrderStatus.PLACED
    assert first.broker_order_id == "BRK1" and first.filled_qty == 0

    second = buy(om)

    assert sent(broker) == [("BUY", "176")], f"bought again while BRK1 was unlisted: {sent(broker)}"
    assert second is not None and not second.success
    assert "BRK1" in second.message and "not in the" in second.message.lower(), second.message
    assert "not proof" in second.message.lower(), second.message


@freeze_time(SESSION_TIME_UTC)
def test_reply_lost_after_the_broker_accepted_keeps_the_reservation_and_blocks_a_repeat(tmp_path):
    """placeOrder times out after the broker recorded BRK1. Outcome unknown → reservation kept, no repeat."""
    broker = FakeBroker(reply="lost", hidden={"BRK1"})
    om = build(tmp_path, broker)

    first = buy(om)

    assert first is not None and not first.success
    assert first.status == OrderStatus.UNKNOWN, first.status
    assert "timed out" in first.message and "reserv" in first.message.lower(), first.message
    reservation = om._entry_reservations.get(KEY)
    assert reservation is not None and reservation.status == "unclear"
    assert reservation.quantity == 176 and reservation.price_estimate == PRICE

    second = buy(om)

    assert sent(broker) == [("BUY", "176")], f"bought again after an unclear submission: {sent(broker)}"
    assert second is not None and not second.success
    assert "reserv" in second.message.lower() and "not proof" in second.message.lower(), second.message


@freeze_time(SESSION_TIME_UTC)
def test_lost_reply_is_adopted_once_the_broker_lists_the_tagged_order(tmp_path):
    """The lost-reply order shows up in the order book later: it is recognised as ours and counted once."""
    broker = FakeBroker(reply="lost")  # recorded at the broker and listed; only the reply was lost
    om = build(tmp_path, broker)
    first = buy(om)
    assert first is not None and first.status == OrderStatus.UNKNOWN

    second = buy(om)

    assert sent(broker) == [("BUY", "176")]
    assert second is not None and not second.success
    assert "BRK1" in second.message, second.message
    reservation = om._entry_reservations.get(KEY)
    assert reservation is not None and reservation.status == "placed" and reservation.broker_order_id == "BRK1"


@freeze_time(SESSION_TIME_UTC)
def test_fresh_manager_with_the_same_storage_does_not_repeat_an_unlisted_buy(tmp_path):
    """Process A places BRK1 (unlisted). Process B starts with the same storage and gets the same signal."""
    broker = FakeBroker(hidden={"BRK1"})
    manager_a = build(tmp_path / "a", broker, reservations=EntryReservationStore(tmp_path / "shared"))
    first = buy(manager_a)
    assert first is not None and first.success

    manager_b = build(tmp_path / "b", broker, reservations=EntryReservationStore(tmp_path / "shared"))
    second = buy(manager_b)

    assert sent(broker) == [("BUY", "176")], f"fresh manager bought again: {sent(broker)}"
    assert second is not None and not second.success
    assert "BRK1" in second.message, second.message


@freeze_time(SESSION_TIME_UTC)
def test_fresh_manager_without_the_storage_would_repeat_it_which_is_why_storage_is_shared(tmp_path):
    """Control: a manager pointed at a different directory cannot know about BRK1. This is the gap the store closes."""
    broker = FakeBroker(hidden={"BRK1"})
    buy(build(tmp_path / "a", broker, reservations=EntryReservationStore(tmp_path / "a_store")))

    buy(build(tmp_path / "b", broker, reservations=EntryReservationStore(tmp_path / "b_store")))

    assert sent(broker) == [("BUY", "176"), ("BUY", "176")]


# ── The reservation must be saved before anything is sent ────


@freeze_time(SESSION_TIME_UTC)
def test_buy_is_not_submitted_when_the_reservation_cannot_be_saved(tmp_path):
    (tmp_path / "blocked").write_text("not a directory")  # the store root is a file
    broker = FakeBroker()
    om = build(tmp_path, broker, reservations=EntryReservationStore(tmp_path / "blocked"))

    result = buy(om)

    assert broker.placed == [], "order submitted without a saved reservation"
    assert result is not None and not result.success
    assert "reserv" in result.message.lower(), result.message
    assert "not submit" in result.message.lower() or "could not be read" in result.message.lower(), result.message


@freeze_time(SESSION_TIME_UTC)
def test_buy_is_not_submitted_without_a_reservation_store(tmp_path):
    broker = FakeBroker()
    audit = AuditTrail(log_dir=tmp_path / "audit")
    om = OrderManager(
        risk_manager=RiskManager(config=RiskConfig(total_capital=CAPITAL), audit=audit),
        circuit_breaker=CircuitBreaker(config=CircuitBreakerConfig(), audit=audit, total_capital=CAPITAL),
        paper_trader=PaperTrader(capital=CAPITAL, slippage_pct=0.0, audit=audit),
        audit=audit, mode=TradingMode.LIVE, live_session=FakeLiveSession(broker),
        exit_intents=ExitIntentStore(tmp_path / "exit_intents"), entry_reservations=None,
    )

    result = buy(om)

    assert broker.placed == []
    assert result is not None and not result.success
    assert "reserv" in result.message.lower(), result.message


@freeze_time(SESSION_TIME_UTC)
def test_reservation_records_instrument_quantity_and_sizing_price_before_the_broker_is_called(tmp_path):
    seen: list = []
    broker = FakeBroker()
    om = build(tmp_path, broker)
    original = broker.placeOrder

    def place_and_look(params):
        seen.append(om._entry_reservations.get(KEY))
        return original(params)

    broker.placeOrder = place_and_look

    result = buy(om)

    assert result is not None and result.success
    [reservation] = seen
    assert reservation is not None, "no reservation existed when the broker was called"
    assert reservation.status == "reserved" and reservation.broker_order_id == ""
    assert (reservation.exchange, reservation.token, reservation.trading_symbol, reservation.product_type) == (
        "NSE", "2885", "RELIANCE-EQ", "INTRADAY",
    )
    assert reservation.quantity == 176 and reservation.price_estimate == PRICE
    assert broker.placed[0]["ordertag"] == reservation.order_tag


# ── Acknowledgements in the SDK's real shapes ────────────────


@freeze_time(SESSION_TIME_UTC)
def test_placeorder_string_reply_is_an_acknowledgement_not_a_rejection(tmp_path):
    """Real SmartConnect.placeOrder() returns the order id as a string."""
    broker = FakeBroker(reply="str", has_full_response=False)
    om = build(tmp_path, broker)

    result = buy(om)

    assert result is not None and result.success, result.message
    assert result.status == OrderStatus.PLACED and result.broker_order_id == "BRK1"
    assert result.placed_qty == 176 and result.filled_qty == 0 and result.avg_fill_price == 0.0
    reservation = om._entry_reservations.get(KEY)
    assert reservation is not None and reservation.status == "placed" and reservation.broker_order_id == "BRK1"


@freeze_time(SESSION_TIME_UTC)
def test_full_response_dict_is_used_when_the_sdk_offers_it(tmp_path):
    broker = FakeBroker(reply="dict")
    om = build(tmp_path, broker)

    result = buy(om)

    assert result is not None and result.success and result.status == OrderStatus.PLACED
    reservation = om._entry_reservations.get(KEY)
    assert reservation is not None
    assert (reservation.broker_order_id, reservation.unique_order_id) == ("BRK1", "U-BRK1")


@freeze_time(SESSION_TIME_UTC)
def test_none_reply_is_unclear_not_a_rejection(tmp_path):
    """The SDK returns None for a rejection it swallowed AND for a reply it could not parse — not proof of either."""
    broker = FakeBroker(reply="none")
    om = build(tmp_path, broker)

    result = buy(om)

    assert result is not None and not result.success
    assert result.status == OrderStatus.UNKNOWN, result.status
    assert "none" in result.message.lower(), result.message
    reservation = om._entry_reservations.get(KEY)
    assert reservation is not None and reservation.status == "unclear"


@freeze_time(SESSION_TIME_UTC)
def test_explicit_rejection_releases_the_reservation_so_a_later_buy_may_try(tmp_path):
    broker = FakeBroker(reply="reject")
    om = build(tmp_path, broker)

    first = buy(om)
    assert first is not None and not first.success and first.status == OrderStatus.REJECTED
    assert "margin shortfall" in first.message
    assert om._entry_reservations.get(KEY) is None  # released — nothing is on its way

    broker.reply = "dict"
    second = buy(om)
    assert second is not None and second.success and second.broker_order_id == "BRK1"


@freeze_time(SESSION_TIME_UTC)
def test_request_that_never_reached_the_broker_still_keeps_the_reservation(tmp_path):
    """A timeout cannot tell 'never sent' from 'sent, reply lost': both are unclear."""
    broker = FakeBroker(reply="down")
    om = build(tmp_path, broker)

    result = buy(om)

    assert broker.placed == []
    assert result is not None and result.status == OrderStatus.UNKNOWN
    reservation = om._entry_reservations.get(KEY)
    assert reservation is not None and reservation.status == "unclear"


# ── Counting and release ─────────────────────────────────────


@freeze_time(SESSION_TIME_UTC)
def test_listed_working_order_and_its_reservation_are_counted_once(tmp_path):
    broker = FakeBroker()  # BRK1 will be listed as open
    om = build(tmp_path, broker)
    buy(om)

    book, reason = om._query_broker_book(om._build_order(
        make_signal(signal_type=SignalType.BUY, price=PRICE), make_analysis_result(action=AnalysisAction.BUY),
    ))
    assert reason is None and book is not None
    [reliance] = book.positions
    assert reliance.quantity == 176, f"counted twice: {reliance.quantity}"
    assert book.pending_entry_qty == 176


@freeze_time(SESSION_TIME_UTC)
def test_reservation_is_released_once_the_order_is_terminal_at_the_broker(tmp_path):
    """Filled → the position book has it; the reservation has done its job and is resolved."""
    broker = FakeBroker()
    om = build(tmp_path, broker)
    buy(om)
    broker.fill("BRK1")

    second = buy(om)  # blocked by the POSITION now, not by the reservation
    assert second is not None and not second.success and "already holding" in second.message.lower()
    assert om._entry_reservations.get(KEY) is None

    broker.exit_all()  # position closed later in the day: a fresh entry may be reserved again
    third = buy(om)
    assert third is not None and third.success and third.broker_order_id == "BRK2"


@freeze_time(SESSION_TIME_UTC)
def test_hidden_order_that_becomes_visible_and_cancelled_releases_the_reservation(tmp_path):
    broker = FakeBroker(hidden={"BRK1"})
    om = build(tmp_path, broker)
    buy(om)
    blocked = buy(om)
    assert blocked is not None and not blocked.success

    broker.hidden.clear()
    broker.orders[0]["status"] = broker.orders[0]["orderstatus"] = "cancelled"
    retry = buy(om)

    assert retry is not None and retry.success and retry.broker_order_id == "BRK2"
    reservation = om._entry_reservations.get(KEY)
    assert reservation is not None and reservation.broker_order_id == "BRK2"  # the new one


# ── DUAL ─────────────────────────────────────────────────────


@freeze_time(SESSION_TIME_UTC)
def test_dual_reservation_guards_the_live_leg_only(tmp_path):
    broker = FakeBroker(hidden={"BRK1"})
    om = build(tmp_path, broker, mode=TradingMode.DUAL)
    first = buy(om)
    assert first is not None and first.is_paper and first.success
    assert first.live_leg is not None and first.live_leg.success

    om._paper_trader._positions.clear()  # paper flat again (e.g. paper exit), broker still has BRK1 unlisted
    second = buy(om)

    assert sent(broker) == [("BUY", "176")]
    assert second is not None and second.is_paper and second.success  # paper leg judged on its own book
    assert second.live_leg is not None and not second.live_leg.success
    assert "BRK1" in second.live_leg.message


# ── The same rule on the exit side ───────────────────────────


@freeze_time(SESSION_TIME_UTC)
def test_exit_whose_reply_was_lost_keeps_its_intent_and_is_not_sent_again(tmp_path):
    """A signal SELL times out after the broker accepted it: the exit intent stays, a repeat is refused."""
    broker = FakeBroker(reply="lost", hidden={"BRK1"})
    broker.rows.append({
        "tradingsymbol": "RELIANCE-EQ", "symboltoken": "2885", "exchange": "NSE", "producttype": "INTRADAY",
        "netqty": "10", "buyqty": "10", "sellqty": "0", "buyavgprice": str(PRICE),
    })
    om = build(tmp_path, broker)

    def sell():
        return om.execute_signal(
            make_signal(signal_type=SignalType.SELL, price=PRICE),
            make_analysis_result(action=AnalysisAction.SELL),
        )

    first = sell()
    assert first is not None and first.status == OrderStatus.UNKNOWN, first
    assert sent(broker) == [("SELL", "10")]

    second = sell()

    assert sent(broker) == [("SELL", "10")], f"exit sent again after a lost reply: {sent(broker)}"
    assert second is not None and not second.success
    assert "may or may not have reached the broker" in second.message, second.message


# ── Release must be earned: malformed replies are not rejections ──


def _reply(broker, value):
    broker.placeOrder = lambda params: (broker._accept(params), value)[1]  # accepted at the broker, odd reply
    broker.placeOrderFullResponse = broker.placeOrder


@freeze_time(SESSION_TIME_UTC)
def test_empty_dict_reply_is_unknown_and_keeps_the_reservation(tmp_path):
    broker = FakeBroker(hidden={"BRK1"})
    _reply(broker, {})
    om = build(tmp_path, broker)

    first = buy(om)
    assert first is not None and first.status == OrderStatus.UNKNOWN, (first.status, first.message)
    reservation = om._entry_reservations.get(KEY)
    assert reservation is not None and reservation.status == "unclear"

    second = buy(om)
    assert sent(broker) == [("BUY", "176")], f"bought again after an empty reply: {sent(broker)}"
    assert second is not None and not second.success


@freeze_time(SESSION_TIME_UTC)
def test_reply_without_status_is_unknown_even_if_it_carries_an_order_id(tmp_path):
    broker = FakeBroker(hidden={"BRK1"})
    _reply(broker, {"data": {"orderid": "BRK1"}})
    om = build(tmp_path, broker)

    first = buy(om)
    assert first is not None and first.status == OrderStatus.UNKNOWN, (first.status, first.message)

    second = buy(om)
    assert sent(broker) == [("BUY", "176")]
    assert second is not None and not second.success


@freeze_time(SESSION_TIME_UTC)
def test_status_none_reply_is_unknown(tmp_path):
    broker = FakeBroker(hidden={"BRK1"})
    _reply(broker, {"status": None, "message": "", "data": None})
    om = build(tmp_path, broker)

    first = buy(om)
    assert first is not None and first.status == OrderStatus.UNKNOWN, (first.status, first.message)
    assert om._entry_reservations.get(KEY) is not None


@freeze_time(SESSION_TIME_UTC)
def test_only_an_explicit_status_false_reply_releases_the_reservation(tmp_path):
    broker = FakeBroker()
    broker.placeOrder = lambda params: {"status": False, "message": "AB1010 margin shortfall", "errorcode": "AB1010", "data": None}
    broker.placeOrderFullResponse = broker.placeOrder
    om = build(tmp_path, broker)

    result = buy(om)

    assert result is not None and result.status == OrderStatus.REJECTED
    assert om._entry_reservations.get(KEY) is None


# ── Release must be earned: the position book must agree ──────


@freeze_time(SESSION_TIME_UTC)
def test_complete_order_with_a_lagging_position_book_keeps_the_reservation(tmp_path):
    """BRK1 shows 'complete' but the position book is still the earlier (flat) snapshot: no second BUY."""
    broker = FakeBroker()
    om = build(tmp_path, broker)
    buy(om)
    order = broker.orders[0]
    order["status"] = order["orderstatus"] = "complete"
    order["filledshares"], order["unfilledshares"], order["averageprice"] = "176", "0", str(PRICE)
    assert broker.rows == []  # position book has not caught up

    second = buy(om)

    assert sent(broker) == [("BUY", "176")], f"bought again while the position book lagged: {sent(broker)}"
    assert second is not None and not second.success
    assert "position book" in second.message.lower(), second.message
    assert om._entry_reservations.get(KEY) is not None, "reservation released on an unconfirmed fill"

    broker.fill("BRK1")  # the position book catches up
    third = buy(om)
    assert third is not None and not third.success and "already holding" in third.message.lower()
    assert om._entry_reservations.get(KEY) is None  # now the reservation has done its job


@freeze_time(SESSION_TIME_UTC)
def test_cancelled_after_partial_fill_with_a_lagging_position_book_keeps_the_reservation(tmp_path):
    """BRK1: 100 of 176 filled, then cancelled. The 100 are exposure the position book does not show yet."""
    broker = FakeBroker()
    om = build(tmp_path, broker)
    buy(om)
    order = broker.orders[0]
    order["status"] = order["orderstatus"] = "cancelled"
    order["filledshares"], order["unfilledshares"], order["averageprice"] = "100", "76", str(PRICE)

    second = buy(om)

    assert sent(broker) == [("BUY", "176")], f"bought again with 100 unaccounted for: {sent(broker)}"
    assert second is not None and not second.success
    assert "100" in second.message, second.message
    assert om._entry_reservations.get(KEY) is not None

    broker.rows.append({  # the fill reaches the position book
        "tradingsymbol": "RELIANCE-EQ", "symboltoken": "2885", "exchange": "NSE", "producttype": "INTRADAY",
        "netqty": "100", "buyqty": "100", "sellqty": "0", "buyavgprice": str(PRICE),
    })
    third = buy(om)
    assert third is not None and not third.success and "already holding" in third.message.lower()
    assert om._entry_reservations.get(KEY) is None


@freeze_time(SESSION_TIME_UTC)
def test_cancelled_with_nothing_filled_releases_the_reservation(tmp_path):
    broker = FakeBroker()
    om = build(tmp_path, broker)
    buy(om)
    order = broker.orders[0]
    order["status"] = order["orderstatus"] = "cancelled"

    second = buy(om)

    assert second is not None and second.success and second.broker_order_id == "BRK2"


# ── Release must be earned: the trading day changing proves nothing ──


def test_unresolved_reservation_from_a_previous_day_blocks_until_the_broker_confirms(tmp_path):
    """Yesterday's BUY: reply lost, never listed. Today a fresh manager must not treat the capacity as free."""
    broker = FakeBroker(reply="lost", hidden={"BRK1"})
    with freeze_time("2026-03-03 04:30:00"):  # Tuesday 10:00 IST
        yesterday = build(tmp_path / "y", broker, reservations=EntryReservationStore(tmp_path / "shared"))
        first = buy(yesterday)
        assert first is not None and first.status == OrderStatus.UNKNOWN

    with freeze_time("2026-03-04 04:30:00"):  # Wednesday
        today = build(tmp_path / "t", broker, reservations=EntryReservationStore(tmp_path / "shared"))
        second = buy(today)
        assert sent(broker) == [("BUY", "176")], f"yesterday's unresolved BUY was forgotten: {sent(broker)}"
        assert second is not None and not second.success
        assert "2026-03-03" in second.message or "20260303" in second.message, second.message
        assert "not proof" in second.message.lower() or "check the broker" in second.message.lower(), second.message

        # The broker is asked: it now lists the order as cancelled → released, capacity is back.
        broker.hidden.clear()
        broker.orders[0]["status"] = broker.orders[0]["orderstatus"] = "cancelled"
        broker.reply = "dict"
        third = buy(today)
        assert third is not None and third.success and third.broker_order_id == "BRK2", third.message


@freeze_time(SESSION_TIME_UTC)
def test_complete_buy_elsewhere_not_yet_in_the_position_book_still_counts_as_capital(tmp_path):
    """TCS BUY 2000 @3400 shows 'complete' in the order book; the position book is still flat: ₹68L is committed."""
    broker = FakeBroker()
    broker.orders.append({
        "orderid": "T1", "uniqueorderid": "U-T1", "status": "complete", "orderstatus": "complete",
        "transactiontype": "BUY", "producttype": "INTRADAY", "tradingsymbol": "TCS-EQ", "symboltoken": "11536",
        "exchange": "NSE", "ordertype": "MARKET", "quantity": "2000", "filledshares": "2000", "unfilledshares": "0",
        "price": "0", "averageprice": "3400.0",
    })
    om = build(tmp_path, broker)

    result = buy(om)

    assert result is not None and result.success, result.message
    assert sent(broker) == [("BUY", "70")]  # sized from the ₹2L left, not the full ₹5L


@freeze_time(SESSION_TIME_UTC)
def test_unlisted_order_the_broker_confirms_complete_is_counted_until_the_position_book_shows_it(tmp_path):
    broker = FakeBroker(hidden={"BRK1"})
    om = build(tmp_path, broker)
    buy(om)
    order = broker.orders[0]
    order["status"] = order["orderstatus"] = "complete"
    order["filledshares"], order["unfilledshares"], order["averageprice"] = "176", "0", str(PRICE)
    # still hidden from orderBook(), but individual_order_details answers for it now
    broker.individual_order_details = lambda uid: {"status": True, "data": dict(order)}

    second = buy(om)

    assert sent(broker) == [("BUY", "176")]
    assert second is not None and not second.success
    assert "position book" in second.message.lower() and "176" in second.message, second.message
    assert om._entry_reservations.get(KEY) is not None

    broker.rows.append({
        "tradingsymbol": "RELIANCE-EQ", "symboltoken": "2885", "exchange": "NSE", "producttype": "INTRADAY",
        "netqty": "176", "buyqty": "176", "sellqty": "0", "buyavgprice": str(PRICE),
    })
    third = buy(om)
    assert third is not None and not third.success and "already holding" in third.message.lower()
    assert om._entry_reservations.get(KEY) is None


def test_previous_day_reservation_the_broker_confirms_complete_is_released(tmp_path):
    broker = FakeBroker(hidden={"BRK1"})
    with freeze_time("2026-03-03 04:30:00"):
        buy(build(tmp_path / "y", broker, reservations=EntryReservationStore(tmp_path / "shared")))
    broker.hidden.clear()
    broker.orders[0]["status"] = broker.orders[0]["orderstatus"] = "complete"  # yesterday's, squared off since

    with freeze_time("2026-03-04 04:30:00"):
        today = build(tmp_path / "t", broker, reservations=EntryReservationStore(tmp_path / "shared"))
        result = buy(today)
        assert result is not None and result.success and result.broker_order_id == "BRK2", result.message
        assert today._entry_reservations.get(reservation_key("20260303", "NSE", "2885", "INTRADAY")) is None


@freeze_time(SESSION_TIME_UTC)
def test_exit_with_an_empty_reply_keeps_its_intent(tmp_path):
    broker = FakeBroker(hidden={"BRK1"})
    _reply(broker, {})
    broker.rows.append({
        "tradingsymbol": "RELIANCE-EQ", "symboltoken": "2885", "exchange": "NSE", "producttype": "INTRADAY",
        "netqty": "10", "buyqty": "10", "sellqty": "0", "buyavgprice": str(PRICE),
    })
    om = build(tmp_path, broker)

    def sell():
        return om.execute_signal(make_signal(signal_type=SignalType.SELL, price=PRICE),
                                 make_analysis_result(action=AnalysisAction.SELL))

    first = sell()
    assert first is not None and first.status == OrderStatus.UNKNOWN, (first.status, first.message)
    second = sell()
    assert sent(broker) == [("SELL", "10")], f"exit repeated after an empty reply: {sent(broker)}"
    assert second is not None and not second.success


# ── Shared capital across instruments ────────────────────────
#
# Every unresolved entry reservation draws on the same capital budget and
# the same position slots, whether or not the broker has listed its order
# yet. Once the exposure appears in the broker's books it is counted from
# there, not from the reservation as well. And reading the books, checking,
# sizing and saving the reservation is ONE protected operation across
# processes — two instances cannot both size against the same balance.

import threading

SMALL = dict(total_capital=1_00_000.0, max_capital_deployed_pct=60.0,   # ₹60,000 deployable
             max_capital_per_trade_pct=50.0, min_order_value=1_000.0)   # ₹50,000 per trade
A_PRICE = 100.0   # stock A at ₹100 → 500 shares = ₹50,000
B_PRICE = 100.0


def small_build(tmp_path, broker, *, shared, rendezvous=None, **risk):
    audit = AuditTrail(log_dir=tmp_path / "audit")
    risk_cfg = RiskConfig(**{**SMALL, **risk})
    om = OrderManager(
        risk_manager=RiskManager(config=risk_cfg, audit=audit),
        circuit_breaker=CircuitBreaker(config=CircuitBreakerConfig(), audit=audit, total_capital=risk_cfg.total_capital),
        paper_trader=PaperTrader(capital=risk_cfg.total_capital, slippage_pct=0.0, audit=audit),
        audit=audit, mode=TradingMode.LIVE, live_session=FakeLiveSession(broker),
        exit_intents=ExitIntentStore(tmp_path / "exit_intents"),
        entry_reservations=EntryReservationStore(shared),
    )
    return om


def buy_stock(om, symbol, token, price):
    return om.execute_signal(
        make_signal(symbol=symbol, token=token, signal_type=SignalType.BUY, price=price),
        make_analysis_result(action=AnalysisAction.BUY, signal_symbol=symbol),
    )


def value_sent(broker) -> float:
    return sum(int(p["quantity"]) * float(p["price"]) for p in broker.placed)


class RacingBroker(FakeBroker):
    """
    Two callers that both get into position() before either has reserved
    have both decided on the same broker state. The barrier makes that
    overlap happen whenever the protection lets it; if a caller is held
    outside while the other finishes, the barrier times out and life goes on.
    """

    def __init__(self, barrier: threading.Barrier, **kw):
        super().__init__(**kw)
        self._barrier = barrier
        self._lock = threading.Lock()

    def position(self):
        try:
            self._barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return super().position()

    def _accept(self, params):
        with self._lock:
            return super()._accept(params)


@freeze_time(SESSION_TIME_UTC)
def test_sequential_reservation_for_one_stock_reduces_capital_for_the_next(tmp_path):
    """₹60,000 deployable; ₹50,000 reserved for A (unlisted). B must get the ₹10,000 left, not another ₹50,000."""
    broker = FakeBroker(hidden={"BRK1"})
    om = small_build(tmp_path, broker, shared=tmp_path / "shared")

    a = buy_stock(om, "AAA", "111", A_PRICE)
    assert a is not None and a.success and broker.placed[0]["quantity"] == "500"

    b = buy_stock(om, "BBB", "222", B_PRICE)

    assert b is not None and b.success, b.message
    assert broker.placed[1]["quantity"] == "100", f"B got {broker.placed[1]['quantity']} — A's ₹50,000 not counted"
    assert value_sent(broker) == 60_000


@freeze_time(SESSION_TIME_UTC)
def test_reservation_is_not_counted_twice_once_the_broker_lists_the_order(tmp_path):
    """A's order is listed as open: counted from the order book only — B still gets ₹10,000, not ₹0."""
    broker = FakeBroker()
    om = small_build(tmp_path, broker, shared=tmp_path / "shared")
    buy_stock(om, "AAA", "111", A_PRICE)
    assert broker.orders[0]["status"] == "open"

    b = buy_stock(om, "BBB", "222", B_PRICE)

    assert b is not None and b.success, b.message
    assert broker.placed[1]["quantity"] == "100"


@freeze_time(SESSION_TIME_UTC)
def test_reservation_is_not_counted_twice_once_the_fill_is_in_the_position_book(tmp_path):
    broker = FakeBroker()
    om = small_build(tmp_path, broker, shared=tmp_path / "shared")
    buy_stock(om, "AAA", "111", A_PRICE)
    broker.fill("BRK1")
    broker.rows[0]["buyavgprice"] = str(A_PRICE)
    broker.orders[0]["averageprice"] = str(A_PRICE)

    b = buy_stock(om, "BBB", "222", B_PRICE)

    assert b is not None and b.success, b.message
    assert broker.placed[1]["quantity"] == "100"


@freeze_time(SESSION_TIME_UTC)
def test_unlisted_reservation_takes_a_position_slot(tmp_path):
    broker = FakeBroker(hidden={"BRK1"})
    om = small_build(tmp_path, broker, shared=tmp_path / "shared", max_open_positions=1)
    buy_stock(om, "AAA", "111", A_PRICE)

    b = buy_stock(om, "BBB", "222", B_PRICE)

    assert len(broker.placed) == 1, f"B took a slot A's reservation already holds: {sent(broker)}"
    assert b is not None and not b.success and "max open positions" in b.message.lower(), b.message


@freeze_time(SESSION_TIME_UTC)
def test_identifiable_pending_market_order_is_valued_at_its_saved_sizing_estimate(tmp_path):
    """A's MARKET order is listed with price 0. Its reservation knows the sizing price: B is sized, not blocked."""
    broker = FakeBroker()
    om = small_build(tmp_path, broker, shared=tmp_path / "shared")
    buy_stock(om, "AAA", "111", A_PRICE)
    broker.orders[0]["price"] = "0"          # as the broker lists a MARKET order
    broker.orders[0]["ordertype"] = "MARKET"

    b = buy_stock(om, "BBB", "222", B_PRICE)

    assert b is not None and b.success, b.message
    assert broker.placed[1]["quantity"] == "100"


@freeze_time(SESSION_TIME_UTC)
def test_unidentifiable_pending_market_order_still_blocks(tmp_path):
    broker = FakeBroker()
    broker.orders.append({  # placed by someone else: no reservation, no tag, no price
        "orderid": "X9", "uniqueorderid": "U-X9", "status": "open", "orderstatus": "open",
        "transactiontype": "BUY", "producttype": "INTRADAY", "tradingsymbol": "CCC-EQ", "symboltoken": "333",
        "exchange": "NSE", "ordertype": "MARKET", "quantity": "10", "filledshares": "0", "unfilledshares": "10",
        "price": "0", "averageprice": "0", "ordertag": "",
    })
    om = small_build(tmp_path, broker, shared=tmp_path / "shared")

    b = buy_stock(om, "BBB", "222", B_PRICE)

    assert broker.placed == []
    assert b is not None and not b.success and "no price" in b.message.lower(), b.message


def _race(tmp_path, *, max_open_positions=20, hidden=frozenset()):
    """Two managers (two 'processes') sharing the store, signalled at the same moment for A and B."""
    barrier = threading.Barrier(2, timeout=0.5)
    broker = RacingBroker(barrier, hidden=hidden)
    shared = tmp_path / "shared"
    om_a = small_build(tmp_path / "a", broker, shared=shared, max_open_positions=max_open_positions)
    om_b = small_build(tmp_path / "b", broker, shared=shared, max_open_positions=max_open_positions)
    results = {}

    def run(name, om, symbol, token):
        results[name] = buy_stock(om, symbol, token, A_PRICE)

    threads = [threading.Thread(target=run, args=("a", om_a, "AAA", "111")),
               threading.Thread(target=run, args=("b", om_b, "BBB", "222"))]
    with freeze_time(SESSION_TIME_UTC):  # one clock for both threads (freezegun is not thread-safe per thread)
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
    assert set(results) == {"a", "b"}, f"a thread did not finish: {results}"
    return broker, results


def test_two_instances_racing_for_remaining_capital_cannot_both_take_it(tmp_path):
    broker, results = _race(tmp_path, hidden={"BRK1", "BRK2"})

    assert value_sent(broker) <= 60_000, (
        f"both instances sized against the same balance: {sent(broker)} = ₹{value_sent(broker):,.0f}"
    )
    quantities = sorted(int(p["quantity"]) for p in broker.placed)
    assert quantities == [100, 500], quantities
    assert all(r is not None and r.success for r in results.values()), {k: v.message for k, v in results.items()}


def test_two_instances_racing_for_the_last_position_slot_cannot_both_take_it(tmp_path):
    broker, results = _race(tmp_path, max_open_positions=1, hidden={"BRK1", "BRK2"})

    assert len(broker.placed) == 1, f"both instances took the last slot: {sent(broker)}"
    outcomes = sorted(r.success for r in results.values())
    assert outcomes == [False, True]
    loser = next(r for r in results.values() if not r.success)
    assert "max open positions" in loser.message.lower(), loser.message


# ── Unknown outcomes are not rejections — anywhere ───────────


def audit_events(tmp_path) -> list[dict]:
    import json as _json
    events = []
    for f in sorted((tmp_path / "audit").glob("audit_*.jsonl")):
        events += [_json.loads(l) for l in f.read_text().splitlines() if l.strip().startswith("{")]
    return events


@freeze_time(SESSION_TIME_UTC)
def test_unknown_outcome_is_counted_apart_from_rejections_in_stats_and_circuit_breaker(tmp_path):
    broker = FakeBroker(reply="lost", hidden={"BRK1"})
    om = build(tmp_path, broker)

    unknown = buy(om)
    assert unknown is not None and unknown.status == OrderStatus.UNKNOWN

    broker.reply = "reject"
    om._entry_reservations.resolve(KEY, "test: clear the way for a confirmed rejection")
    rejected = buy(om)
    assert rejected is not None and rejected.status == OrderStatus.REJECTED

    stats = om.stats
    assert stats["orders_unknown"] == 1 and stats["orders_rejected"] == 1 and stats["orders_placed"] == 0
    cb = stats["circuit_breaker"]
    assert cb["unknown_orders"] == 1
    assert cb["failed_orders"] == 1 and cb["total_orders"] == 1   # the unknown one is not a failure


@freeze_time(SESSION_TIME_UTC)
def test_unknown_outcome_is_audited_as_unknown_not_as_rejected(tmp_path):
    broker = FakeBroker(reply="lost", hidden={"BRK1"})
    om = build(tmp_path, broker)
    buy(om)

    events = audit_events(tmp_path)
    unknown = [e for e in events if e["details"].get("action") == "order_outcome_unknown"]
    assert len(unknown) == 1 and unknown[0]["details"]["symbol"] == "RELIANCE"
    assert "neither placement nor rejection confirmed" in unknown[0]["details"]["note"]
    rejected = [e for e in events if e["event_type"] == "ORDER_REJECTED" and e["details"].get("symbol") == "RELIANCE"]
    assert rejected == [], f"an unknown outcome was audited as a rejection: {rejected}"
    assert not [e for e in events if e["event_type"] in ("ORDER_PLACED", "ORDER_FILLED") and e["details"].get("order_id")]


@freeze_time(SESSION_TIME_UTC)
def test_dual_result_message_says_unknown_not_rejected(tmp_path):
    broker = FakeBroker(reply="lost", hidden={"BRK1"})
    om = build(tmp_path, broker, mode=TradingMode.DUAL)

    result = buy(om)

    assert result is not None and result.is_paper and result.success     # paper leg filled
    assert result.live_leg is not None and result.live_leg.status == OrderStatus.UNKNOWN
    assert "outcome unknown" in result.message.lower(), result.message
    assert "leg rejected" not in result.message.lower() and "REJECTED:" not in result.message, result.message
    assert "not placed" not in result.message.lower() and "not placed" not in result.live_leg.message.lower()


@freeze_time(SESSION_TIME_UTC)
def test_square_off_exit_with_lost_reply_is_reported_as_submitted_unknown_not_rejected_or_unsent(tmp_path):
    broker = FakeBroker(reply="lost", hidden={"BRK1"})
    broker.rows.append({
        "tradingsymbol": "RELIANCE-EQ", "symboltoken": "2885", "exchange": "NSE", "producttype": "INTRADAY",
        "netqty": "10", "buyqty": "10", "sellqty": "0", "buyavgprice": str(PRICE),
    })
    om = build(tmp_path, broker)

    report = om.square_off_intraday()

    [x] = report.live
    assert x.state.value == "UNKNOWN" and report.live_unknown == [x]
    assert report.live_rejected == [] and report.live_unclear == []
    text = x.describe()
    assert "SUBMITTED" in text and "UNKNOWN" in text, text
    assert "no order sent" not in text.lower() and "rejected" not in text.lower(), text
    assert report.needs_attention and not report.settled
    # The exit intent is kept (unacknowledged), so a second square-off does not resend.
    again = om.square_off_intraday()
    assert sent(broker) == [("SELL", "10")], sent(broker)
    assert again.live_unclear and "may or may not have reached the broker" in again.live_unclear[0].detail
    events = audit_events(tmp_path)
    assert [e for e in events if e["details"].get("action") == "intraday_square_off_exit_outcome_unknown"]
    assert not [e for e in events if e["details"].get("action") == "intraday_square_off_exit"
                and e["event_type"] == "ORDER_REJECTED"]


# ── Adoption by tag must verify the instrument ───────────────
#
# A reservation left unacknowledged after a lost reply is recovered by
# finding the order that carries its tag. The tag embeds the token, but
# tokens are exchange-specific and the same tag could be on an order for
# another exchange, product or side. Adopting such an order — and releasing
# the reservation on ITS status — would let the original, still-unresolved
# BUY be sent again. Exchange + token/symbol + product + side must match,
# exactly once, and be verifiable; otherwise the reservation is kept.

from trading.entry_reservation_store import entry_order_tag

TAG = entry_order_tag("20260304", "2885")


def stranger(order_id: str, *, exchange: str | None = "BSE", product: str = "INTRADAY", side: str = "BUY",
             status: str = "cancelled", token: str = "2885", symbol: str = "RELIANCE-EQ") -> dict:
    """An order that carries OUR tag but is (or may be) something else."""
    row = {
        "orderid": order_id, "uniqueorderid": f"U-{order_id}", "status": status, "orderstatus": status,
        "transactiontype": side, "producttype": product, "tradingsymbol": symbol, "symboltoken": token,
        "ordertype": "LIMIT", "quantity": "50", "filledshares": "0", "unfilledshares": "50" if status == "open" else "0",
        "price": str(PRICE), "averageprice": "0", "ordertag": TAG,
    }
    if exchange is not None:
        row["exchange"] = exchange
    return row


def lost_reply_then(tmp_path, *listed: dict):
    """BUY RELIANCE whose reply was lost and whose real order the broker has not listed; `listed` are also in the book."""
    broker = FakeBroker(reply="lost", hidden={"BRK1"})
    om = build(tmp_path, broker)
    first = buy(om)
    assert first is not None and first.status == OrderStatus.UNKNOWN
    broker.orders.extend(listed)
    broker.reply = "dict"
    return om, broker


def assert_kept_and_blocked(om, broker, second, *needles: str):
    assert sent(broker) == [("BUY", "176")], f"duplicate BUY submitted: {sent(broker)}"
    assert second is not None and not second.success
    reservation = om._entry_reservations.get(KEY)
    assert reservation is not None and reservation.status == "unclear" and reservation.broker_order_id == ""
    for needle in needles:
        assert needle.lower() in second.message.lower(), second.message


@freeze_time(SESSION_TIME_UTC)
def test_cancelled_order_with_our_tag_on_another_exchange_is_not_adopted(tmp_path):
    om, broker = lost_reply_then(tmp_path, stranger("BSE1", exchange="BSE"))

    second = buy(om)

    assert_kept_and_blocked(om, broker, second, "BSE1", "not adopted", "BSE/2885", "not NSE/2885/INTRADAY/BUY")


@freeze_time(SESSION_TIME_UTC)
def test_cancelled_order_with_our_tag_for_another_product_is_not_adopted(tmp_path):
    om, broker = lost_reply_then(tmp_path, stranger("DLV1", exchange="NSE", product="DELIVERY"))

    second = buy(om)

    assert_kept_and_blocked(om, broker, second, "DLV1", "not adopted")


@freeze_time(SESSION_TIME_UTC)
def test_cancelled_order_with_our_tag_on_the_other_side_is_not_adopted(tmp_path):
    om, broker = lost_reply_then(tmp_path, stranger("SEL1", exchange="NSE", side="SELL"))

    second = buy(om)

    assert_kept_and_blocked(om, broker, second, "SEL1", "not adopted")


@freeze_time(SESSION_TIME_UTC)
def test_tagged_order_without_an_exchange_cannot_be_verified_and_is_not_adopted(tmp_path):
    om, broker = lost_reply_then(tmp_path, stranger("NOX1", exchange=None))

    second = buy(om)

    # The order-book scan already refuses a BUY row with no exchange (cannot tell
    # which instrument it is for) before adoption is even attempted.
    assert_kept_and_blocked(om, broker, second, "NOX1", "exchange")


@freeze_time(SESSION_TIME_UTC)
def test_multiple_orders_matching_our_tag_is_ambiguous_and_none_is_adopted(tmp_path):
    om, broker = lost_reply_then(
        tmp_path,
        stranger("DUP1", exchange="NSE", status="cancelled"),
        stranger("DUP2", exchange="NSE", status="cancelled"),
    )

    second = buy(om)

    assert_kept_and_blocked(om, broker, second, "DUP1", "DUP2", "ambiguous")


@freeze_time(SESSION_TIME_UTC)
def test_the_one_genuinely_matching_tagged_order_is_adopted_and_its_status_used(tmp_path):
    """NSE + token 2885 + INTRADAY + BUY, exactly one: adopted. Cancelled with nothing filled → released."""
    om, broker = lost_reply_then(tmp_path, stranger("REAL1", exchange="NSE", status="cancelled"),
                                 stranger("BSEX", exchange="BSE", status="open"))  # a BSE look-alike alongside

    second = buy(om)

    assert second is not None and second.success and second.broker_order_id == "BRK2", second.message
    assert sent(broker) == [("BUY", "176"), ("BUY", "176")]
    reservation = om._entry_reservations.get(KEY)
    assert reservation is not None and reservation.broker_order_id == "BRK2"   # the new one; REAL1's was released


@freeze_time(SESSION_TIME_UTC)
def test_the_one_genuinely_matching_tagged_order_still_open_blocks_as_a_working_buy(tmp_path):
    om, broker = lost_reply_then(tmp_path, stranger("REAL2", exchange="NSE", status="open"))

    second = buy(om)

    assert sent(broker) == [("BUY", "176")]
    assert second is not None and not second.success and "REAL2" in second.message, second.message
    reservation = om._entry_reservations.get(KEY)
    assert reservation is not None and reservation.broker_order_id == "REAL2"   # adopted


@freeze_time(SESSION_TIME_UTC)
def test_listed_order_for_the_reservations_id_that_is_another_instrument_is_not_trusted(tmp_path):
    """The broker lists our order id against a different instrument: nothing is released on that."""
    broker = FakeBroker()
    om = build(tmp_path, broker)
    buy(om)
    broker.orders[0].update({"exchange": "BSE", "status": "cancelled", "orderstatus": "cancelled"})

    second = buy(om)

    assert sent(broker) == [("BUY", "176")], f"duplicate BUY submitted: {sent(broker)}"
    assert second is not None and not second.success and "BRK1" in second.message, second.message
    assert om._entry_reservations.get(KEY) is not None
