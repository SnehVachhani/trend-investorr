"""Profile-driven algorithm tests: technical filters block entries before risk/execution."""

from engine.filter_engine.context import build_evaluation_context
from engine.filter_engine.models import EvaluationMode, FilterExpression, Timeframe
from strategies.profile_rule_algo import ProfileRuleAlgorithm
from tests.factories import make_candle_series
from core.constants import SignalSource
from data.models.market_data import Observation


def _expression():
    return FilterExpression.model_validate(
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


def _observation():
    return Observation(
        symbol="RELIANCE",
        token="2885",
        source=SignalSource.BREAKOUT,
        direction="bullish",
        confidence=0.8,
        price=220,
    )


def test_profile_algorithm_emits_entry_with_profile_explanation():
    candles = make_candle_series(count=60, base_price=100, daily_change=2)

    def context_provider(obs):
        return build_evaluation_context(
            symbol=obs.symbol,
            as_of=obs.timestamp,
            mode=EvaluationMode.SIGNAL,
            candles={Timeframe.ONE_DAY: candles},
        )

    algorithm = ProfileRuleAlgorithm(
        profile_id=7,
        profile_version=3,
        expression=_expression(),
        context_provider=context_provider,
    )
    algorithm.on_observation(_observation())

    entries = algorithm.check_entries()

    assert len(entries) == 1
    assert entries[0].metadata["profile_id"] == 7
    assert entries[0].metadata["profile_version"] == 3
    assert entries[0].metadata["evaluation"]["status"] == "pass"


def test_failed_profile_produces_no_entry():
    candles = make_candle_series(count=60, base_price=200, daily_change=-2)

    def context_provider(obs):
        return build_evaluation_context(
            symbol=obs.symbol,
            as_of=obs.timestamp,
            mode=EvaluationMode.SIGNAL,
            candles={Timeframe.ONE_DAY: candles},
        )

    algorithm = ProfileRuleAlgorithm(
        profile_id=7,
        profile_version=3,
        expression=_expression(),
        context_provider=context_provider,
    )
    algorithm.on_observation(_observation())

    assert algorithm.check_entries() == []


def test_insufficient_profile_data_blocks_entry():
    candles = make_candle_series(count=10, base_price=100, daily_change=2)

    def context_provider(obs):
        return build_evaluation_context(
            symbol=obs.symbol,
            as_of=obs.timestamp,
            mode=EvaluationMode.SIGNAL,
            candles={Timeframe.ONE_DAY: candles},
        )

    algorithm = ProfileRuleAlgorithm(
        profile_id=7,
        profile_version=3,
        expression=_expression(),
        context_provider=context_provider,
    )
    algorithm.on_observation(_observation())

    assert algorithm.check_entries() == []
    assert algorithm.last_evaluations["RELIANCE"]["status"] == "insufficient_data"
