"""Edge case tests: boundary values and zero-division guards."""

import pytest

from core.constants import AnalysisAction
from freezegun import freeze_time
from data.models.market_data import (
    Candle,
    CandleInterval,
    DailyPnL,
    OrderRequest,
    Position,
    Tick,
    VolumeSnapshot,
)
from analyzers.static_rule_analyzer import StaticRuleAnalyzer
from trading.risk_manager import RiskManager, RiskConfig
from tests.factories import (
    make_candle,
    make_candle_series,
    make_order_request,
    make_signal,
    now_ist,
)


class TestZeroDivisionGuards:
    """Every computation with a denominator must handle zero."""

    def test_candle_change_pct_zero_open(self):
        candle = make_candle(open=0.0, close=100.0)
        assert candle.change_pct == 0.0

    def test_volume_snapshot_zero_30d(self):
        snap = VolumeSnapshot(
            symbol="TEST", timestamp=now_ist(),
            cumulative_volume=1000, avg_volume_30d=0,
        )
        assert snap.volume_ratio_30d == 0.0

    def test_volume_snapshot_zero_90d(self):
        snap = VolumeSnapshot(
            symbol="TEST", timestamp=now_ist(),
            cumulative_volume=1000, avg_volume_90d=0,
        )
        assert snap.volume_ratio_90d == 0.0

    def test_position_zero_buy_price(self):
        pos = Position(
            symbol="TEST", token="0", quantity=10,
            avg_buy_price=0.0, current_price=100.0,
        )
        assert pos.unrealized_pnl_pct == 0.0

    def test_daily_pnl_zero_trades(self):
        from datetime import date
        pnl = DailyPnL(date=date.today(), total_trades=0, winning_trades=0)
        assert pnl.win_rate == 0.0

    def test_tick_zero_bid(self):
        tick = Tick(symbol="TEST", token="0", ltp=100.0,
                    timestamp=now_ist(), best_bid=0.0, best_ask=100.5)
        assert tick.spread == 0.0

    def test_order_request_zero_price(self):
        order = make_order_request(price=0.0, quantity=10)
        assert order.estimated_value == 0.0


class TestBoundaryValues:
    """Test exact boundary conditions."""

    @freeze_time("2026-05-02 05:00:00")  # 10:30 IST = 05:00 UTC
    def test_risk_manager_zero_price_rejected(self):
        risk_mgr = RiskManager(config=RiskConfig())
        order = make_order_request(price=0.0)
        decision = risk_mgr.evaluate(order, positions=[])
        assert decision.approved is False

    def test_analyzer_empty_candle_list(self):
        analyzer = StaticRuleAnalyzer()
        signal = make_signal()
        result = analyzer.analyze(signal, [])
        assert result.action == AnalysisAction.SKIP

    def test_position_pnl_at_breakeven(self):
        pos = Position(
            symbol="TEST", token="0", quantity=10,
            avg_buy_price=100.0, current_price=100.0,
        )
        assert pos.unrealized_pnl == pytest.approx(0.0)
        assert pos.unrealized_pnl_pct == pytest.approx(0.0)
