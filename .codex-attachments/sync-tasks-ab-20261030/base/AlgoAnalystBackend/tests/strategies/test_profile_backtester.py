from datetime import datetime, timedelta, timezone

import pytest

from engine.filter_engine.models import FilterExpression, FilterStatus, Timeframe
from engine.filter_engine.backtest_cache import build_atomic_backtest_cache
from engine.filter_engine.catalog import build_default_catalog
from engine.filter_engine.evaluator import FilterEvaluator
import strategies.profile_backtester as profile_backtester
from strategies.profile_backtester import ProfileBacktestConfig, ProfileBacktester
from trading.charges import DeliveryChargeConfig


def _candles(count: int = 6):
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return [
        {
            "symbol": "TEST",
            "timestamp": start + timedelta(days=index),
            "open": 100 + index,
            "high": 102 + index,
            "low": 99 + index,
            "close": 101 + index,
            "volume": 1000,
        }
        for index in range(count)
    ]


def test_profile_backtester_enters_on_next_bar_and_explains_signal():
    expression = FilterExpression.model_validate(
        {
            "operator": "ALL",
            "children": [
                {
                    "type": "condition",
                    "filter_id": "price_range",
                    "timeframe": "1d",
                    "params": {"min_price": 0},
                }
            ],
        }
    )

    result = ProfileBacktester(
        symbol="TEST",
        candles=_candles(),
        expression=expression,
        config=ProfileBacktestConfig(initial_capital=1000, position_size_pct=100),
    ).run()

    assert result["total_trades"] == 1
    assert result["trades"][0]["entry_date"].startswith("2024-01-02")
    assert result["trades"][0]["entry_explanation"]["children"][0]["filter_id"] == "price_range"
    assert result["total_charges"] > 0
    assert result["trades"][0]["total_charges"] == pytest.approx(
        result["trades"][0]["entry_charges"]["total"]
        + result["trades"][0]["exit_charges"]["total"]
    )
    assert result["trades"][0]["pnl"] == pytest.approx(
        result["trades"][0]["gross_pnl"] - result["trades"][0]["total_charges"]
    )
    assert result["final_capital"] > result["initial_capital"]


def test_profile_backtester_reinvests_net_wallet_after_charges():
    expression = FilterExpression.model_validate(
        {
            "operator": "ALL",
            "children": [
                {
                    "type": "condition",
                    "filter_id": "price_range",
                    "timeframe": "1d",
                    "params": {"min_price": 0},
                }
            ],
        }
    )

    result = ProfileBacktester(
        symbol="TEST",
        candles=_candles(10),
        expression=expression,
        config=ProfileBacktestConfig(
            initial_capital=10_000,
            position_size_pct=100,
            stop_loss_pct=50,
            take_profit_pct=0,
            max_hold_bars=1,
        ),
    ).run()

    assert result["total_trades"] >= 2
    second_trade = result["trades"][1]
    static_wallet_quantity = DeliveryChargeConfig().max_buy_quantity(
        result["initial_capital"], second_trade["entry_price"]
    )
    assert second_trade["quantity"] > static_wallet_quantity
    assert result["total_charges"] == pytest.approx(
        sum(trade["total_charges"] for trade in result["trades"])
    )


def test_filter_failure_exit_is_opt_in_and_uses_next_bar_open():
    candles = _candles(6)
    candles[0]["close"] = 100
    candles[1]["open"] = 100
    candles[1]["close"] = 100
    candles[2]["open"] = 90
    candles[2]["close"] = 80
    candles[3]["open"] = 91
    expression = FilterExpression.model_validate(
        {
            "operator": "ALL",
            "children": [
                {
                    "type": "condition",
                    "filter_id": "price_range",
                    "timeframe": "1d",
                    "params": {"min_price": 90},
                }
            ],
        }
    )

    default_result = ProfileBacktester(
        symbol="TEST",
        candles=candles,
        expression=expression,
        config=ProfileBacktestConfig(stop_loss_pct=50, take_profit_pct=0, max_hold_bars=10),
    ).run()
    opted_in_result = ProfileBacktester(
        symbol="TEST",
        candles=candles,
        expression=expression,
        config=ProfileBacktestConfig(
            stop_loss_pct=50,
            take_profit_pct=0,
            max_hold_bars=10,
            close_on_filter_fail=True,
        ),
    ).run()

    assert default_result["trades"][0]["exit_reason"] == "end_of_test"
    assert opted_in_result["trades"][0]["exit_reason"] == "filter_failed"
    assert opted_in_result["trades"][0]["entry_price"] == 100
    assert opted_in_result["trades"][0]["exit_price"] == 91
    assert opted_in_result["trades"][0]["exit_date"].startswith("2024-01-04")


def test_profile_backtester_does_not_enter_before_requested_start():
    expression = FilterExpression.model_validate(
        {
            "operator": "ALL",
            "children": [
                {
                    "type": "condition",
                    "filter_id": "price_range",
                    "timeframe": "1d",
                    "params": {"min_price": 0},
                }
            ],
        }
    )
    candles = _candles()
    result = ProfileBacktester(
        symbol="TEST",
        candles=candles,
        expression=expression,
        start_date=candles[3]["timestamp"],
        end_date=candles[-1]["timestamp"],
    ).run()

    assert result["total_trades"] == 1
    assert result["trades"][0]["entry_date"].startswith("2024-01-05")


def test_profile_backtester_reports_requested_execution_window():
    expression = FilterExpression.model_validate(
        {
            "operator": "ALL",
            "children": [
                {
                    "type": "condition",
                    "filter_id": "price_range",
                    "timeframe": "1d",
                    "params": {"min_price": 0},
                }
            ],
        }
    )
    candles = _candles(8)
    result = ProfileBacktester(
        symbol="TEST",
        candles=candles,
        expression=expression,
        start_date=candles[2]["timestamp"],
        end_date=candles[5]["timestamp"],
    ).run()

    assert result["start_date"] == candles[2]["timestamp"].isoformat()
    assert result["end_date"] == candles[5]["timestamp"].isoformat()
    assert all(
        candles[2]["timestamp"].isoformat() <= point["timestamp"] <= candles[5]["timestamp"].isoformat()
        for point in result["equity_curve"]
    )


def test_profile_backtester_requires_a_stop_and_a_time_or_target_exit():
    expression = FilterExpression.model_validate(
        {
            "operator": "ALL",
            "children": [
                {
                    "type": "condition",
                    "filter_id": "price_range",
                    "timeframe": "1d",
                    "params": {"min_price": 0},
                }
            ],
        }
    )

    with pytest.raises(ValueError, match="stop loss"):
        ProfileBacktester(
            symbol="TEST",
            candles=_candles(),
            expression=expression,
            config=ProfileBacktestConfig(
                stop_loss_pct=0,
                take_profit_pct=0,
                max_hold_bars=0,
            ),
        ).run()


def test_profile_backtester_exits_at_the_configured_max_hold_bar():
    expression = FilterExpression.model_validate(
        {
            "operator": "ALL",
            "children": [
                {
                    "type": "condition",
                    "filter_id": "price_range",
                    "timeframe": "1d",
                    "params": {"min_price": 0},
                }
            ],
        }
    )

    result = ProfileBacktester(
        symbol="TEST",
        candles=_candles(8),
        expression=expression,
        config=ProfileBacktestConfig(
            stop_loss_pct=5,
            take_profit_pct=0,
            max_hold_bars=2,
        ),
    ).run()

    assert result["trades"][0]["exit_reason"] == "max_hold_bars"
    assert result["trades"][0]["hold_bars"] == 2


def test_profile_backtester_checks_target_on_the_entry_bar():
    candles = _candles(5)
    candles[1]["high"] = 110
    expression = FilterExpression.model_validate(
        {
            "operator": "ALL",
            "children": [
                {
                    "type": "condition",
                    "filter_id": "price_range",
                    "timeframe": "1d",
                    "params": {"min_price": 0},
                }
            ],
        }
    )

    result = ProfileBacktester(
        symbol="TEST",
        candles=candles,
        expression=expression,
        config=ProfileBacktestConfig(
            stop_loss_pct=5,
            take_profit_pct=5,
            max_hold_bars=10,
        ),
    ).run()

    assert result["trades"][0]["exit_reason"] == "take_profit"
    assert result["trades"][0]["entry_date"] == result["trades"][0]["exit_date"]


def test_profile_backtester_reports_trading_bars_not_calendar_days():
    candles = _candles(7)
    trading_offsets = [0, 1, 4, 5, 8, 9, 12]
    for candle, offset in zip(candles, trading_offsets):
        candle["timestamp"] = datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(days=offset)
    expression = FilterExpression.model_validate(
        {
            "operator": "ALL",
            "children": [
                {
                    "type": "condition",
                    "filter_id": "price_range",
                    "timeframe": "1d",
                    "params": {"min_price": 0},
                }
            ],
        }
    )

    result = ProfileBacktester(
        symbol="TEST",
        candles=candles,
        expression=expression,
        config=ProfileBacktestConfig(
            stop_loss_pct=5,
            take_profit_pct=0,
            max_hold_bars=2,
        ),
    ).run()

    assert result["trades"][0]["exit_reason"] == "max_hold_bars"
    assert result["trades"][0]["hold_bars"] == 2


def test_profile_backtester_limits_context_to_filter_lookback(monkeypatch):
    expression = FilterExpression.model_validate(
        {
            "operator": "ALL",
            "children": [
                {
                    "type": "condition",
                    "filter_id": "price_above_sma",
                    "timeframe": "1d",
                    "params": {"period": 20, "relation": "above"},
                }
            ],
        }
    )
    observed_sizes: list[int] = []
    original_evaluate = FilterEvaluator.evaluate

    def record_context_size(self, current_expression, context):
        observed_sizes.append(len(context.candles[Timeframe.ONE_DAY]))
        return original_evaluate(self, current_expression, context)

    monkeypatch.setattr(FilterEvaluator, "evaluate", record_context_size)
    ProfileBacktester(
        symbol="TEST",
        candles=_candles(120),
        expression=expression,
        config=ProfileBacktestConfig(max_hold_bars=10),
    ).run()

    assert observed_sizes
    assert max(observed_sizes) <= 25


def test_profile_backtester_honors_data_completeness_minimum_window():
    expression = FilterExpression.model_validate(
        {
            "operator": "ALL",
            "children": [
                {
                    "type": "condition",
                    "filter_id": "data_completeness",
                    "timeframe": "1d",
                    "params": {"minimum_candles": 260},
                }
            ],
        }
    )

    result = ProfileBacktester(
        symbol="TEST",
        candles=_candles(300),
        expression=expression,
        config=ProfileBacktestConfig(max_hold_bars=10),
    ).run()

    assert result["signal_counts"]["insufficient_data"] == 259
    assert result["signal_counts"]["pass"] == 41


def test_profile_backtester_uses_five_minute_execution_clock_with_daily_context():
    start = datetime(2024, 1, 2, 9, 15, tzinfo=timezone.utc)
    five_minute = [
        {
            "symbol": "TEST",
            "timestamp": start + timedelta(minutes=index * 5),
            "open": 100 + index,
            "high": 102 + index,
            "low": 99 + index,
            "close": 101 + index,
            "volume": 1000,
        }
        for index in range(8)
    ]
    daily = [
        {
            "symbol": "TEST",
            "timestamp": start - timedelta(days=2 - index),
            "open": 95 + index,
            "high": 98 + index,
            "low": 94 + index,
            "close": 96 + index,
            "volume": 10_000,
        }
        for index in range(3)
    ]
    expression = FilterExpression.model_validate(
        {
            "operator": "ALL",
            "children": [
                {"type": "condition", "filter_id": "price_range", "timeframe": "1d", "params": {"min_price": 0}},
                {"type": "condition", "filter_id": "price_range", "timeframe": "5m", "params": {"min_price": 0}},
            ],
        }
    )

    result = ProfileBacktester(
        symbol="TEST",
        candles_by_timeframe={Timeframe.ONE_DAY: daily, Timeframe.FIVE_MINUTE: five_minute},
        expression=expression,
        config=ProfileBacktestConfig(initial_capital=1000),
        execution_timeframe=Timeframe.FIVE_MINUTE,
    ).run()

    assert result["execution_timeframe"] == "5m"
    assert result["start_date"] == five_minute[0]["timestamp"].isoformat()


def test_cached_all_expression_matches_reference_backtest_exactly():
    """The accelerated path must remain observationally identical.

    This covers both slower daily alignment and intraday cache-backed rules.
    """
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    daily = [
        {
            "symbol": "TEST",
            "timestamp": start + timedelta(days=index),
            "open": 100 + index,
            "high": 102 + index,
            "low": 99 + index,
            "close": 101 + index,
            "volume": 10_000 + index * 10,
        }
        for index in range(40)
    ]
    intraday_start = start + timedelta(days=30, hours=9, minutes=15)
    five_minute = [
        {
            "symbol": "TEST",
            "timestamp": intraday_start + timedelta(minutes=index * 5),
            "open": 130 + index * 0.2,
            "high": 131 + index * 0.2,
            "low": 129 + index * 0.2,
            "close": 130.5 + index * 0.2,
            "volume": 1_000 + (index % 7) * 100,
        }
        for index in range(100)
    ]
    expression = FilterExpression.model_validate(
        {
            "operator": "ALL",
            "children": [
                {
                    "type": "condition",
                    "filter_id": "price_above_sma",
                    "timeframe": "1d",
                    "params": {"period": 20, "relation": "above"},
                },
                {
                    "type": "condition",
                    "filter_id": "relative_volume",
                    "timeframe": "5m",
                    "params": {"period": 20, "multiple": 0.1},
                },
            ],
        }
    )
    candles = {Timeframe.ONE_DAY: daily, Timeframe.FIVE_MINUTE: five_minute}
    catalog = build_default_catalog()
    config = ProfileBacktestConfig(stop_loss_pct=5, take_profit_pct=10, max_hold_bars=10)

    reference = ProfileBacktester(
        symbol="TEST",
        candles_by_timeframe=candles,
        expression=expression,
        config=config,
        catalog=catalog,
        execution_timeframe=Timeframe.FIVE_MINUTE,
    ).run()
    cache = build_atomic_backtest_cache(
        [expression],
        {
            Timeframe.ONE_DAY: ProfileBacktester._normalize(daily),
            Timeframe.FIVE_MINUTE: ProfileBacktester._normalize(five_minute),
        },
        catalog,
    )
    accelerated = ProfileBacktester(
        symbol="TEST",
        candles_by_timeframe=candles,
        expression=expression,
        config=config,
        catalog=catalog,
        evaluator=FilterEvaluator(catalog, atomic_cache=cache),
        execution_timeframe=Timeframe.FIVE_MINUTE,
    ).run()

    assert accelerated == reference


def test_fast_path_divergence_restarts_on_the_reference_evaluator(monkeypatch):
    """A suspicious cache result must never leave a watchlist run partial."""
    expression = FilterExpression.model_validate(
        {
            "operator": "ALL",
            "children": [
                {
                    "type": "condition",
                    "filter_id": "price_range",
                    "timeframe": "1d",
                    "params": {"min_price": 10_000},
                }
            ],
        }
    )

    class AlwaysPass:
        def at(self, _index):
            return True, FilterStatus.PASS

    monkeypatch.setattr(
        profile_backtester._AtomicAllExpressionOutcomes,
        "build",
        classmethod(lambda *_: AlwaysPass()),
    )
    backtester = ProfileBacktester(
        symbol="TEST",
        candles=_candles(),
        expression=expression,
        config=ProfileBacktestConfig(max_hold_bars=2),
    )

    accelerated = backtester.run()
    reference = backtester.run(_allow_fast_path=False)

    assert accelerated == reference
    assert accelerated["total_trades"] == 0
