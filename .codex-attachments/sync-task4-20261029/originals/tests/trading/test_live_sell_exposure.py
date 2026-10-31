"""
LIVE / DUAL mode: a SELL must never add exposure at the broker.

OrderManager.execute_signal() builds orders with quantity=0, lets the risk
manager size them from capital, and only then writes the computed quantity
into the order. For a SELL that means the quantity sent to the broker is the
capital-based size (e.g. 176 shares), not the holding (e.g. 10) — which the
broker fills as an MIS short for the difference.

The paper position book is not proof of anything the broker holds, so a
live SELL must be sized against holdings *verified at the broker*; if they
cannot be verified, the SELL is blocked. BUYs are unaffected.
"""

from __future__ import annotations

import pytest
from freezegun import freeze_time

from core.constants import AnalysisAction, SignalType, TransactionType
from core.security.audit import AuditTrail
from tests.factories import make_analysis_result, make_order_request, make_signal
from trading.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
from trading.exit_intent_store import ExitIntentStore
from trading.order_manager import OrderManager, TradingMode
from trading.paper_trader import PaperTrader
from trading.risk_manager import RiskConfig, RiskManager

# 10:00 IST on a Wednesday (UTC+5:30) — inside market hours for every gate.
SESSION_TIME_UTC = "2026-03-04 04:30:00"
CAPITAL = 1_00_00_000.0  # 1 Cr → 5% per trade ≈ 176 shares at ₹2830


class FakeBroker:
    """Stand-in for SmartConnect: position() + placeOrder()."""

    def __init__(self, holdings: dict[str, int] | None = None, *, positions_fail: bool = False):
        self.holdings = holdings or {}  # token -> net quantity (INTRADAY)
        self.positions_fail = positions_fail
        self.placed: list[dict] = []

    def position(self):
        if self.positions_fail:
            raise RuntimeError("position API unavailable")
        data = [
            {
                "tradingsymbol": f"SYM{token}-EQ",
                "symboltoken": token,
                "producttype": "INTRADAY",
                "netqty": str(qty),
            }
            for token, qty in self.holdings.items()
        ]
        return {"status": True, "message": "SUCCESS", "data": data or None}

    def placeOrder(self, params):
        self.placed.append(dict(params))
        return {"status": True, "data": {"orderid": f"BRK{len(self.placed)}"}}

    def orderBook(self):
        return {"status": True, "message": "SUCCESS", "data": None}  # no orders today


class FakeLiveSession:
    def __init__(self, broker: FakeBroker | None):
        self._broker = broker

    def get_api(self):
        if self._broker is None:
            raise RuntimeError("not connected")
        return self._broker


def build(tmp_path, mode: TradingMode, broker: FakeBroker | None, paper_holding: int = 0):
    audit = AuditTrail(log_dir=tmp_path)
    risk = RiskManager(config=RiskConfig(total_capital=CAPITAL), audit=audit)
    cb = CircuitBreaker(config=CircuitBreakerConfig(), audit=audit, total_capital=CAPITAL)
    paper = PaperTrader(capital=CAPITAL, slippage_pct=0.0, audit=audit)
    om = OrderManager(
        risk_manager=risk, circuit_breaker=cb, paper_trader=paper, audit=audit,
        mode=mode, live_session=FakeLiveSession(broker),
        # A live SELL is recorded as a durable exit intent (shared with square-off).
        exit_intents=ExitIntentStore(tmp_path / "intents"),
    )
    if paper_holding:
        # Seed a small paper holding directly, so the paper book "believes"
        # we are long regardless of what the broker actually holds.
        seeded = paper.place_order(
            make_order_request(transaction_type=TransactionType.BUY, quantity=paper_holding, price=2830.0)
        )
        assert seeded.success and paper._positions["RELIANCE"].quantity == paper_holding
    return om, paper


def sell(om: OrderManager):
    return om.execute_signal(
        make_signal(signal_type=SignalType.SELL, price=2830.0),
        make_analysis_result(action=AnalysisAction.SELL),
    )


@freeze_time(SESSION_TIME_UTC)
def test_live_sell_is_capped_to_verified_broker_holding_not_capital_size(tmp_path):
    """Holding 10 at the broker, capital sizing says 176: the broker must get SELL 10."""
    broker = FakeBroker(holdings={"2885": 10})
    om, _ = build(tmp_path, TradingMode.LIVE, broker, paper_holding=10)

    result = sell(om)

    assert result is not None and result.success, result.message
    assert len(broker.placed) == 1
    sent = broker.placed[0]
    assert sent["transactiontype"] == "SELL"
    assert sent["quantity"] == "10", (
        f"broker received SELL {sent['quantity']} with only 10 held — flips to short"
    )
    # Placed for 10; the broker only acknowledged it, so no fill is claimed.
    assert result.placed_qty == 10 and result.filled_qty == 0


@freeze_time(SESSION_TIME_UTC)
def test_dual_mode_paper_book_is_not_proof_of_broker_holding(tmp_path):
    """Paper book long 10, broker flat: the live leg must not send a SELL; paper exit still runs."""
    broker = FakeBroker(holdings={})
    om, paper = build(tmp_path, TradingMode.DUAL, broker, paper_holding=10)

    result = sell(om)

    assert broker.placed == [], f"live SELL sent with no broker holding: {broker.placed}"
    # Paper leg: a genuine paper exit of the 10 held.
    assert result is not None and result.success and result.is_paper
    assert result.filled_qty == 10
    assert "RELIANCE" not in paper._positions


@freeze_time(SESSION_TIME_UTC)
def test_live_sell_is_blocked_when_broker_holdings_cannot_be_verified(tmp_path):
    broker = FakeBroker(holdings={"2885": 10}, positions_fail=True)
    om, _ = build(tmp_path, TradingMode.LIVE, broker, paper_holding=10)

    result = sell(om)

    assert broker.placed == [], "SELL sent without verified holdings"
    assert result is not None and not result.success
    assert "verif" in result.message.lower(), result.message
    assert "exposure" in result.message.lower(), result.message


@freeze_time(SESSION_TIME_UTC)
def test_live_sell_with_no_broker_holding_is_blocked_as_short_entry(tmp_path):
    broker = FakeBroker(holdings={})
    om, _ = build(tmp_path, TradingMode.LIVE, broker, paper_holding=0)

    result = sell(om)

    assert broker.placed == []
    assert result is not None and not result.success
    assert "short" in result.message.lower(), result.message


@freeze_time(SESSION_TIME_UTC)
def test_live_buy_is_unaffected(tmp_path):
    broker = FakeBroker(holdings={})
    om, _ = build(tmp_path, TradingMode.LIVE, broker)

    result = om.execute_signal(
        make_signal(signal_type=SignalType.BUY, price=2830.0),
        make_analysis_result(action=AnalysisAction.BUY),
    )

    assert result is not None and result.success, result.message
    assert len(broker.placed) == 1 and broker.placed[0]["transactiontype"] == "BUY"
    assert int(broker.placed[0]["quantity"]) > 10  # capital-sized, as before


@freeze_time(SESSION_TIME_UTC)
def test_live_sell_within_verified_holding_is_sent_unchanged(tmp_path):
    """Holding 500 at the broker, capital sizing 176: a partial exit of 176 goes through as-is."""
    broker = FakeBroker(holdings={"2885": 500})
    om, _ = build(tmp_path, TradingMode.LIVE, broker, paper_holding=500)

    result = sell(om)

    assert result is not None and result.success, result.message
    assert broker.placed[0]["quantity"] == "176"


@freeze_time(SESSION_TIME_UTC)
def test_live_sell_only_counts_broker_rows_of_the_same_product_type(tmp_path):
    """A DELIVERY holding must not be used to justify an INTRADAY SELL."""
    broker = FakeBroker(holdings={})
    broker.position = lambda: {  # noqa: E731 — override with a mixed book
        "status": True,
        "data": [
            {"tradingsymbol": "RELIANCE-EQ", "symboltoken": "2885", "producttype": "DELIVERY", "netqty": "300"},
            {"tradingsymbol": "RELIANCE-EQ", "symboltoken": "2885", "producttype": "INTRADAY", "netqty": "7"},
        ],
    }
    om, _ = build(tmp_path, TradingMode.LIVE, broker, paper_holding=10)

    result = sell(om)

    assert result is not None and result.success, result.message
    assert broker.placed[0]["quantity"] == "7"


# ── After a live restart the paper book is empty ─────────────


@freeze_time(SESSION_TIME_UTC)
def test_live_verified_exit_works_after_restart_with_empty_paper_book_and_entries_closed(tmp_path):
    """
    Restart after square-off in LIVE mode: the paper book is empty but the
    broker holds 10. The entry gate is closed. A SELL is a verified exit and
    must reach the broker for 10 — the empty paper book must not get it
    rejected as "would open a short" before the broker is even asked.
    """
    broker = FakeBroker(holdings={"2885": 10})
    om, paper = build(tmp_path, TradingMode.LIVE, broker, paper_holding=0)
    assert paper.positions == []  # nothing survived the restart
    om._risk_manager.close_new_entries("intraday square-off done")

    result = sell(om)

    assert result is not None and result.success, result.message
    assert not result.is_paper
    assert [(p["transactiontype"], p["quantity"]) for p in broker.placed] == [("SELL", "10")]
    assert result.placed_qty == 10 and result.filled_qty == 0  # placed, not confirmed filled


@freeze_time(SESSION_TIME_UTC)
def test_live_sell_unverifiable_after_restart_is_rejected_for_that_reason_not_the_paper_book(tmp_path):
    broker = FakeBroker(holdings={"2885": 10}, positions_fail=True)
    om, _ = build(tmp_path, TradingMode.LIVE, broker, paper_holding=0)
    om._risk_manager.close_new_entries("intraday square-off done")

    result = sell(om)

    assert broker.placed == []
    assert result is not None and not result.success
    assert "verif" in result.message.lower(), result.message


# ── DUAL mode must not hide the live leg's outcome ───────────


@freeze_time(SESSION_TIME_UTC)
def test_dual_live_leg_rejection_is_visible_and_not_reported_as_a_live_fill(tmp_path):
    """Paper long 10, broker flat: the paper exit fills, but the caller must see the live rejection."""
    broker = FakeBroker(holdings={})
    om, _ = build(tmp_path, TradingMode.DUAL, broker, paper_holding=10)

    result = sell(om)

    assert broker.placed == []
    assert result is not None
    assert result.is_paper and result.success and result.filled_qty == 10  # the paper leg, honestly
    assert result.order_id.startswith("PAPER-")                             # no live fill claimed
    assert result.live_leg is not None and not result.live_leg.success
    assert "short" in result.live_leg.message.lower()
    assert "live" in result.message.lower() and "reject" in result.message.lower(), result.message


@freeze_time(SESSION_TIME_UTC)
def test_dual_after_restart_live_exit_works_and_paper_miss_is_visible(tmp_path):
    """Paper book empty after restart, broker holds 10, entries closed: the live exit must go through."""
    broker = FakeBroker(holdings={"2885": 10})
    om, _ = build(tmp_path, TradingMode.DUAL, broker, paper_holding=0)
    om._risk_manager.close_new_entries("intraday square-off done")

    result = sell(om)

    assert [(p["transactiontype"], p["quantity"]) for p in broker.placed] == [("SELL", "10")]
    assert result is not None
    assert result.live_leg is not None and result.live_leg.success
    assert result.live_leg.placed_qty == 10 and result.live_leg.filled_qty == 0  # placed, not filled
    assert result.live_leg.broker_order_id == "BRK1"
    # The paper leg had nothing to sell; the result says so rather than inventing a paper fill.
    assert result.is_paper and not result.success
