from engine.filter_engine.catalog import build_default_catalog
from engine.filter_engine.models import EvaluationMode
from engine.filter_engine.presets import INTRADAY_BARS_PER_TRADING_DAY, build_filter_presets


def test_short_term_presets_are_named_and_runnable():
    presets = build_filter_presets()
    catalog = build_default_catalog()

    assert len(presets) >= 4
    assert {preset.preset_id for preset in presets} >= {
        "momentum_breakout",
        "trend_pullback",
        "volatility_breakout",
        "trend_continuation",
    }
    for preset in presets:
        catalog.validate_expression(preset.expression, EvaluationMode.BACKTEST)
        assert preset.expression.children
        assert preset.timeframe == "5m"
        assert any(condition.timeframe == "5m" for condition in preset.expression.children if condition.type == "condition")
        assert preset.max_hold_bars > 0
        assert preset.default_stop_loss_pct > 0
        assert preset.default_take_profit_pct > 0


def test_research_presets_include_their_complete_technical_rules():
    presets = {preset.preset_id: preset for preset in build_filter_presets()}
    catalog = build_default_catalog()

    expected = {
        "minervini_trend_leader": {
            ("data_completeness", "minimum_candles"): 260,
            ("price_above_sma", "period"): {50, 150, 200},
            ("breakout", "lookback"): 20,
        },
        "oneil_growth_breakout": {
            ("data_completeness", "minimum_candles"): 260,
            ("breakout", "lookback"): 20,
            ("adx_threshold", "minimum"): 20,
        },
        "weinstein_stage2_approx": {
            ("data_completeness", "minimum_candles"): 180,
            ("price_above_sma", "period"): 150,
            ("breakout", "lookback"): 52,
        },
        "connors_rsi2_pullback": {
            ("data_completeness", "minimum_candles"): 210,
            ("price_above_sma", "period"): {5, 200},
            ("rsi_range", "period"): 2,
        },
        "ema_crossback_pullback": {
            ("data_completeness", "minimum_candles"): 60,
            ("ema_relation", "fast_period"): {10, 20},
            ("ema_relation", "slow_period"): {20, 50},
        },
        "trend_strength_baseline": {
            ("data_completeness", "minimum_candles"): 60,
            ("price_above_sma", "period"): 50,
            ("adx_threshold", "minimum"): 20,
        },
    }

    assert set(expected) <= presets.keys()
    expected_risk = {
        "minervini_trend_leader": (7, 15, 60),
        "oneil_growth_breakout": (7, 15, 63),
        "weinstein_stage2_approx": (10, 25, 90),
        "connors_rsi2_pullback": (4, 6, 5),
        "ema_crossback_pullback": (5, 10, 15),
        "trend_strength_baseline": (7, 15, 30),
    }
    for preset_id, rules in expected.items():
        preset = presets[preset_id]
        conditions = [node for node in preset.expression.children if node.type == "condition"]
        by_filter = {}
        for condition in conditions:
            by_filter.setdefault(condition.filter_id, []).append(condition.params)
        for (filter_id, parameter), expected_value in rules.items():
            actual_values = {params[parameter] for params in by_filter[filter_id]}
            assert actual_values == (expected_value if isinstance(expected_value, set) else {expected_value})

        assert preset.expression.operator == "ALL"
        assert preset.expression.veto == []
        assert any(condition.timeframe == "1d" for condition in conditions)
        assert any(condition.timeframe == "5m" for condition in conditions)
        assert all(condition.required is True and condition.weight == 1 for condition in conditions)
        assert all(
            catalog.get(condition.filter_id).availability == "available"
            and EvaluationMode.BACKTEST in catalog.get(condition.filter_id).supported_modes
            for condition in conditions
        )
        assert by_filter["average_traded_value"] == [{"period": 30, "minimum": 10_000_000}]
        assert any(condition.filter_id == "average_traded_value" for condition in conditions)
        assert not any(condition.filter_id in {"event_risk", "circuit_status"} for condition in conditions)
        stop, target, max_hold = expected_risk[preset_id]
        assert (preset.default_stop_loss_pct, preset.default_take_profit_pct, preset.max_hold_bars) == (
            stop,
            target,
            max_hold * INTRADAY_BARS_PER_TRADING_DAY,
        )
        assert 0 < preset.default_stop_loss_pct < preset.default_take_profit_pct
        assert preset.max_hold_bars > 0

    assert "technical proxy" in presets["minervini_trend_leader"].description.lower()
    assert presets["ema_crossback_pullback"].name.endswith("(technical proxy)")
    assert "actual first retest" in presets["ema_crossback_pullback"].description.lower()
