from datetime import datetime, timedelta, timezone

from engine.enhanced_momentum_scanner import (
    build_market_context,
    enrich_candidates,
    summarize_benchmark,
)


def _candles(count: int = 320, daily_change: float = 1.0, base: float = 100.0):
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    candles = []
    for index in range(count):
        close = base + index * daily_change
        candles.append(
            {
                "timestamp": start + timedelta(days=index),
                "open": close - 0.25,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "volume": 100_000,
            }
        )
    return candles


def _row(symbol: str, return_60d: float, volatility: float, prior_high: float = 120.0):
    return {
        "stock_id": hash(symbol) % 10000,
        "symbol": symbol,
        "name": symbol,
        "latest_close": 125.0,
        "return_20d": return_60d / 3,
        "return_60d": return_60d,
        "return_120d": return_60d * 1.5,
        "return_252d": return_60d * 2.5,
        "sma_20": 120.0,
        "sma_50": 110.0,
        "sma_200": 95.0,
        "volume_ratio": 2.0,
        "volume_ratio_20d": 2.0,
        "volume_ratio_60d": 1.6,
        "volume_ratio_120d": 1.35,
        "volume_ratio_252d": 1.15,
        "avg_turnover_20d_cr": 10.0,
        "avg_delivery_pct_20d": 45.0,
        "atr_abs_20d": 2.0,
        "atr_pct_20d": volatility,
        "realized_volatility_pct": volatility,
        "atr_pct_60d": volatility + 1.0,
        "atr_pct_120d": volatility + 2.0,
        "atr_pct_252d": volatility + 3.0,
        "realized_volatility_60d": volatility + 1.0,
        "realized_volatility_120d": volatility + 2.0,
        "realized_volatility_252d": volatility + 3.0,
        "prior_high_20": prior_high,
        "prior_high_55": prior_high,
        "close_position": 0.9,
        "observations": 320,
    }


def test_benchmark_context_uses_bullish_agreement_between_nifty_and_sensex():
    context = build_market_context(_candles(), _candles(base=200.0))

    assert context["available"] is True
    assert context["regime"] == "bullish"
    assert context["regime_score"] == 100.0
    assert context["primary_benchmark"] == "NIFTY"


def test_enhanced_score_rewards_market_outperformance_and_lower_risk():
    market = build_market_context(_candles(daily_change=0.5), _candles(base=200.0, daily_change=0.5))
    candidates = enrich_candidates(
        [
            _row("LEADER", return_60d=45.0, volatility=12.0, prior_high=120.0),
            _row("LAGGARD", return_60d=15.0, volatility=25.0, prior_high=140.0),
        ],
        market,
    )
    by_symbol = {item["symbol"]: item for item in candidates}

    assert by_symbol["LEADER"]["market_relative_strength_score"] > by_symbol["LAGGARD"]["market_relative_strength_score"]
    assert by_symbol["LEADER"]["risk_adjusted_momentum_score"] > by_symbol["LAGGARD"]["risk_adjusted_momentum_score"]
    assert by_symbol["LEADER"]["enhanced_score"] > by_symbol["LAGGARD"]["enhanced_score"]


def test_sector_slot_is_explicitly_reserved_but_does_not_change_score():
    market = build_market_context(_candles(), [])
    result = enrich_candidates([_row("TEST", return_60d=30.0, volatility=15.0)], market)[0]

    assert result["sector_score"] is None
    assert result["sector_context_available"] is False
    assert result["score_weights"]["base_momentum"] == 0.35
    assert result["score_weights"]["market_relative_strength"] == 0.15
    assert result["score_weights"]["risk_adjusted_momentum"] == 0.25
    assert result["score_weights"]["breakout_quality"] == 0.15
    assert result["score_weights"]["market_regime"] == 0.10
    assert result["score_weights"]["sector_context"] == 0.0


def test_benchmark_without_candles_is_unavailable_instead_of_failing():
    summary = summarize_benchmark("NIFTY", [])

    assert summary.available is False
    assert summary.observations == 0


def test_risk_and_volume_participation_use_multiple_horizons():
    market = build_market_context(_candles(daily_change=0.5), _candles(base=200.0, daily_change=0.5))
    row = _row("MULTI_HORIZON", return_60d=30.0, volatility=12.0)
    result = enrich_candidates([row], market)[0]

    assert result["risk_adjusted_20d"] is not None
    assert result["risk_adjusted_60d"] is not None
    assert result["risk_adjusted_120d"] is not None
    assert result["risk_adjusted_252d"] is not None
    assert result["volume_participation_score"] is not None
    assert result["volume_ratio_252d"] == 1.15
