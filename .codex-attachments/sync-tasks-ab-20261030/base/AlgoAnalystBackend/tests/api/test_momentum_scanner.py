"""Focused tests for the independent NSE momentum-ranking formula."""

from api import server


def _row(**overrides):
    row = {
        "latest_close": 120,
        "return_20d": 12,
        "return_60d": 32,
        "return_120d": 58,
        "return_252d": 110,
        "sma_20": 112,
        "sma_50": 104,
        "sma_200": 84,
        "high_52w": 125,
        "volume_ratio": 2.2,
        "avg_turnover_20d_cr": 8,
    }
    row.update(overrides)
    return row


def test_momentum_score_rewards_price_trend_volume_and_breakout_confirmation():
    result = server._momentum_scan_score(_row())

    assert result["momentum_score"] >= 75
    assert result["trend_aligned"] is True
    assert result["category"] == "Strong momentum"
    assert result["price_momentum_score"] > 60
    assert result["volume_score"] > 50


def test_momentum_score_does_not_treat_volume_alone_as_price_momentum():
    result = server._momentum_scan_score(
        _row(
            return_20d=-8,
            return_60d=-15,
            return_120d=-20,
            return_252d=-35,
            sma_20=130,
            sma_50=135,
            sma_200=140,
            high_52w=180,
            volume_ratio=4.0,
            avg_turnover_20d_cr=20,
        )
    )

    assert result["volume_score"] > result["price_momentum_score"]
    assert result["trend_aligned"] is False
    assert result["category"] == "Volume surge"
