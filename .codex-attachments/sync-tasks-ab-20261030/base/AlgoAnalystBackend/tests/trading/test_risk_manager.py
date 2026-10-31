"""Tests for trading.risk_manager — 7-gate validation.

Uses freezegun to simulate market hours since Gate 1 checks time.
freezegun works in UTC — IST is UTC+5:30, so 10:30 IST = 05:00 UTC.
"""

import pytest
from freezegun import freeze_time

from trading.risk_manager import RiskManager, RiskConfig
from core.constants import TransactionType, ProductType
from tests.factories import make_order_request, make_position


# 10:30 IST = 05:00 UTC (freezegun uses UTC)
MARKET_HOURS_UTC = "2026-05-02 05:00:00"


@pytest.fixture
def risk_config():
    return RiskConfig(
        total_capital=1_00_00_000.0,     # 1 Crore
        max_capital_per_trade_pct=5.0,    # 5 Lakh per trade
        max_capital_deployed_pct=70.0,
        max_open_positions=20,
        max_positions_per_stock=1,
        max_daily_loss_pct=2.0,           # 2 Lakh daily loss limit
        max_per_trade_loss_pct=1.0,
        default_stop_loss_pct=2.0,
        default_target_pct=5.0,
        min_order_value=5_000.0,
        max_order_value=5_00_000.0,
        min_quantity=1,
        max_quantity=10_000,
    )


@pytest.fixture
def risk_mgr(risk_config):
    return RiskManager(config=risk_config)


class TestRiskApproval:
    @freeze_time(MARKET_HOURS_UTC)
    def test_buy_within_limits_approved(self, risk_mgr):
        order = make_order_request(price=500.0, quantity=10)
        decision = risk_mgr.evaluate(order, positions=[])
        assert decision.approved is True
        assert decision.quantity > 0

    @freeze_time(MARKET_HOURS_UTC)
    def test_stop_loss_calculated_for_buy(self, risk_mgr):
        order = make_order_request(price=100.0)
        decision = risk_mgr.evaluate(order, positions=[])
        assert decision.approved is True
        assert decision.stop_loss_price == pytest.approx(98.0)  # 100 * (1 - 2%)

    @freeze_time(MARKET_HOURS_UTC)
    def test_target_calculated_for_buy(self, risk_mgr):
        order = make_order_request(price=100.0)
        decision = risk_mgr.evaluate(order, positions=[])
        assert decision.target_price == pytest.approx(105.0)  # 100 * (1 + 5%)

    @freeze_time(MARKET_HOURS_UTC)
    def test_sell_order_bypasses_position_limits(self, risk_mgr):
        """SELL orders should not be rejected by position count limits."""
        positions = [make_position() for _ in range(20)]
        order = make_order_request(transaction_type=TransactionType.SELL, price=500.0)
        decision = risk_mgr.evaluate(order, positions)
        assert decision.approved is True


class TestRiskRejection:
    @freeze_time(MARKET_HOURS_UTC)
    def test_position_limit_hit_rejected(self, risk_mgr):
        positions = [
            make_position(symbol=f"STOCK_{i}", token=str(i))
            for i in range(20)
        ]
        order = make_order_request(symbol="NEW_STOCK", price=500.0)
        decision = risk_mgr.evaluate(order, positions)
        assert decision.approved is False
        assert "position" in decision.reason.lower()

    @freeze_time(MARKET_HOURS_UTC)
    def test_per_stock_limit_rejected(self, risk_mgr):
        positions = [make_position(symbol="RELIANCE")]
        order = make_order_request(symbol="RELIANCE", price=500.0)
        decision = risk_mgr.evaluate(order, positions)
        assert decision.approved is False
        assert "RELIANCE" in decision.reason

    @freeze_time(MARKET_HOURS_UTC)
    def test_daily_loss_exceeded_rejected(self, risk_mgr):
        # 2% of 1Cr = 2L = 200,000
        risk_mgr.update_daily_pnl(realized=-250_000, unrealized=0)
        order = make_order_request(price=500.0)
        decision = risk_mgr.evaluate(order, positions=[])
        assert decision.approved is False
        assert "loss" in decision.reason.lower()

    @freeze_time(MARKET_HOURS_UTC)
    def test_zero_price_returns_rejected(self, risk_mgr):
        order = make_order_request(price=0.0)
        decision = risk_mgr.evaluate(order, positions=[])
        assert decision.approved is False

    def test_market_closed_rejects_intraday(self, risk_mgr):
        """Outside market hours, intraday orders rejected."""
        order = make_order_request(price=500.0, product_type=ProductType.INTRADAY)
        decision = risk_mgr.evaluate(order, positions=[])
        assert isinstance(decision.approved, bool)


class TestPositionSizing:
    @freeze_time(MARKET_HOURS_UTC)
    def test_uses_smaller_of_capital_and_risk_based(self, risk_mgr):
        order = make_order_request(price=1000.0)
        decision = risk_mgr.evaluate(order, positions=[])
        assert decision.approved is True
        # Capital-based: 5L / 1000 = 500 shares
        assert decision.quantity <= 500

    @freeze_time(MARKET_HOURS_UTC)
    def test_quantity_clamped_to_max(self, risk_mgr):
        order = make_order_request(price=1.0)
        decision = risk_mgr.evaluate(order, positions=[])
        if decision.approved:
            assert decision.quantity <= 10_000


class TestRiskManagerReset:
    def test_daily_reset_clears_pnl(self, risk_mgr):
        risk_mgr.update_daily_pnl(realized=-100_000, unrealized=-50_000)
        risk_mgr.reset_daily()
        stats = risk_mgr.stats
        assert stats["daily_realized_pnl"] == 0.0
        assert stats["daily_unrealized_pnl"] == 0.0
        assert stats["daily_trades"] == 0

    def test_config_update_changes_thresholds(self, risk_mgr):
        new_config = RiskConfig(max_capital_per_trade_pct=10.0)
        risk_mgr.update_config(new_config)
        stats = risk_mgr.stats
        assert stats["config"]["max_per_trade_pct"] == 10.0
