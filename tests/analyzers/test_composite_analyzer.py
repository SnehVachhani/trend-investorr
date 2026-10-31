"""Tests for analyzers.composite_analyzer — multi-strategy voting."""

import pytest
from unittest.mock import MagicMock

from analyzers.composite_analyzer import CompositeAnalyzer
from analyzers.base import SignalAnalyzer
from core.constants import AnalysisAction, CompositeStrategy
from data.models.market_data import AnalysisResult
from tests.factories import make_signal, make_candle_series, make_analysis_result


def _mock_analyzer(name: str, action: AnalysisAction, confidence: float, healthy: bool = True):
    """Create a mock analyzer returning a fixed result."""
    mock = MagicMock(spec=SignalAnalyzer)
    mock.name = name
    mock.health_check.return_value = healthy
    mock.analyze.return_value = AnalysisResult(
        action=action,
        confidence=confidence,
        reasoning=f"{name}: {action.value} @ {confidence}",
        analyzer_name=name,
        signal_symbol="TEST",
    )
    return mock


class TestWeightedStrategy:
    def test_weighted_both_buy(self):
        static = _mock_analyzer("static", AnalysisAction.BUY, 0.8)
        gemini = _mock_analyzer("gemini", AnalysisAction.BUY, 0.6)

        composite = CompositeAnalyzer(
            analyzers=[static, gemini],
            weights=[0.7, 0.3],
            strategy=CompositeStrategy.WEIGHTED,
        )

        signal = make_signal()
        candles = make_candle_series(count=5)
        result = composite.analyze(signal, candles)

        assert result.action == AnalysisAction.BUY
        assert result.confidence > 0
        assert "WEIGHTED" in result.reasoning


class TestUnanimousStrategy:
    def test_unanimous_agreement(self):
        a1 = _mock_analyzer("a1", AnalysisAction.BUY, 0.8)
        a2 = _mock_analyzer("a2", AnalysisAction.BUY, 0.7)

        composite = CompositeAnalyzer(
            analyzers=[a1, a2], weights=[0.5, 0.5],
            strategy=CompositeStrategy.UNANIMOUS,
        )

        result = composite.analyze(make_signal(), make_candle_series(count=5))
        assert result.action == AnalysisAction.BUY
        assert "UNANIMOUS" in result.reasoning

    def test_unanimous_disagreement_skips(self):
        a1 = _mock_analyzer("a1", AnalysisAction.BUY, 0.8)
        a2 = _mock_analyzer("a2", AnalysisAction.SELL, 0.7)

        composite = CompositeAnalyzer(
            analyzers=[a1, a2], weights=[0.5, 0.5],
            strategy=CompositeStrategy.UNANIMOUS,
        )

        result = composite.analyze(make_signal(), make_candle_series(count=5))
        assert result.action == AnalysisAction.SKIP
        assert "consensus" in result.reasoning.lower() or "No consensus" in result.reasoning


class TestMajorityStrategy:
    def test_majority_wins(self):
        a1 = _mock_analyzer("a1", AnalysisAction.BUY, 0.8)
        a2 = _mock_analyzer("a2", AnalysisAction.BUY, 0.6)
        a3 = _mock_analyzer("a3", AnalysisAction.SELL, 0.7)

        composite = CompositeAnalyzer(
            analyzers=[a1, a2, a3], weights=[0.33, 0.33, 0.34],
            strategy=CompositeStrategy.MAJORITY,
        )

        result = composite.analyze(make_signal(), make_candle_series(count=5))
        assert result.action == AnalysisAction.BUY
        assert "MAJORITY" in result.reasoning


class TestSkipHandling:
    def test_all_skip_returns_skip(self):
        a1 = _mock_analyzer("a1", AnalysisAction.SKIP, 0.0)
        a2 = _mock_analyzer("a2", AnalysisAction.SKIP, 0.0)

        composite = CompositeAnalyzer(
            analyzers=[a1, a2], weights=[0.5, 0.5],
            strategy=CompositeStrategy.WEIGHTED,
        )

        result = composite.analyze(make_signal(), make_candle_series(count=5))
        assert result.action == AnalysisAction.SKIP

    def test_unhealthy_analyzer_skipped(self):
        healthy = _mock_analyzer("healthy", AnalysisAction.BUY, 0.8)
        unhealthy = _mock_analyzer("unhealthy", AnalysisAction.SELL, 0.9, healthy=False)

        composite = CompositeAnalyzer(
            analyzers=[healthy, unhealthy], weights=[0.5, 0.5],
            strategy=CompositeStrategy.WEIGHTED,
        )

        result = composite.analyze(make_signal(), make_candle_series(count=5))
        # Only healthy analyzer should be used
        assert result.action == AnalysisAction.BUY
        unhealthy.analyze.assert_not_called()


class TestSingleAnalyzer:
    def test_single_analyzer_works(self):
        a1 = _mock_analyzer("solo", AnalysisAction.BUY, 0.75)
        composite = CompositeAnalyzer(analyzers=[a1], strategy=CompositeStrategy.WEIGHTED)

        result = composite.analyze(make_signal(), make_candle_series(count=5))
        assert result.action == AnalysisAction.BUY


class TestCompositeEdgeCases:
    def test_empty_analyzers_raises(self):
        with pytest.raises(ValueError, match="at least one"):
            CompositeAnalyzer(analyzers=[])

    def test_mismatched_weights_raises(self):
        a1 = _mock_analyzer("a1", AnalysisAction.BUY, 0.8)
        with pytest.raises(ValueError, match="Weights length"):
            CompositeAnalyzer(analyzers=[a1], weights=[0.5, 0.5])
