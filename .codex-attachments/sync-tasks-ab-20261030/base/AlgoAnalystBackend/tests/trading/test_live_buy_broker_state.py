"""
LIVE / DUAL mode: a BUY must be judged against what the broker actually
holds, not against the paper position book.

The paper book is this process's belief. After a restart it is empty even
when the broker is long, so the per-stock position limit, the total
position count and the deployed-capital figure would all be computed from
nothing — and a second BUY for a stock already held at the broker would go
straight through, spending the same capital allocation twice.

Rules covered here:
  * LIVE: the risk gates see the broker's verified position book.
  * A position book that cannot be read is NOT an empty one: the BUY is
    blocked, in LIVE outright and in DUAL for the live leg.
  * DUAL: the paper leg is judged against the paper book, the live leg
    against the broker's book — separately, each outcome visible.
  * The broker's row for "this instrument" is matched on exchange AND
    token/trading symbol AND product type.

No real orders are placed: FakeBroker stands in for SmartConnect and
records what would have been sent.
"""

from __future__ import annotations

from freezegun import freeze_time

from core.constants import AnalysisAction, SignalType, TransactionType
from core.security.audit import AuditTrail
from tests.factories import make_analysis_result, make_order_request, make_signal
from trading.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
from trading.entry_reservation_store import EntryReservationStore
from trading.exit_intent_store import ExitIntentStore
from trading.order_manager import OrderManager, TradingMode
from trading.paper_trader import PaperTrader
from trading.risk_manager import RiskConfig, RiskManager

# 10:00 IST on a Wednesday (UTC+5:30) — inside market hours for every gate.
SESSION_TIME_UTC = "2026-03-04 04:30:00"
CAPITAL = 1_00_00_000.0  # 1 Cr → 5% per trade = ₹5,00,000 ≈ 176 shares at ₹2830
PRICE = 2830.0


def row(token: str = "2885", qty: int = 176, *, symbol: str = "RELIANCE-EQ",
        exchange: str | None = "NSE", product: str = "INTRADAY", price: str = "2830.0") -> dict:
    """A SmartAPI position() row. exchange=None leaves the field out entirely."""
    r = {
        "tradingsymbol": symbol, "symboltoken": token, "producttype": product,
        "netqty": str(qty), "buyqty": str(max(qty, 0)), "sellqty": str(max(-qty, 0)),
        "buyavgprice": price, "avgnetprice": price,
    }
    if exchange is not None:
        r["exchange"] = exchange
    return r


def working_buy(token: str = "2885", qty: int = 176, *, filled: int = 0, symbol: str = "RELIANCE-EQ",
                exchange: str | None = "NSE", product: str = "INTRADAY", price: str = "2830.0",
                ordertype: str = "LIMIT", status: str = "open", order_id: str = "OLD1",
                side: str = "BUY") -> dict:
    """A SmartAPI orderBook() row for a BUY still working (or in any given status)."""
    r = {
        "orderid": order_id, "uniqueorderid": f"U-{order_id}", "transactiontype": side,
        "tradingsymbol": symbol, "symboltoken": token, "producttype": product,
        "ordertype": ordertype, "status": status, "orderstatus": status,
        "quantity": str(qty), "filledshares": str(filled), "unfilledshares": str(qty - filled),
        "price": price if ordertype == "LIMIT" else "0",
        "averageprice": price if filled else "0",
    }
    if exchange is not None:
        r["exchange"] = exchange
    return r


class FakeBroker:
    """Stand-in for SmartConnect: position() + orderBook() + placeOrder()."""

    def __init__(self, rows: list[dict] | None = None, *, orders: list[dict] | None = None,
                 positions_fail: bool = False, positions_reject: bool = False,
                 orderbook_fail: bool = False, orderbook_reject: bool = False):
        self.rows = rows or []
        self.orders = orders or []
        self.positions_fail = positions_fail      # the API call raises
        self.positions_reject = positions_reject  # the API answers status=False
        self.orderbook_fail = orderbook_fail
        self.orderbook_reject = orderbook_reject
        self.placed: list[dict] = []
        self.position_calls = 0

    def position(self):
        self.position_calls += 1
        if self.positions_fail:
            raise RuntimeError("position API unavailable")
        if self.positions_reject:
            return {"status": False, "message": "AG8001 Invalid Token", "data": None}
        return {"status": True, "message": "SUCCESS", "data": list(self.rows) or None}

    def orderBook(self):
        if self.orderbook_fail:
            raise RuntimeError("order book API unavailable")
        if self.orderbook_reject:
            return {"status": False, "message": "AB1004 Something went wrong", "data": None}
        return {"status": True, "message": "SUCCESS", "data": list(self.orders) or None}

    def placeOrder(self, params):
        self.placed.append(dict(params))
        return {"status": True, "data": {"orderid": f"BRK{len(self.placed)}"}}


class FakeLiveSession:
    def __init__(self, broker: FakeBroker):
        self._broker = broker

    def get_api(self):
        return self._broker


def build(tmp_path, mode: TradingMode, broker: FakeBroker, *,
          paper_holding: int = 0, **risk_overrides):
    audit = AuditTrail(log_dir=tmp_path)
    # max_positions_per_stock=1 is the default: one position per stock is the limit.
    risk = RiskManager(config=RiskConfig(total_capital=CAPITAL, **risk_overrides), audit=audit)
    cb = CircuitBreaker(config=CircuitBreakerConfig(), audit=audit, total_capital=CAPITAL)
    paper = PaperTrader(capital=CAPITAL, slippage_pct=0.0, audit=audit)
    om = OrderManager(
        risk_manager=risk, circuit_breaker=cb, paper_trader=paper, audit=audit,
        mode=mode, live_session=FakeLiveSession(broker),
        exit_intents=ExitIntentStore(tmp_path / "intents"),
        # A live BUY is reserved durably before the broker is called.
        entry_reservations=EntryReservationStore(tmp_path / "entry_reservations"),
    )
    if paper_holding:
        seeded = paper.place_order(make_order_request(
            transaction_type=TransactionType.BUY, quantity=paper_holding, price=PRICE,
        ))
        assert seeded.success and paper._positions["RELIANCE"].quantity == paper_holding
    return om, paper


def buy(om: OrderManager):
    return om.execute_signal(
        make_signal(signal_type=SignalType.BUY, price=PRICE),
        make_analysis_result(action=AnalysisAction.BUY),
    )


def sent(broker: FakeBroker) -> list[tuple[str, str]]:
    return [(p["transactiontype"], p["quantity"]) for p in broker.placed]


# ── Existing position at the broker ──────────────────────────


@freeze_time(SESSION_TIME_UTC)
def test_live_buy_is_blocked_when_broker_already_holds_the_stock_at_its_position_limit(tmp_path):
    """
    Restart mid-session in LIVE mode: the paper book is empty, but the broker
    already holds RELIANCE (176 shares, INTRADAY) — one position, which is the
    per-stock limit. A fresh BUY signal for RELIANCE must NOT reach the broker:
    it would open a second position in the same stock and spend the same
    capital allocation twice.
    """
    broker = FakeBroker([row("2885", 176)])
    om, paper = build(tmp_path, TradingMode.LIVE, broker)
    assert paper.positions == []  # nothing survived the restart

    result = buy(om)

    assert broker.placed == [], (
        f"BUY sent to the broker although it already holds RELIANCE: {broker.placed}"
    )
    assert result is not None and not result.success
    assert "RELIANCE" in result.message
    assert "already" in result.message.lower() or "position" in result.message.lower(), result.message


@freeze_time(SESSION_TIME_UTC)
def test_live_buy_of_a_different_stock_is_sized_from_capital_the_broker_has_left(tmp_path):
    """
    The broker already holds TCS worth ₹68,00,000 (INTRADAY). With 70% of
    1 Cr deployable, only ₹2,00,000 is left — a BUY for RELIANCE must be
    sized from that, not from the full ₹5,00,000 per-trade cap the empty
    paper book would allow.
    """
    broker = FakeBroker([row("11536", 2000, symbol="TCS-EQ", price="3400.0")])  # 68L deployed
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    result = buy(om)

    assert result is not None and result.success, result.message
    assert len(broker.placed) == 1 and broker.placed[0]["transactiontype"] == "BUY"
    qty = int(broker.placed[0]["quantity"])
    assert qty * PRICE <= 2_00_000, f"BUY {qty} @ {PRICE} = ₹{qty * PRICE:,.0f} exceeds the ₹2,00,000 left"
    assert qty == int(2_00_000 / PRICE)  # 70 — not the 176 an empty book would give


@freeze_time(SESSION_TIME_UTC)
def test_live_buy_with_flat_broker_and_readable_book_goes_through(tmp_path):
    """A verified empty book (SmartAPI data=None) is a genuine green light."""
    broker = FakeBroker([])
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    result = buy(om)

    assert result is not None and result.success, result.message
    assert sent(broker) == [("BUY", "176")]
    assert broker.position_calls == 1  # read once in execute_signal, reused at placement


# ── Unavailable / unreadable position book is not an empty one ──


@freeze_time(SESSION_TIME_UTC)
def test_live_buy_is_blocked_when_broker_position_query_fails(tmp_path):
    broker = FakeBroker([row("2885", 176)], positions_fail=True)
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    result = buy(om)

    assert broker.placed == [], "BUY sent without a verified position book"
    assert result is not None and not result.success
    assert "verif" in result.message.lower(), result.message
    assert "empty" in result.message.lower(), result.message


@freeze_time(SESSION_TIME_UTC)
def test_live_buy_is_blocked_when_broker_rejects_the_position_query(tmp_path):
    broker = FakeBroker([row("2885", 176)], positions_reject=True)
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    result = buy(om)

    assert broker.placed == []
    assert result is not None and not result.success
    assert "verif" in result.message.lower(), result.message


@freeze_time(SESSION_TIME_UTC)
def test_live_buy_is_blocked_when_a_matching_row_has_no_exchange(tmp_path):
    """
    A row with our token but no exchange might or might not be our
    instrument (tokens are exchange-specific). That is unverifiable, not
    'not ours'.
    """
    broker = FakeBroker([row("2885", 176, exchange=None)])
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    result = buy(om)

    assert broker.placed == []
    assert result is not None and not result.success
    assert "verif" in result.message.lower(), result.message


@freeze_time(SESSION_TIME_UTC)
def test_live_buy_is_blocked_when_a_position_row_is_unreadable(tmp_path):
    broker = FakeBroker([row("2885", 176)])
    broker.rows[0]["netqty"] = "lots"
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    result = buy(om)

    assert broker.placed == []
    assert result is not None and not result.success
    assert "verif" in result.message.lower(), result.message


@freeze_time(SESSION_TIME_UTC)
def test_live_buy_is_blocked_when_a_held_row_has_no_readable_price_unknown_capital_is_not_free(tmp_path):
    """Broker holds TCS but the row has no average price: its deployed capital is unknown, so no BUY."""
    tcs = row("11536", 100, symbol="TCS-EQ")
    for key in ("buyavgprice", "avgnetprice"):
        del tcs[key]
    broker = FakeBroker([tcs])
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    result = buy(om)

    assert broker.placed == []
    assert result is not None and not result.success
    assert "capital" in result.message.lower() and "TCS" in result.message, result.message


@freeze_time(SESSION_TIME_UTC)
def test_live_sell_exit_is_not_blocked_by_a_row_without_a_price(tmp_path):
    """A missing price does not make the holding unverifiable: the quantity is what proves it."""
    held = row("2885", 10)
    for key in ("buyavgprice", "avgnetprice"):
        del held[key]
    broker = FakeBroker([held])
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    result = om.execute_signal(
        make_signal(signal_type=SignalType.SELL, price=PRICE),
        make_analysis_result(action=AnalysisAction.SELL),
    )

    assert result is not None and result.success, result.message
    assert sent(broker) == [("SELL", "10")]


# ── DUAL: paper and live judged separately, in both directions ──
#
# Each leg is evaluated, sized and placed against its own book. A paper
# rejection does not stop a live leg the broker's book approves; a live
# rejection (or an unverifiable broker) does not hide or undo a valid
# paper result. The top-level result is the paper leg; the live leg's
# outcome is in result.live_leg.


def assert_paper_filled(result, qty: int) -> None:
    assert result is not None and result.is_paper and result.success, result.message
    assert result.status.value == "FILLED" and result.filled_qty == qty
    assert result.order_id.startswith("PAPER-")


def assert_paper_rejected(result, *needles: str) -> None:
    assert result is not None and result.is_paper and not result.success
    for needle in needles:
        assert needle.lower() in result.message.lower(), result.message


def assert_live_placed(result, broker: FakeBroker, qty: int) -> None:
    live = result.live_leg
    assert live is not None and live.success and not live.is_paper, live and live.message
    assert live.status.value == "PLACED"
    assert live.placed_qty == qty and live.filled_qty == 0  # acknowledged, not filled
    assert live.broker_order_id == "BRK1"
    assert sent(broker) == [("BUY", str(qty))]
    assert "live leg placed" in result.message.lower(), result.message


def assert_live_rejected(result, broker: FakeBroker, *needles: str) -> None:
    live = result.live_leg
    assert live is not None and not live.success and not live.is_paper
    for needle in needles:
        assert needle.lower() in live.message.lower(), live.message
    assert broker.placed == [], f"live order sent despite rejection: {broker.placed}"
    assert "live leg rejected" in result.message.lower(), result.message


@freeze_time(SESSION_TIME_UTC)
def test_dual_paper_rejects_already_held_but_broker_flat_live_leg_still_goes_through(tmp_path):
    """
    Paper book already long RELIANCE (per-stock limit reached), broker flat.
    The paper leg is rejected on its own book; the live leg is approved on
    the broker's and must be placed — the paper rejection does not stop it.
    """
    broker = FakeBroker([])
    om, paper = build(tmp_path, TradingMode.DUAL, broker, paper_holding=176)

    result = buy(om)

    assert_paper_rejected(result, "RELIANCE", "already")
    assert paper._positions["RELIANCE"].quantity == 176  # paper book untouched
    assert_live_placed(result, broker, 176)


@freeze_time(SESSION_TIME_UTC)
def test_dual_paper_empty_but_broker_holds_the_stock_paper_fills_live_leg_is_blocked(tmp_path):
    """The reverse: paper flat, broker long RELIANCE. Paper fills; the live leg is blocked and visible."""
    broker = FakeBroker([row("2885", 176)])
    om, paper = build(tmp_path, TradingMode.DUAL, broker)

    result = buy(om)

    assert_paper_filled(result, 176)
    assert paper._positions["RELIANCE"].quantity == 176
    assert_live_rejected(result, broker, "RELIANCE", "already")


@freeze_time(SESSION_TIME_UTC)
def test_dual_broker_unavailable_paper_leg_proceeds_live_leg_is_blocked_not_treated_as_flat(tmp_path):
    """Broker position API down: the paper leg is unaffected; the live leg is refused, not sent blind."""
    broker = FakeBroker([], positions_fail=True)
    om, paper = build(tmp_path, TradingMode.DUAL, broker)

    result = buy(om)

    assert_paper_filled(result, 176)
    assert paper._positions["RELIANCE"].quantity == 176
    assert_live_rejected(result, broker, "verif", "empty")


@freeze_time(SESSION_TIME_UTC)
def test_dual_broker_unavailable_and_paper_rejects_both_outcomes_are_reported(tmp_path):
    """Neither leg can go: the paper rejection is the result, the live one is alongside it, nothing is sent."""
    broker = FakeBroker([], positions_fail=True)
    om, paper = build(tmp_path, TradingMode.DUAL, broker, paper_holding=176)

    result = buy(om)

    assert_paper_rejected(result, "RELIANCE", "already")
    assert paper._positions["RELIANCE"].quantity == 176
    assert_live_rejected(result, broker, "verif")


@freeze_time(SESSION_TIME_UTC)
def test_dual_each_leg_is_sized_from_its_own_book(tmp_path):
    """
    Paper book empty → the paper leg fills the full ₹5,00,000 (176 shares).
    The broker already has ₹68,00,000 of the ₹70,00,000 deployable out in
    TCS → the live leg is sized from the ₹2,00,000 left (70 shares), not
    from the paper leg's figure. Capital the broker has deployed is not
    allocated again.
    """
    broker = FakeBroker([row("11536", 2000, symbol="TCS-EQ", price="3400.0")])
    om, _ = build(tmp_path, TradingMode.DUAL, broker)

    result = buy(om)

    assert_paper_filled(result, 176)
    assert_live_placed(result, broker, int(2_00_000 / PRICE))  # 70


@freeze_time(SESSION_TIME_UTC)
def test_dual_live_rejected_by_the_broker_itself_does_not_undo_the_paper_fill(tmp_path):
    """placeOrder is refused: the paper leg stays filled, the refusal is reported on the live leg."""
    broker = FakeBroker([])
    broker.placeOrder = lambda params: {"status": False, "message": "AB1010 margin shortfall"}
    om, paper = build(tmp_path, TradingMode.DUAL, broker)

    result = buy(om)

    assert_paper_filled(result, 176)
    assert paper._positions["RELIANCE"].quantity == 176
    live = result.live_leg
    assert live is not None and not live.success and "margin shortfall" in live.message
    assert "live leg rejected" in result.message.lower(), result.message


@freeze_time(SESSION_TIME_UTC)
def test_dual_both_legs_approved_are_both_placed_and_each_audited_as_itself(tmp_path):
    """Paper flat, broker flat: paper FILLED, live PLACED — and the audit never calls the live leg a fill."""
    import json

    broker = FakeBroker([])
    om, _ = build(tmp_path, TradingMode.DUAL, broker)

    result = buy(om)

    assert_paper_filled(result, 176)
    assert_live_placed(result, broker, 176)
    events = []
    for f in sorted(tmp_path.glob("audit_*.jsonl")):
        events += [json.loads(l) for l in f.read_text().splitlines() if l.strip().startswith("{")]
    legs = {(e["event_type"], e["details"].get("leg")) for e in events if e["details"].get("leg")}
    assert ("ORDER_FILLED", "paper") in legs
    assert ("ORDER_PLACED", "live") in legs
    assert ("ORDER_FILLED", "live") not in legs
    assert om.stats["orders_placed"] == 2 and om.stats["orders_filled"] == 1


# ── Instrument matching: exchange + token/symbol + product type ──


@freeze_time(SESSION_TIME_UTC)
def test_live_buy_ignores_a_delivery_holding_in_the_same_stock(tmp_path):
    """A DELIVERY holding is a different product: it neither fills the INTRADAY per-stock slot nor counts as INTRADAY capital."""
    broker = FakeBroker([row("2885", 300, product="DELIVERY", price="2500.0")])
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    result = buy(om)

    assert result is not None and result.success, result.message
    assert sent(broker) == [("BUY", "176")]


@freeze_time(SESSION_TIME_UTC)
def test_live_buy_does_not_match_the_same_symbol_on_another_exchange_as_this_instrument(tmp_path):
    """
    Same trading symbol on BSE is a different instrument from NSE RELIANCE.
    It must not trip the per-stock gate for NSE — but it is still an open
    INTRADAY position at the broker, so it counts towards the total.
    """
    bse = row("500325", 100, symbol="RELIANCE-EQ", exchange="BSE")
    om, _ = build(tmp_path, TradingMode.LIVE, FakeBroker([bse]))
    result = buy(om)
    assert result is not None and result.success, result.message  # not "already holding RELIANCE"

    broker = FakeBroker([bse])
    om, _ = build(tmp_path / "total", TradingMode.LIVE, broker, max_open_positions=1)
    result = buy(om)
    assert broker.placed == []
    assert result is not None and not result.success
    assert "max open positions" in result.message.lower(), result.message


@freeze_time(SESSION_TIME_UTC)
def test_live_buy_matches_by_exchange_and_token_even_when_trading_symbol_differs(tmp_path):
    """Same exchange + same token is this instrument, whatever the broker calls it."""
    broker = FakeBroker([row("2885", 176, symbol="RELIANCE")])
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    result = buy(om)

    assert broker.placed == []
    assert result is not None and not result.success
    assert "RELIANCE" in result.message


@freeze_time(SESSION_TIME_UTC)
def test_live_buy_ignores_closed_rows_with_zero_net_quantity(tmp_path):
    """A row the broker keeps for a position exited earlier today is not a position."""
    broker = FakeBroker([row("2885", 0)])
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    result = buy(om)

    assert result is not None and result.success, result.message
    assert sent(broker) == [("BUY", "176")]


# ── Feeding the broker's book to the risk gates must not block exits ──


@freeze_time(SESSION_TIME_UTC)
def test_live_sell_exit_is_not_blocked_because_the_broker_is_fully_deployed(tmp_path):
    """
    ₹72,00,000 in TCS (over the 70% deployable) plus 10 RELIANCE. A SELL of
    RELIANCE reduces exposure and takes no allocation: the deployed-capital
    gate (now judged against the broker's real book) must not reject it.
    """
    broker = FakeBroker([row("11536", 2118, symbol="TCS-EQ", price="3400.0"), row("2885", 10)])
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    result = om.execute_signal(
        make_signal(signal_type=SignalType.SELL, price=PRICE),
        make_analysis_result(action=AnalysisAction.SELL),
    )

    assert result is not None and result.success, result.message
    assert sent(broker) == [("SELL", "10")]


@freeze_time(SESSION_TIME_UTC)
def test_live_buy_is_still_blocked_when_the_broker_is_fully_deployed(tmp_path):
    broker = FakeBroker([row("11536", 2118, symbol="TCS-EQ", price="3400.0")])
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    result = buy(om)

    assert broker.placed == []
    assert result is not None and not result.success
    assert "capital fully deployed" in result.message.lower(), result.message


# ── A BUY already waiting at the broker ──────────────────────
#
# The position book shows nothing until an order fills. A BUY that is still
# working is exposure all the same: it counts towards the per-stock limit,
# the total-position limit and committed capital. Only its UNFILLED part is
# taken from the order book — the filled part is already in the position
# book — so nothing is counted twice.


def live_book(om: OrderManager, broker: FakeBroker):
    """The BrokerBook the live leg would be judged against for a BUY RELIANCE."""
    from data.models.market_data import OrderRequest
    from core.constants import ProductType
    order = OrderRequest(symbol="RELIANCE", token="2885", transaction_type=TransactionType.BUY,
                         quantity=0, price=PRICE, product_type=ProductType.INTRADAY)
    return om._broker_book(broker, order)


@freeze_time(SESSION_TIME_UTC)
def test_live_buy_is_blocked_when_a_buy_for_the_stock_is_already_working_and_position_is_flat(tmp_path):
    """Position book flat, BUY 176 RELIANCE open at the broker: a second BUY must not bypass the per-stock limit."""
    broker = FakeBroker([], orders=[working_buy("2885", 176, order_id="OLD1")])
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    result = buy(om)

    assert broker.placed == [], f"second BUY sent while one is still working: {broker.placed}"
    assert result is not None and not result.success
    assert "RELIANCE" in result.message and "OLD1" in result.message, result.message
    assert "working" in result.message.lower(), result.message


@freeze_time(SESSION_TIME_UTC)
def test_live_buy_is_blocked_when_a_buy_for_the_stock_is_partly_filled(tmp_path):
    """100 of 176 filled: the position book shows 100, the order 76 unfilled — one position, 176 in total."""
    broker = FakeBroker(
        [row("2885", 100)],
        orders=[working_buy("2885", 176, filled=100, status="partially filled", order_id="OLD1")],
    )
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    book = live_book(om, broker)
    assert book.held == 100 and book.pending_entry_qty == 76
    [reliance] = book.positions
    assert reliance.symbol == "RELIANCE" and reliance.quantity == 176  # 100 held + 76 unfilled, not 100 + 176
    assert reliance.invested_value == 176 * PRICE

    result = buy(om)

    assert broker.placed == []
    assert result is not None and not result.success
    assert "RELIANCE" in result.message and "76" in result.message and "OLD1" in result.message, result.message


@freeze_time(SESSION_TIME_UTC)
def test_live_buy_counts_a_working_buy_for_another_instrument_as_committed_capital(tmp_path):
    """
    Position book flat; LIMIT BUY 2000 TCS @3400 (₹68,00,000) still open.
    That capital is committed: a BUY for RELIANCE is sized from the
    ₹2,00,000 left of the ₹70,00,000 deployable — 70 shares, not 176.
    """
    broker = FakeBroker([], orders=[working_buy("11536", 2000, symbol="TCS-EQ", price="3400.0")])
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    result = buy(om)

    assert result is not None and result.success, result.message
    assert sent(broker) == [("BUY", str(int(2_00_000 / PRICE)))]  # 70


@freeze_time(SESSION_TIME_UTC)
def test_live_buy_counts_a_partly_filled_buy_for_another_instrument_exactly_once(tmp_path):
    """
    BUY 2000 TCS @3400: 1000 filled (position book: 1000 @3400 = ₹34L),
    1000 still open (order book: 1000 unfilled = ₹34L). Committed = ₹68L
    exactly — not ₹102L (held + whole order) and not ₹34L (held only).
    """
    broker = FakeBroker(
        [row("11536", 1000, symbol="TCS-EQ", price="3400.0")],
        orders=[working_buy("11536", 2000, filled=1000, symbol="TCS-EQ", price="3400.0",
                            status="partially filled")],
    )
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    book = live_book(om, broker)
    [tcs] = book.positions
    assert tcs.quantity == 2000 and tcs.invested_value == 68_00_000

    result = buy(om)

    assert result is not None and result.success, result.message
    # ₹70L − ₹68L = ₹2L left → 70 shares. Double counting would reject (fully deployed);
    # ignoring the unfilled half would allow the full 176.
    assert sent(broker) == [("BUY", "70")]


@freeze_time(SESSION_TIME_UTC)
def test_live_buy_counts_a_working_buy_for_another_instrument_towards_the_total_position_limit(tmp_path):
    broker = FakeBroker([], orders=[working_buy("11536", 10, symbol="TCS-EQ", price="3400.0")])
    om, _ = build(tmp_path, TradingMode.LIVE, broker, max_open_positions=1)

    result = buy(om)

    assert broker.placed == []
    assert result is not None and not result.success
    assert "max open positions" in result.message.lower(), result.message


@freeze_time(SESSION_TIME_UTC)
def test_live_buy_ignores_finished_orders_and_working_sells(tmp_path):
    """Complete / cancelled / rejected BUYs are history; a working SELL is an exit, not an entry."""
    broker = FakeBroker(
        [row("11536", 1000, symbol="TCS-EQ", price="3400.0")],  # 34L held
        orders=[
            working_buy("2885", 50, status="cancelled", order_id="C1"),
            working_buy("2885", 50, status="rejected", order_id="R1"),
            working_buy("11536", 1000, filled=1000, symbol="TCS-EQ", price="3400.0", status="complete", order_id="F1"),
            working_buy("11536", 1000, symbol="TCS-EQ", price="3400.0", side="SELL", order_id="S1"),
        ],
    )
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    book = live_book(om, broker)
    assert book.pending_entry_qty == 0
    [tcs] = book.positions
    assert tcs.quantity == 1000  # the complete BUY is in the position book already; the SELL is not exposure

    result = buy(om)

    assert result is not None and result.success, result.message
    assert sent(broker) == [("BUY", "176")]  # 70L − 34L = 36L left ≥ the 5L per-trade cap


# ── Matching working orders: exchange + token/symbol + product ──


@freeze_time(SESSION_TIME_UTC)
def test_working_buy_for_the_same_symbol_on_another_exchange_is_not_this_instrument(tmp_path):
    """RELIANCE-EQ on BSE is not NSE RELIANCE: no per-stock block, but it is still a pending position."""
    bse = working_buy("500325", 100, symbol="RELIANCE-EQ", exchange="BSE", price="2830.0")
    om, _ = build(tmp_path, TradingMode.LIVE, FakeBroker([], orders=[bse]))
    assert live_book(om, FakeBroker([], orders=[bse])).pending_entry_qty == 0
    result = buy(om)
    assert result is not None and result.success, result.message

    broker = FakeBroker([], orders=[bse])
    om, _ = build(tmp_path / "total", TradingMode.LIVE, broker, max_open_positions=1)
    result = buy(om)
    assert broker.placed == [] and result is not None and not result.success
    assert "max open positions" in result.message.lower(), result.message


@freeze_time(SESSION_TIME_UTC)
def test_working_delivery_buy_in_the_same_stock_is_ignored(tmp_path):
    broker = FakeBroker([], orders=[working_buy("2885", 300, product="DELIVERY")])
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    result = buy(om)

    assert result is not None and result.success, result.message
    assert sent(broker) == [("BUY", "176")]


@freeze_time(SESSION_TIME_UTC)
def test_working_buy_matched_by_exchange_and_token_when_the_symbol_differs(tmp_path):
    broker = FakeBroker([], orders=[working_buy("2885", 10, symbol="RELIANCE", order_id="T1")])
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    result = buy(om)

    assert broker.placed == []
    assert result is not None and not result.success
    assert "T1" in result.message, result.message


# ── Order book that cannot be verified blocks the entry, and says why ──


@freeze_time(SESSION_TIME_UTC)
def test_live_buy_is_blocked_when_the_order_book_cannot_be_read(tmp_path):
    broker = FakeBroker([], orderbook_fail=True)  # position book fine and flat
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    result = buy(om)

    assert broker.placed == []
    assert result is not None and not result.success
    assert "order book could not be read" in result.message.lower(), result.message
    assert "working" in result.message.lower(), result.message


@freeze_time(SESSION_TIME_UTC)
def test_live_buy_is_blocked_when_the_broker_rejects_the_order_book_query(tmp_path):
    broker = FakeBroker([], orderbook_reject=True)
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    result = buy(om)

    assert broker.placed == []
    assert result is not None and not result.success
    assert "order book could not be read" in result.message.lower(), result.message


@freeze_time(SESSION_TIME_UTC)
def test_live_buy_is_blocked_when_a_working_buy_has_no_exchange(tmp_path):
    broker = FakeBroker([], orders=[working_buy("11536", 10, symbol="TCS-EQ", exchange=None, order_id="X1")])
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    result = buy(om)

    assert broker.placed == []
    assert result is not None and not result.success
    assert "X1" in result.message and "exchange" in result.message.lower(), result.message


@freeze_time(SESSION_TIME_UTC)
def test_live_buy_is_blocked_when_a_working_market_buy_elsewhere_cannot_be_valued(tmp_path):
    """An unfilled MARKET BUY on TCS has no price: the capital it commits is unknown, not free."""
    broker = FakeBroker([], orders=[working_buy("11536", 100, symbol="TCS-EQ", ordertype="MARKET", order_id="M1")])
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    result = buy(om)

    assert broker.placed == []
    assert result is not None and not result.success
    assert "M1" in result.message and "no price" in result.message.lower(), result.message


@freeze_time(SESSION_TIME_UTC)
def test_working_market_buy_for_this_instrument_is_valued_at_the_signal_price(tmp_path):
    broker = FakeBroker([], orders=[working_buy("2885", 176, ordertype="MARKET", order_id="M2")])
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    [reliance] = live_book(om, broker).positions
    assert reliance.quantity == 176 and reliance.avg_buy_price == PRICE

    result = buy(om)
    assert broker.placed == [] and result is not None and not result.success
    assert "M2" in result.message, result.message


@freeze_time(SESSION_TIME_UTC)
def test_partly_filled_market_buy_elsewhere_is_valued_at_its_average_fill_price(tmp_path):
    """MARKET BUY 2000 TCS, 1000 filled @3400: the unfilled 1000 is valued at that fill price."""
    broker = FakeBroker(
        [row("11536", 1000, symbol="TCS-EQ", price="3400.0")],
        orders=[working_buy("11536", 2000, filled=1000, symbol="TCS-EQ", price="3400.0",
                            ordertype="MARKET", status="partially filled")],
    )
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    [tcs] = live_book(om, broker).positions
    assert tcs.quantity == 2000 and tcs.invested_value == 68_00_000

    result = buy(om)
    assert result is not None and result.success, result.message
    assert sent(broker) == [("BUY", "70")]


# ── DUAL and SELL are unaffected in the ways that matter ─────


@freeze_time(SESSION_TIME_UTC)
def test_dual_working_buy_at_broker_blocks_live_leg_only(tmp_path):
    broker = FakeBroker([], orders=[working_buy("2885", 176, order_id="OLD1")])
    om, paper = build(tmp_path, TradingMode.DUAL, broker)

    result = buy(om)

    assert_paper_filled(result, 176)
    assert paper._positions["RELIANCE"].quantity == 176
    assert_live_rejected(result, broker, "OLD1", "working")


@freeze_time(SESSION_TIME_UTC)
def test_dual_unreadable_order_book_blocks_live_leg_only(tmp_path):
    broker = FakeBroker([], orderbook_fail=True)
    om, paper = build(tmp_path, TradingMode.DUAL, broker)

    result = buy(om)

    assert_paper_filled(result, 176)
    assert_live_rejected(result, broker, "order book could not be read")


@freeze_time(SESSION_TIME_UTC)
def test_live_sell_holding_is_the_position_book_only_not_the_working_buy(tmp_path):
    """Held 10, BUY 176 still working: a SELL may exit the 10 — the working BUY is not a holding."""
    broker = FakeBroker([row("2885", 10)], orders=[working_buy("2885", 176, order_id="OLD1")])
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    result = om.execute_signal(
        make_signal(signal_type=SignalType.SELL, price=PRICE),
        make_analysis_result(action=AnalysisAction.SELL),
    )

    assert result is not None and result.success, result.message
    assert sent(broker) == [("SELL", "10")]
