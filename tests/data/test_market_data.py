"""Tests for data.models.market_data — all 11 dataclasses."""

import pytest
from dataclasses import FrozenInstanceError
from datetime import datetime, date

from core.constants import (
    AnalysisAction,
    CandleInterval,
    Exchange,
    IST,
    ProductType,
    SignalStrength,
    SignalType,
)
from data.models.market_data import (
    AnalysisResult,
    Candle,
    DailyPnL,
    OrderRequest,
    Position,
    Signal,
    Stock,
    Tick,
    VolumeSnapshot,
)
from tests.factories import (
    make_stock,
    make_candle,
    make_tick,
    make_signal,
    make_position,
    make_volume_snapshot,
    make_analysis_result,
    make_order_request,
)


class TestStock:
    def test_frozen_assignment_raises(self):
        stock = make_stock()
        with pytest.raises(FrozenInstanceError):
            stock.symbol = "TCS"  # type: ignore

    def test_trading_symbol_format(self):
        stock = make_stock(symbol="RELIANCE", series="EQ")
        assert stock.trading_symbol == "RELIANCE-EQ"

    def test_str_format(self):
        stock = make_stock(symbol="TCS", exchange=Exchange.NSE)
        assert str(stock) == "TCS (NSE)"


class TestCandle:
    def test_change_pct_normal(self):
        candle = make_candle(open=100.0, close=105.0)
        assert candle.change_pct == pytest.approx(5.0)

    def test_change_pct_zero_open_no_crash(self):
        candle = make_candle(open=0.0, close=100.0)
        assert candle.change_pct == 0.0

    def test_is_bullish(self):
        assert make_candle(open=100, close=110).is_bullish is True
        assert make_candle(open=110, close=100).is_bullish is False

    def test_is_bearish(self):
        assert make_candle(open=110, close=100).is_bearish is True
        assert make_candle(open=100, close=110).is_bearish is False

    def test_body_size_always_positive(self):
        assert make_candle(open=100, close=90).body_size == 10.0
        assert make_candle(open=90, close=100).body_size == 10.0

    def test_range_calculation(self):
        candle = make_candle(high=2850.0, low=2780.0)
        assert candle.range == pytest.approx(70.0)


class TestTick:
    def test_spread_normal(self):
        tick = make_tick(best_bid=100.0, best_ask=100.5)
        assert tick.spread == pytest.approx(0.5)

    def test_spread_zero_bid_no_crash(self):
        tick = make_tick(best_bid=0.0, best_ask=100.5)
        assert tick.spread == 0.0


class TestVolumeSnapshot:
    def test_volume_ratio_30d_normal(self):
        snap = make_volume_snapshot(cumulative_volume=1_000_000, avg_volume_30d=500_000)
        assert snap.volume_ratio_30d == pytest.approx(2.0)

    def test_volume_ratio_30d_zero_avg_no_crash(self):
        snap = make_volume_snapshot(cumulative_volume=1_000_000, avg_volume_30d=0)
        assert snap.volume_ratio_30d == 0.0

    def test_volume_ratio_90d_zero_avg_no_crash(self):
        snap = make_volume_snapshot(cumulative_volume=1_000_000, avg_volume_90d=0)
        assert snap.volume_ratio_90d == 0.0


class TestSignal:
    def test_volume_spike_x_format(self):
        sig = make_signal(volume_ratio=2.5)
        assert sig.volume_spike_x == "2.5x"


class TestAnalysisResult:
    def test_is_actionable_buy(self):
        result = make_analysis_result(action=AnalysisAction.BUY)
        assert result.is_actionable is True

    def test_is_actionable_sell(self):
        result = make_analysis_result(action=AnalysisAction.SELL)
        assert result.is_actionable is True

    def test_is_not_actionable_skip(self):
        result = make_analysis_result(action=AnalysisAction.SKIP)
        assert result.is_actionable is False

    def test_is_not_actionable_hold(self):
        result = make_analysis_result(action=AnalysisAction.HOLD)
        assert result.is_actionable is False

    def test_confidence_pct_format(self):
        result = make_analysis_result(confidence=0.75)
        assert result.confidence_pct == "75%"


class TestPosition:
    def test_unrealized_pnl_profit(self):
        pos = make_position(avg_buy_price=100.0, current_price=110.0, quantity=10)
        assert pos.unrealized_pnl == pytest.approx(100.0)

    def test_unrealized_pnl_loss(self):
        pos = make_position(avg_buy_price=100.0, current_price=90.0, quantity=10)
        assert pos.unrealized_pnl == pytest.approx(-100.0)

    def test_unrealized_pnl_pct_zero_buy_no_crash(self):
        pos = make_position(avg_buy_price=0.0, current_price=100.0, quantity=10)
        assert pos.unrealized_pnl_pct == 0.0

    def test_current_value(self):
        pos = make_position(current_price=500.0, quantity=20)
        assert pos.current_value == pytest.approx(10000.0)

    def test_invested_value(self):
        pos = make_position(avg_buy_price=450.0, quantity=20)
        assert pos.invested_value == pytest.approx(9000.0)


class TestDailyPnL:
    def test_total_pnl(self):
        pnl = DailyPnL(date=date.today(), realized_pnl=1000.0, unrealized_pnl=-200.0)
        assert pnl.total_pnl == pytest.approx(800.0)

    def test_win_rate_zero_trades_no_crash(self):
        pnl = DailyPnL(date=date.today(), total_trades=0, winning_trades=0)
        assert pnl.win_rate == 0.0

    def test_win_rate_calculation(self):
        pnl = DailyPnL(date=date.today(), total_trades=10, winning_trades=7)
        assert pnl.win_rate == pytest.approx(70.0)


class TestOrderRequest:
    def test_estimated_value_normal(self):
        order = make_order_request(price=500.0, quantity=10)
        assert order.estimated_value == pytest.approx(5000.0)

    def test_estimated_value_zero_price(self):
        order = make_order_request(price=0.0, quantity=10)
        assert order.estimated_value == 0.0
