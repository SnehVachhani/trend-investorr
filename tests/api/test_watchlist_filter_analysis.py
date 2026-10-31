"""Focused tests for multi-stock filter analysis ranking and market regimes."""

from datetime import datetime, timedelta, timezone

from api import server


def _completed(preset_id: str, preset_name: str, total_return: float, drawdown: float, sharpe: float):
    return {
        "preset_id": preset_id,
        "preset_name": preset_name,
        "status": "completed",
        "total_return_pct": total_return,
        "max_drawdown_pct": drawdown,
        "sharpe_ratio": sharpe,
        "total_trades": 3,
    }


def test_market_behavior_classifies_sideways_and_large_directional_moves():
    assert server._classify_watchlist_market_behavior(
        {
            "observations": 250,
            "total_return_pct": 4,
            "trend_efficiency_pct": 18,
            "annualized_volatility_pct": 22,
        }
    )["id"] == "sideways"

    assert server._classify_watchlist_market_behavior(
        {
            "observations": 250,
            "total_return_pct": 150,
            "trend_efficiency_pct": 56,
            "annualized_volatility_pct": 34,
        }
    )["id"] == "strong_uptrend"


def test_daily_market_profile_uses_only_selected_period_and_reports_metrics():
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    rows = []
    for index in range(40):
        close = 100 + index
        rows.append(
            {
                "timestamp": start + timedelta(days=index),
                "high": close + 1,
                "low": close - 1,
                "close": close,
            }
        )

    class FakeDatabase:
        def fetch_all(self, query, params):
            assert "candles_1d" in query
            assert params[0] == 7
            return rows

    profile = server._load_watchlist_market_profile(
        FakeDatabase(),
        {"id": 7},
        start,
        start + timedelta(days=45),
    )

    assert profile["observations"] == 40
    assert profile["total_return_pct"] == 39.0
    assert profile["max_drawdown_pct"] == 0.0
    assert profile["behavior"]["id"] == "uptrend"


def test_watchlist_buy_and_hold_summary_is_independent_of_filter_results():
    summary = server._watchlist_buy_and_hold_summary(
        [
            {"market_profile": {"total_return_pct": 20.0}, "combinations": []},
            {"market_profile": {"total_return_pct": -5.0}, "combinations": []},
            {"market_profile": {}, "combinations": [_completed("breakout", "Breakout", 99, 5, 1.0)]},
        ]
    )

    assert summary == {
        "average_watchlist_return_pct": 7.5,
        "watchlist_return_stock_count": 2,
    }


def test_leaderboard_and_regime_recommendation_rank_independent_stock_returns():
    stock_results = [
        {
            "symbol": "SIDEWAYS-EQ",
            "market_profile": {"behavior": {"id": "sideways", "label": "Sideways", "description": "Range"}},
            "combinations": [
                _completed("breakout", "Breakout", 4, 5, 0.3),
                _completed("mean_revert", "Mean Revert", 12, 3, 1.1),
            ],
        },
        {
            "symbol": "TREND-EQ",
            "market_profile": {"behavior": {"id": "uptrend", "label": "Uptrend", "description": "Trend"}},
            "combinations": [
                _completed("breakout", "Breakout", 24, 7, 1.4),
                _completed("mean_revert", "Mean Revert", 8, 4, 0.7),
            ],
        },
    ]

    leaderboard = server._filter_leaderboard(stock_results)
    recommendations = server._watchlist_regime_recommendations(stock_results)

    assert leaderboard[0]["preset_id"] == "breakout"
    assert leaderboard[0]["average_return_pct"] == 14.0
    by_regime = {item["regime_id"]: item for item in recommendations}
    assert by_regime["sideways"]["recommended_filter"]["preset_id"] == "mean_revert"
    assert by_regime["uptrend"]["recommended_filter"]["preset_id"] == "breakout"


def test_leaderboard_calculates_equal_weight_portfolio_value_and_keeps_missing_allocations_as_cash():
    stock_results = [
        {
            "symbol": "ONE-EQ",
            "market_profile": {},
            "combinations": [_completed("breakout", "Breakout", 20, 5, 1.0)],
        }
    ]

    leaderboard = server._filter_leaderboard(
        stock_results,
        portfolio_initial_capital=1_000_000,
        portfolio_stock_count=2,
    )

    assert leaderboard[0]["average_return_pct"] == 20.0
    assert leaderboard[0]["portfolio_return_pct"] == 10.0
    assert leaderboard[0]["portfolio_final_capital"] == 1_100_000.0
