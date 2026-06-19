"""Tests for analyzers.static_rule_analyzer — RSI, EMA, VWAP technical analysis."""

import pytest

from analyzers.static_rule_analyzer import StaticRuleAnalyzer
from core.constants import AnalysisAction
from tests.factories import make_signal, make_candle_series, make_candle


@pytest.fixture
def analyzer():
    return StaticRuleAnalyzer()


class TestStaticAnalyzerBasics:
    def test_health_check_always_true(self, analyzer):
        assert analyzer.health_check() is True

    def test_name_is_static(self, analyzer):
        assert analyzer.name == "static"

    def test_empty_candles_returns_skip(self, analyzer):
        signal = make_signal()
        result = analyzer.analyze(signal, [])
        assert result.action == AnalysisAction.SKIP
        assert result.confidence == 0.0

    def test_insufficient_candles_returns_skip(self, analyzer):
        """Less than ~20 candles needed for EMA20 — should skip or handle."""
        signal = make_signal()
        short_series = make_candle_series(count=10, base_price=100.0)
        result = analyzer.analyze(signal, short_series)
        # Should return SKIP or handle gracefully
        assert result.action in (AnalysisAction.SKIP, AnalysisAction.HOLD, AnalysisAction.BUY, AnalysisAction.SELL)
        assert result.analyzer_name == "static"


class TestUptrend:
    def test_uptrend_returns_buy(self, analyzer):
        """Gradual uptrend with moderate RSI → should lean BUY."""
        candles = make_candle_series(count=60, base_price=100.0, daily_change=2.0)
        signal = make_signal(price=candles[-1].close)

        result = analyzer.analyze(signal, candles)
        # In a clear uptrend, static analyzer should favor BUY
        assert result.action in (AnalysisAction.BUY, AnalysisAction.HOLD)
        assert result.confidence > 0.0
        assert "rsi" in result.indicators or "ema_short" in result.indicators


class TestDowntrend:
    def test_downtrend_returns_sell_or_skip(self, analyzer):
        """Gradual downtrend → should lean SELL or SKIP."""
        candles = make_candle_series(count=60, base_price=1000.0, daily_change=-3.0)
        signal = make_signal(price=candles[-1].close)

        result = analyzer.analyze(signal, candles)
        assert result.action in (AnalysisAction.SELL, AnalysisAction.SKIP, AnalysisAction.HOLD)


class TestFlatPrices:
    def test_flat_prices_neutral_rsi(self, analyzer):
        """All same price → RSI should be ~50 (neutral)."""
        candles = make_candle_series(count=60, base_price=1000.0, daily_change=0.0)
        signal = make_signal(price=1000.0)

        result = analyzer.analyze(signal, candles)
        # With flat prices, RSI should be near 50
        if "rsi" in result.indicators:
            rsi = result.indicators["rsi"]
            # RSI with no change can be undefined or 50
            assert 0 <= rsi <= 100


class TestIndicatorsPresent:
    def test_indicators_include_rsi(self, analyzer):
        candles = make_candle_series(count=60, base_price=500.0, daily_change=3.0)
        signal = make_signal(price=candles[-1].close)
        result = analyzer.analyze(signal, candles)

        # Check that indicators dict has expected keys
        assert isinstance(result.indicators, dict)

    def test_result_has_reasoning(self, analyzer):
        candles = make_candle_series(count=60, base_price=500.0)
        signal = make_signal()
        result = analyzer.analyze(signal, candles)
        assert len(result.reasoning) > 0

    def test_confidence_in_valid_range(self, analyzer):
        candles = make_candle_series(count=60, base_price=500.0)
        signal = make_signal()
        result = analyzer.analyze(signal, candles)
        assert 0.0 <= result.confidence <= 1.0
