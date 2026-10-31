"""
Square-off and live entries across instances sharing the same storage.

When one instance squares off, no instance sharing its storage may place a
new entry during or after it — not one whose signal was approved a moment
earlier, and not one that starts later. The entry hold must be durable and
shared, like the entry reservations, and square-off must take the same
cross-process entry lock the BUY decision takes, in the same order as the
in-process square-off lock, so the two can never deadlock. Genuine exits
stay available throughout, and the next trading day reopens entries.

Two kinds of test: threads (two OrderManagers in one process, a barrier in
the broker to force overlap) and real separate processes (fork) sharing a
file-backed broker — the only way to prove the flock and the hold file do
their job across process boundaries.

No real orders are placed.
"""

from __future__ import annotations

import fcntl
import json
import multiprocessing
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from freezegun import freeze_time

from core.constants import AnalysisAction, SignalType
from core.security.audit import AuditTrail
from tests.factories import make_analysis_result, make_signal
from tests.trading.test_live_squareoff import (
    RELIANCE,
    TCS,
    FakeBroker,
    FakeLiveSession,
    broker_row,
    sent,
)
from trading.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
from trading.entry_reservation_store import EntryReservationStore
from trading.exit_intent_store import ExitIntentStore
from trading.order_manager import OrderManager, TradingMode
from trading.paper_trader import PaperTrader
from trading.risk_manager import RiskConfig, RiskManager

SQUAREOFF_TIME_UTC = "2026-03-04 09:45:00"   # 15:15 IST Wednesday
NEXT_DAY_UTC = "2026-03-05 04:30:00"         # 10:00 IST Thursday
CAPITAL = 1_00_00_000.0


def build(tmp_path: Path, broker, shared: Path, **risk) -> OrderManager:
    """An instance: its own audit/paper state, the SHARED reservation + intent stores."""
    audit = AuditTrail(log_dir=tmp_path / "audit")
    cfg = RiskConfig(**{"total_capital": CAPITAL, **risk})
    return OrderManager(
        risk_manager=RiskManager(config=cfg, audit=audit),
        circuit_breaker=CircuitBreaker(config=CircuitBreakerConfig(), audit=audit, total_capital=cfg.total_capital),
        paper_trader=PaperTrader(capital=cfg.total_capital, slippage_pct=0.0, audit=audit),
        audit=audit, mode=TradingMode.LIVE, live_session=FakeLiveSession(broker),
        exit_intents=ExitIntentStore(shared / "exit_intents"),
        entry_reservations=EntryReservationStore(shared / "entry_reservations"),
    )


def buy(om: OrderManager, symbol="TCS", token="11536", price=3400.0):
    return om.execute_signal(
        make_signal(symbol=symbol, token=token, signal_type=SignalType.BUY, price=price),
        make_analysis_result(action=AnalysisAction.BUY, signal_symbol=symbol),
    )


def sell(om: OrderManager, symbol="TCS", token="11536", price=3400.0):
    return om.execute_signal(
        make_signal(symbol=symbol, token=token, signal_type=SignalType.SELL, price=price),
        make_analysis_result(action=AnalysisAction.SELL, signal_symbol=symbol),
    )


def held(instrument: dict, qty: int, price: str = "2830.0") -> dict:
    """A position row with its average price (the BUY guard values deployed capital from it)."""
    return {**broker_row(instrument, qty), "buyavgprice": price}


def buys(broker) -> list:
    return [p for p in sent(broker) if p[0] == "BUY"]


def open_buys(broker) -> list[dict]:
    return [o for o in broker.orders if o["transactiontype"] == "BUY" and o["status"] == "open"]


# ── Threads: two instances in one process ────────────────────


@freeze_time(SQUAREOFF_TIME_UTC)
def test_buy_waiting_while_another_instance_squares_off_is_refused_afterwards(tmp_path):
    """
    Instance A starts square-off (long 10 RELIANCE). Instance B's BUY TCS
    arrives while A is inside the broker's order book. B must not get in
    during A's square-off, and must be refused after it: entries are closed.
    """
    barrier = threading.Barrier(2, timeout=0.5)
    broker = FakeBroker(rows=[held(RELIANCE, 10)], rendezvous=("orderBook", barrier))
    shared = tmp_path / "shared"
    a, b = build(tmp_path / "a", broker, shared), build(tmp_path / "b", broker, shared)
    outcome: dict = {}

    def square_off():
        outcome["report"] = a.square_off_intraday()

    def entry():
        outcome["buy"] = buy(b)

    t_a, t_b = threading.Thread(target=square_off), threading.Thread(target=entry)
    t_a.start()
    time.sleep(0.05)  # A is already inside its square-off when B's signal arrives
    t_b.start()
    t_a.join(10)
    t_b.join(10)

    assert buys(broker) == [], f"a BUY got in around another instance's square-off: {sent(broker)}"
    assert open_buys(broker) == []
    result = outcome["buy"]
    assert result is not None and not result.success
    assert "closed" in result.message.lower(), result.message
    report = outcome["report"]
    assert [x.trading_symbol for x in report.live_placed] == ["RELIANCE-EQ"]  # A's exit went through


@freeze_time(SQUAREOFF_TIME_UTC)
def test_instance_started_after_square_off_is_refused_an_entry(tmp_path):
    broker = FakeBroker(rows=[held(RELIANCE, 10)])
    shared = tmp_path / "shared"
    build(tmp_path / "a", broker, shared).square_off_intraday()

    later = build(tmp_path / "later", broker, shared)  # started after the square-off, knows nothing
    result = buy(later)

    assert buys(broker) == [], f"a later instance placed a BUY after square-off: {sent(broker)}"
    assert result is not None and not result.success
    assert "closed" in result.message.lower() and "square-off" in result.message.lower(), result.message


@freeze_time(SQUAREOFF_TIME_UTC)
def test_buy_approved_in_one_instance_before_square_off_in_another_ends_cancelled_not_working(tmp_path):
    """
    B's risk gates approve a BUY; at that instant A starts square-off. Either
    B's order is placed before A's square-off and A cancels it, or A finishes
    first and B is refused. What may NOT happen: a working BUY after square-off.
    """
    broker = FakeBroker(rows=[held(RELIANCE, 10)])
    shared = tmp_path / "shared"
    a, b = build(tmp_path / "a", broker, shared), build(tmp_path / "b", broker, shared)
    square_off = threading.Thread(target=lambda: setattr(a, "_report", a.square_off_intraday()))
    real_evaluate = b._risk_manager.evaluate

    def evaluate_then_start_square_off(*args, **kwargs):
        decision = real_evaluate(*args, **kwargs)
        square_off.start()
        time.sleep(0.1)  # A's square-off is under way before B places
        return decision

    b._risk_manager.evaluate = evaluate_then_start_square_off

    result = buy(b)
    square_off.join(10)

    assert open_buys(broker) == [], f"a BUY is still working after square-off: {broker.orders}"
    if result is not None and result.success:
        assert broker.cancel_requests == [result.broker_order_id], "B's BUY was placed but A's square-off did not cancel it"
        assert [c.order_id for c in a._report.live_cancelled] == [result.broker_order_id]
    else:
        assert result is not None and "closed" in result.message.lower(), result.message


@freeze_time(SQUAREOFF_TIME_UTC)
def test_genuine_exits_stay_available_to_other_instances_while_entries_are_closed(tmp_path):
    """A's square-off exit for TCS is rejected by the broker; a later instance's SELL TCS must still go."""
    broker = FakeBroker(rows=[held(RELIANCE, 10), held(TCS, 5, "3400.0")], reject={"TCS-EQ"})
    shared = tmp_path / "shared"
    report = build(tmp_path / "a", broker, shared).square_off_intraday()
    assert [x.trading_symbol for x in report.live_rejected] == ["TCS-EQ"]
    broker.reject.clear()

    later = build(tmp_path / "later", broker, shared)
    assert not buy(later).success                      # entries closed for it too …
    result = sell(later)                               # … but its exit of the 5 TCS is allowed

    assert result is not None and result.success, result.message
    assert ("SELL", "TCS-EQ", "11536", "NSE", "INTRADAY", "5") in sent(broker)


def test_shared_entry_hold_expires_with_the_trading_day_but_not_with_a_same_day_reset(tmp_path):
    shared = tmp_path / "shared"
    with freeze_time(SQUAREOFF_TIME_UTC):
        broker = FakeBroker(rows=[held(RELIANCE, 10)])
        build(tmp_path / "a", broker, shared).square_off_intraday()

        same_day = build(tmp_path / "same_day", FakeBroker(rows=None), shared)
        same_day._risk_manager.reset_daily()           # pre-market replayed after a restart, same day
        assert not buy(same_day).success, "a same-day reset reopened entries"

    with freeze_time(NEXT_DAY_UTC):
        fresh_broker = FakeBroker(rows=None)
        next_day = build(tmp_path / "next_day", fresh_broker, shared)
        next_day._risk_manager.reset_daily()
        result = buy(next_day)
        assert result is not None and result.success, result.message
        assert buys(fresh_broker) == [("BUY", "TCS-EQ", "11536", "NSE", "INTRADAY", "147")]


# ── Real separate processes sharing a file-backed broker ─────


class FileBroker:
    """
    SmartConnect stand-in whose books live in files, so processes share
    one broker. Every read/write is under a flock. `rendezvous`: callers
    of that API leave a marker and wait up to 0.5s for a second caller —
    two callers that both get in have both decided on the same state.
    """

    def __init__(self, root: Path, rendezvous: str | None = None):
        self.root = Path(root)
        self.rendezvous = rendezvous
        self.root.mkdir(parents=True, exist_ok=True)
        if not (self.root / "state.json").exists():
            self._save({"rows": [], "orders": [], "placed": [], "hidden": [], "cancels": []})

    @contextmanager
    def _locked(self):
        fd = os.open(self.root / "broker.lock", os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _load(self) -> dict:
        return json.loads((self.root / "state.json").read_text())

    def _save(self, state: dict) -> None:
        tmp = self.root / "state.json.tmp"
        tmp.write_text(json.dumps(state))
        os.replace(tmp, self.root / "state.json")

    def _meet(self, api: str) -> None:
        if self.rendezvous != api:
            return
        (self.root / f"arrive_{os.getpid()}").touch()
        for _ in range(100):  # ≈0.5s; a loop count, not a clock — the clock is frozen in these tests
            if len(list(self.root.glob("arrive_*"))) >= 2:
                break
            time.sleep(0.005)

    # ── books ──
    def position(self):
        with self._locked():
            rows = self._load()["rows"]
        return {"status": True, "message": "SUCCESS", "data": rows or None}

    def orderBook(self):
        with self._locked():
            state = self._load()
            listed = [o for o in state["orders"] if o["orderid"] not in state["hidden"]]
        self._meet("orderBook")
        return {"status": True, "message": "SUCCESS", "data": listed or None}

    def individual_order_details(self, unique_id):
        with self._locked():
            state = self._load()
        order = next((o for o in state["orders"] if o["uniqueorderid"] == unique_id), None)
        if order is None or order["orderid"] in state["hidden"]:
            return {"status": False, "message": "order not found", "data": None}
        return {"status": True, "message": "SUCCESS", "data": dict(order)}

    # ── orders ──
    def placeOrder(self, params):
        with self._locked():
            state = self._load()
            state["placed"].append(dict(params))
            order_id = f"BRK{len(state['placed'])}"
            state["orders"].append({
                "orderid": order_id, "uniqueorderid": f"U-{order_id}", "status": "open", "orderstatus": "open",
                "transactiontype": params["transactiontype"], "producttype": params["producttype"],
                "tradingsymbol": params["tradingsymbol"], "symboltoken": params["symboltoken"],
                "exchange": params["exchange"], "ordertype": params["ordertype"],
                "quantity": params["quantity"], "filledshares": "0", "unfilledshares": params["quantity"],
                "price": params["price"] if params["ordertype"] == "LIMIT" else "0",
                "averageprice": "0", "ordertag": params.get("ordertag", ""),
            })
            self._save(state)
        return {"status": True, "message": "SUCCESS", "data": {"orderid": order_id, "uniqueorderid": f"U-{order_id}"}}

    placeOrderFullResponse = placeOrder

    def cancelOrder(self, order_id, variety="NORMAL"):
        with self._locked():
            state = self._load()
            state["cancels"].append(order_id)
            for o in state["orders"]:
                if o["orderid"] == order_id:
                    o["status"] = o["orderstatus"] = "cancelled"
            self._save(state)
        return {"status": True, "message": "SUCCESS", "data": {"orderid": order_id}}

    # ── test side ──
    def seed(self, rows=(), hidden=()) -> None:
        with self._locked():
            state = self._load()
            state["rows"] = list(rows)
            state["hidden"] = list(hidden)
            self._save(state)

    def state(self) -> dict:
        with self._locked():
            return self._load()


SMALL = dict(total_capital=1_00_000.0, max_capital_deployed_pct=60.0,
             max_capital_per_trade_pct=50.0, min_order_value=1_000.0)


def _child(kind: str, when: str, tmp: str, shared: str, broker_dir: str, rendezvous, result_path: str,
           symbol: str, token: str, price: float, risk: dict) -> None:
    """Runs in a forked process: one BUY signal or one square-off, outcome written to result_path."""
    out: dict = {}
    try:
        with freeze_time(when):
            broker = FileBroker(Path(broker_dir), rendezvous=rendezvous)
            om = build(Path(tmp), broker, Path(shared), **risk)
            if kind == "buy":
                r = buy(om, symbol, token, price)
                out = {"success": r.success, "message": r.message, "order_id": r.broker_order_id}
            else:
                report = om.square_off_intraday()
                out = {"placed": [x.trading_symbol for x in report.live_placed],
                       "cancelled": [c.order_id for c in report.live_cancelled],
                       "error": report.live_error}
    except Exception as e:  # surfaced to the parent rather than lost
        out = {"exception": repr(e)}
    Path(result_path).write_text(json.dumps(out))


def run_processes(tmp_path: Path, jobs: list[dict], *, rendezvous="orderBook", stagger=0.0, risk=None) -> list[dict]:
    """
    Start one real process per job (fork), all sharing tmp_path/shared and
    the file-backed broker in tmp_path/broker. Returns each job's outcome.
    """
    ctx = multiprocessing.get_context("fork")
    broker_dir, shared = tmp_path / "broker", tmp_path / "shared"
    procs, paths = [], []
    for i, job in enumerate(jobs):
        result_path = tmp_path / f"result_{i}.json"
        paths.append(result_path)
        p = ctx.Process(target=_child, args=(
            job["kind"], job.get("when", SQUAREOFF_TIME_UTC), str(tmp_path / f"p{i}"), str(shared),
            str(broker_dir), rendezvous, str(result_path),
            job.get("symbol", "TCS"), job.get("token", "11536"), job.get("price", 100.0), risk or {},
        ))
        procs.append(p)
        p.start()
        if stagger:
            time.sleep(stagger)
    for p in procs:
        p.join(20)
        assert p.exitcode == 0, f"child exited with {p.exitcode}"
    results = [json.loads(p.read_text()) for p in paths]
    for r in results:
        assert "exception" not in r, r
    return results


def placed_buys(broker: FileBroker) -> list[tuple[str, str]]:
    return [(p["tradingsymbol"], p["quantity"]) for p in broker.state()["placed"] if p["transactiontype"] == "BUY"]


def test_processes_racing_for_remaining_capital_cannot_both_take_it(tmp_path):
    """₹60,000 deployable, ₹50,000 per trade: two processes buying A and B at once must total ≤ ₹60,000."""
    broker = FileBroker(tmp_path / "broker")
    broker.seed(hidden=["BRK1", "BRK2"])  # neither order is listed yet: only the reservations can tell

    results = run_processes(tmp_path, [
        {"kind": "buy", "symbol": "AAA", "token": "111", "price": 100.0},
        {"kind": "buy", "symbol": "BBB", "token": "222", "price": 100.0},
    ], risk=SMALL)

    assert all(r["success"] for r in results), results
    quantities = sorted(int(q) for _, q in placed_buys(broker))
    assert quantities == [100, 500], f"both processes sized against the same balance: {placed_buys(broker)}"


def test_processes_racing_for_the_last_position_slot_cannot_both_take_it(tmp_path):
    broker = FileBroker(tmp_path / "broker")
    broker.seed(hidden=["BRK1", "BRK2"])

    results = run_processes(tmp_path, [
        {"kind": "buy", "symbol": "AAA", "token": "111", "price": 100.0},
        {"kind": "buy", "symbol": "BBB", "token": "222", "price": 100.0},
    ], risk={**SMALL, "max_open_positions": 1})

    assert len(placed_buys(broker)) == 1, f"both processes took the last slot: {placed_buys(broker)}"
    assert sorted(r["success"] for r in results) == [False, True], results
    loser = next(r for r in results if not r["success"])
    assert "max open positions" in loser["message"].lower(), loser["message"]


def test_process_squaring_off_while_another_process_buys_leaves_no_working_entry(tmp_path):
    broker = FileBroker(tmp_path / "broker")
    broker.seed(rows=[held(RELIANCE, 10)])

    results = run_processes(tmp_path, [
        {"kind": "square_off"},
        {"kind": "buy", "symbol": "TCS", "token": "11536", "price": 3400.0},
    ], stagger=0.05)

    square_off, entry = results
    assert square_off["error"] is None and square_off["placed"] == ["RELIANCE-EQ"], square_off
    state = broker.state()
    assert [o for o in state["orders"] if o["transactiontype"] == "BUY" and o["status"] == "open"] == [], (
        f"a BUY is working after another process's square-off: {state['orders']}"
    )
    if entry["success"]:
        assert entry["order_id"] in state["cancels"], "BUY placed before square-off was not cancelled by it"
    else:
        assert "closed" in entry["message"].lower(), entry["message"]


def test_process_attempting_an_entry_after_another_process_squared_off_is_refused(tmp_path):
    broker = FileBroker(tmp_path / "broker")
    broker.seed(rows=[held(RELIANCE, 10)])
    [square_off] = run_processes(tmp_path, [{"kind": "square_off"}], rendezvous=None)
    assert square_off["placed"] == ["RELIANCE-EQ"]

    [entry] = run_processes(tmp_path, [{"kind": "buy", "symbol": "TCS", "token": "11536", "price": 3400.0}],
                            rendezvous=None)

    assert placed_buys(broker) == [], f"BUY placed after another process's square-off: {placed_buys(broker)}"
    assert not entry["success"] and "closed" in entry["message"].lower(), entry["message"]


# ── Square-off while a BUY is inside its broker call ─────────
#
# The only entry square-off cannot see coming is one already inside
# placeOrder when the hold is published. Square-off must: publish the hold
# first, say that it ran without the lock, still place its exits, report the
# reservation it found with no listed order as UNRECONCILED (not settled),
# and keep reporting it through confirm_square_off() until the broker shows
# the order — a flat position snapshot is not settlement.


class HeldCallBroker(FakeBroker):
    """BUY placeOrder calls block (the broker is slow to answer) until `release` is set."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.in_call = threading.Event()
        self.release = threading.Event()

    def placeOrder(self, params):
        if params["transactiontype"] == "BUY":
            self.in_call.set()
            assert self.release.wait(10), "test did not release the held broker call"
        return super().placeOrder(params)

    placeOrderFullResponse = placeOrder


def build_fast_lock(tmp_path: Path, broker, shared: Path, lock_timeout: float) -> OrderManager:
    om = build(tmp_path, broker, shared)
    om._entry_reservations = EntryReservationStore(shared / "entry_reservations", lock_timeout=lock_timeout)
    return om


@freeze_time(SQUAREOFF_TIME_UTC)
def test_buy_reply_arriving_after_the_square_off_snapshot_is_reported_unreconciled_not_settled(tmp_path):
    broker = HeldCallBroker(rows=[held(RELIANCE, 10)])
    shared = tmp_path / "shared"
    b = build_fast_lock(tmp_path / "b", broker, shared, lock_timeout=30.0)
    a = build_fast_lock(tmp_path / "a", broker, shared, lock_timeout=0.2)  # A falls back quickly
    outcome: dict = {}
    entry = threading.Thread(target=lambda: outcome.__setitem__("buy", buy(b)))
    entry.start()
    assert broker.in_call.wait(5), "B never reached the broker"   # B holds the entry lock, inside placeOrder

    report = a.square_off_intraday()                                # snapshot taken while B's call is pending

    assert [x.trading_symbol for x in report.live_placed] == ["RELIANCE-EQ"]   # exits still went
    assert report.live_lock_skipped and "WITHOUT the entry lock" in report.live_lock_skipped
    [pending] = report.live_unreconciled
    assert pending.trading_symbol == "TCS-EQ" and pending.quantity == 147 and pending.status == "reserved"
    assert "no acknowledgement was stored" in pending.detail
    assert report.broker_flat is False and not report.settled and report.needs_attention
    assert a._entry_reservations.entries_closed("20260304") is not None   # hold published

    broker.release.set()                                            # the broker now answers B
    entry.join(10)
    result = outcome["buy"]
    assert result is not None and result.success and result.broker_order_id == "BRK2"  # honest: it WAS placed
    assert open_buys(broker) and open_buys(broker)[0]["orderid"] == "BRK2"             # and it is working

    # Until it is reconciled, confirmation keeps saying so — even though the
    # position book would look flat once RELIANCE's exit fills.
    broker.fill("BRK1")
    conf = a.confirm_square_off()
    assert conf.flat is True and not conf.confirmed_flat
    assert any("TCS-EQ" in line and "BRK2" in line for line in conf.unreconciled_entries), conf.unreconciled_entries
    assert "NOT settled" in conf.describe()

    # A later square-off (replay / operator) finds it listed and cancels it; then it settles.
    again = a.square_off_intraday()
    assert [c.order_id for c in again.live_cancelled] == ["BRK2"]
    assert again.live_unreconciled == []
    conf = a.confirm_square_off()
    assert conf.confirmed_flat, conf.describe()
    assert a._entry_reservations.unresolved() == []                 # released: cancelled, nothing filled


@freeze_time(SQUAREOFF_TIME_UTC)
def test_square_off_with_the_lock_held_is_settled_and_says_so(tmp_path):
    broker = FakeBroker(rows=[held(RELIANCE, 10)])
    report = build(tmp_path / "a", broker, tmp_path / "shared").square_off_intraday()
    assert report.live_lock_skipped is None and report.live_unreconciled == []
    assert report.broker_flat is False   # RELIANCE still open (exit placed, not filled)
    broker.fill("BRK1")
    flat = build(tmp_path / "b", broker, tmp_path / "shared").square_off_intraday()
    assert flat.broker_flat is True and flat.settled and not flat.needs_attention


@freeze_time(SQUAREOFF_TIME_UTC)
def test_late_entry_that_is_listed_by_the_end_of_square_off_is_cancelled_in_the_same_run(tmp_path):
    """B's order gets listed while A is still placing exits: A's final re-read cancels it."""
    broker = HeldCallBroker(rows=[held(RELIANCE, 10)])
    shared = tmp_path / "shared"
    b = build_fast_lock(tmp_path / "b", broker, shared, lock_timeout=30.0)
    a = build_fast_lock(tmp_path / "a", broker, shared, lock_timeout=0.2)
    real_place_exit = a._place_live_exit

    def place_exit_then_broker_answers_b(*args, **kwargs):
        out = real_place_exit(*args, **kwargs)
        broker.release.set()   # B's reply arrives while A is mid-square-off
        time.sleep(0.1)
        return out

    a._place_live_exit = place_exit_then_broker_answers_b
    entry = threading.Thread(target=lambda: buy(b))
    entry.start()
    assert broker.in_call.wait(5)

    report = a.square_off_intraday()
    entry.join(10)

    assert [c.order_id for c in report.live_cancelled] == ["BRK2"]
    assert report.live_unreconciled == [] and open_buys(broker) == []
    assert report.live_lock_skipped and report.needs_attention   # still honest about running without the lock
