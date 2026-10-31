from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from strategies.fundamental_setup_backtester import (
    FILTER_BACKED_SETUP_IDS,
    FIVE_MINUTE_BARS_PER_SESSION,
    FUNDAMENTAL_SETUP_CANDLE_INTERVAL,
    SETUPS,
    prepare_setup_candles,
    run_setup,
)

START = date(2025, 1, 1)
IST = ZoneInfo("Asia/Kolkata")
FIVE_MINUTE = timedelta(minutes=5)
_SESSION_DATES: list[date] = []
_candidate = START
while len(_SESSION_DATES) < 300:
    if _candidate.weekday() < 5:
        _SESSION_DATES.append(_candidate)
    _candidate += timedelta(days=1)


def timestamp(index: int) -> datetime:
    session, slot = divmod(index, FIVE_MINUTE_BARS_PER_SESSION)
    return datetime.combine(_SESSION_DATES[session], time(9, 15), tzinfo=IST) + slot * FIVE_MINUTE


def candle(index, *, open_=100, high=101, low=99, close=100, volume=1000):
    return {
        "timestamp": timestamp(index),
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


def flat_sessions(sessions=20, *, price=100, high=None, low=None, volume=1000):
    return [
        candle(index, open_=price, high=high or price + 1, low=low or price - 1, close=price, volume=volume)
        for index in range(sessions * FIVE_MINUTE_BARS_PER_SESSION)
    ]


def rising_sessions(sessions, *, slope=0.02, start=100):
    candles = []
    for index in range(sessions * FIVE_MINUTE_BARS_PER_SESSION):
        price = start + index * slope
        candles.append(candle(index, open_=price - 0.1, high=price + 0.5, low=price - 0.5, close=price))
    return candles


def run(candles, setup_id, **options):
    return run_setup(
        candles=candles,
        setup_id=setup_id,
        start_date=START,
        end_date=timestamp(len(candles) - 1).date(),
        initial_capital=100_000,
        **options,
    )


def sweep_candles(*, sweep_volume=1000, exit_bar=None):
    candles = flat_sessions()
    candles.extend([
        candle(1500, open_=99.5, high=100, low=97, close=98, volume=sweep_volume),
        candle(1501, open_=98, high=102, low=98, close=101.5),
        candle(1502, open_=102, high=103, low=101, close=102),
        exit_bar or candle(1503, open_=110, high=116, low=109, close=114),
    ])
    return candles


def test_sweep_reclaim_freezes_prior_volume_level_and_enters_next_five_minute_open():
    ordinary = run(sweep_candles(), "consolidation_sweep_reclaim")
    future_volume_changed = run(sweep_candles(sweep_volume=10_000_000), "consolidation_sweep_reclaim")

    assert ordinary["metrics"]["trades"] == 1
    trade = ordinary["trades"][0]
    assert trade["base_start_date"] == timestamp(0).isoformat()
    assert trade["base_end_date"] == timestamp(1499).isoformat()
    assert trade["sweep_date"] == timestamp(1500).isoformat()
    assert trade["signal_date"] == timestamp(1501).isoformat()
    assert trade["entry_date"] == timestamp(1502).isoformat()
    assert datetime.fromisoformat(trade["entry_date"]) - datetime.fromisoformat(trade["signal_date"]) == FIVE_MINUTE
    assert trade["entry_price"] > 102  # adverse buy slippage
    assert trade["exit_reason"] == "target_2r"
    assert trade["poc_method"] == "five_minute_ohlcv_estimate"
    assert future_volume_changed["trades"][0]["poc"] == trade["poc"]
    assert trade["total_charges"] > 0
    assert ordinary["metrics"]["final_capital"] > 100_000
    assert len(ordinary["equity_curve"]) == len(sweep_candles())


def test_gap_below_stop_fills_at_gap_open_and_updates_equity():
    candles = sweep_candles(exit_bar=candle(1503, open_=90, high=92, low=88, close=91))

    result = run(candles, "consolidation_sweep_reclaim")
    trade = result["trades"][0]

    assert trade["exit_reason"] == "gap_stop"
    assert trade["exit_price"] == pytest.approx(90 * 0.9995)
    assert trade["exit_price"] < trade["stop_price"]
    assert result["equity_curve"][-1]["value"] == result["metrics"]["final_capital"]
    assert result["metrics"]["max_drawdown_pct"] > 0


def test_same_five_minute_bar_stop_and_target_resolve_conservatively_at_stop():
    candles = sweep_candles()
    candles[1502] = candle(1502, open_=102, high=120, low=95, close=110)

    trade = run(candles, "consolidation_sweep_reclaim")["trades"][0]

    assert trade["entry_date"] == trade["exit_date"]
    assert trade["exit_reason"] == "stop_loss"
    assert trade["exit_price"] < trade["stop_price"]  # adverse sell slippage


def test_time_exit_uses_the_known_next_five_minute_open():
    candles = sweep_candles()[:1502]
    candles.extend(candle(index, open_=102, high=104, low=100, close=102) for index in range(1502, 1505))
    candles.append(candle(1505, open_=103, high=110, low=100, close=106))

    trade = run(candles, "consolidation_sweep_reclaim", max_hold_bars=2)["trades"][0]

    assert trade["exit_reason"] == "time_exit"
    assert trade["exit_date"] == timestamp(1504).isoformat()
    assert trade["exit_price"] == pytest.approx(102 * 0.9995)


def test_custom_target_stop_buffer_and_hold_period_change_trade_levels():
    candles = sweep_candles(exit_bar=candle(1503, open_=103, high=110, low=100, close=108))
    result = run(candles, "consolidation_sweep_reclaim", target_r=1.0, stop_buffer_pct=2.0, max_hold_bars=5)
    trade = result["trades"][0]

    assert trade["stop_price"] == pytest.approx(97 * 0.98)
    assert trade["target_price"] == pytest.approx(trade["entry_price"] + (trade["entry_price"] - trade["stop_price"]))
    assert trade["exit_reason"] == "target_1r"
    assert trade["target_r"] == 1.0
    assert trade["max_hold_bars"] == 5
    assert trade["stop_buffer_pct"] == 2.0
    assert result["settings"]["target_r"] == 1.0


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"target_r": 0.4}, "target_r"),
        ({"target_r": 11}, "target_r"),
        ({"target_r": True}, "target_r"),
        ({"max_hold_bars": 0}, "max_hold_bars"),
        ({"max_hold_bars": 7501}, "max_hold_bars"),
        ({"max_hold_bars": 2.5}, "max_hold_bars"),
        ({"stop_buffer_pct": -1}, "stop_buffer_pct"),
        ({"stop_buffer_pct": 11}, "stop_buffer_pct"),
    ],
)
def test_target_hold_and_stop_settings_have_strict_bounds(options, message):
    with pytest.raises(ValueError, match=message):
        run(sweep_candles(), "consolidation_sweep_reclaim", **options)


def test_consolidation_breakout_uses_same_slot_volume_and_next_five_minute_open():
    candles = flat_sessions()
    candles.extend([
        candle(1500, open_=100, high=104, low=100, close=103, volume=1300),
        candle(1501, open_=103, high=105, low=102, close=104),
        candle(1502, open_=109, high=112, low=108, close=111),
    ])
    result = run(candles, "consolidation_breakout")

    assert result["metrics"]["trades"] == 1
    trade = result["trades"][0]
    assert trade["signal_date"] == timestamp(1500).isoformat()
    assert trade["entry_date"] == timestamp(1501).isoformat()
    assert trade["sweep_date"] is None

    candles[1500]["volume"] = 1100
    assert run(candles, "consolidation_breakout")["metrics"]["trades"] == 0


def test_pullback_reclaim_uses_session_equivalent_moving_averages():
    candles = rising_sessions(60)
    candles.extend([
        candle(4500, open_=172.8, high=173.2, low=172, close=172.9),
        candle(4501, open_=179.5, high=181, low=179, close=180),
        candle(4502, open_=181, high=182, low=180, close=181.5),
        candle(4503, open_=220, high=225, low=219, close=224),
    ])

    result = run(candles, "pullback_reclaim")

    assert result["metrics"]["trades"] == 1
    trade = result["trades"][0]
    assert trade["signal_date"] == timestamp(4501).isoformat()
    assert trade["entry_date"] == timestamp(4502).isoformat()
    assert trade["poc"] is None
    assert trade["sweep_date"] is None


def test_vcp_style_breakout_uses_three_contracting_five_session_ranges():
    candles = []
    for index in range(20 * FIVE_MINUTE_BARS_PER_SESSION):
        session = index // FIVE_MINUTE_BARS_PER_SESSION
        half_range = 8 if session < 5 else 4 if session < 10 else 2 if session < 15 else 0.8
        candles.append(candle(index, open_=100, high=100 + half_range, low=100 - half_range, close=100))
    candles.extend([
        candle(1500, open_=104.7, high=106, low=104.5, close=105.5, volume=2000),
        candle(1501, open_=105.5, high=108, low=105, close=107),
        candle(1502, open_=110, high=115, low=109, close=114),
    ])

    result = run(candles, "volatility_contraction_breakout")

    assert result["metrics"]["trades"] == 1
    assert result["trades"][0]["signal_date"] == timestamp(1500).isoformat()
    assert result["trades"][0]["entry_date"] == timestamp(1501).isoformat()
    candles[1500]["volume"] = 1400
    assert run(candles, "volatility_contraction_breakout")["metrics"]["trades"] == 0


def test_gap_continuation_uses_opening_five_minute_bar_and_prior_session_close():
    candles = rising_sessions(56)
    prior_close = candles[-1]["close"]
    opening = prior_close * 1.03
    close = opening * 1.01
    candles.extend([
        candle(56 * FIVE_MINUTE_BARS_PER_SESSION, open_=opening, high=close + 0.2, low=opening - 0.2, close=close, volume=2200),
        candle(56 * FIVE_MINUTE_BARS_PER_SESSION + 1, open_=close, high=close + 1, low=close - 0.5, close=close + 0.8),
        candle(56 * FIVE_MINUTE_BARS_PER_SESSION + 2, open_=close + 10, high=close + 20, low=close + 9, close=close + 19),
    ])

    result = run(candles, "high_volume_gap_continuation")

    assert result["metrics"]["trades"] == 1
    assert result["trades"][0]["signal_date"] == timestamp(56 * FIVE_MINUTE_BARS_PER_SESSION).isoformat()
    assert result["trades"][0]["entry_date"] == timestamp(56 * FIVE_MINUTE_BARS_PER_SESSION + 1).isoformat()
    candles[56 * FIVE_MINUTE_BARS_PER_SESSION]["volume"] = 1700
    assert run(candles, "high_volume_gap_continuation")["metrics"]["trades"] == 0


def test_gap_continuation_does_not_treat_a_missing_opening_bar_as_the_open():
    candles = rising_sessions(56)
    index = len(candles) + 1  # Leave out the new session's 09:15 bar.
    previous_close = candles[-1]["close"]
    opening = previous_close * 1.03
    close = opening * 1.01
    candles.extend([
        candle(index, open_=opening, high=close + 0.2, low=opening - 0.2, close=close, volume=2200),
        candle(index + 1, open_=close, high=close + 1, low=close - 0.5, close=close + 0.8),
    ])

    assert run(candles, "high_volume_gap_continuation")["metrics"]["trades"] == 0


def test_52_week_breakout_uses_252_session_equivalent_five_minute_history():
    candles = rising_sessions(252)
    previous = candles[-1]
    breakout_index = 252 * FIVE_MINUTE_BARS_PER_SESSION
    open_ = previous["close"] + 0.25
    close = previous["high"] + 1
    candles.extend([
        candle(breakout_index, open_=open_, high=close + 0.2, low=open_ - 0.2, close=close, volume=2000),
        candle(breakout_index + 1, open_=close, high=close + 1, low=close - 0.5, close=close + 0.8),
        candle(breakout_index + 2, open_=close + 10, high=close + 20, low=close + 9, close=close + 19),
    ])

    result = run(candles, "52_week_high_breakout")

    assert result["metrics"]["trades"] == 1
    assert result["trades"][0]["signal_date"] == timestamp(breakout_index).isoformat()
    assert result["trades"][0]["entry_date"] == timestamp(breakout_index + 1).isoformat()
    candles[breakout_index]["close"] = previous["high"] - 0.1
    assert run(candles, "52_week_high_breakout")["metrics"]["trades"] == 0


def test_donchian_breakout_waits_for_retest_and_reclaim_on_five_minute_bars():
    candles = rising_sessions(56)
    breakout_index = 56 * FIVE_MINUTE_BARS_PER_SESSION
    prior_high = candles[breakout_index - 1]["high"]
    candles.extend([
        candle(breakout_index, open_=prior_high * 1.001, high=prior_high * 1.02, low=prior_high * 0.999,
               close=prior_high * 1.01, volume=1500),
        candle(breakout_index + 1, open_=prior_high, high=prior_high * 1.005, low=prior_high * 0.99,
               close=prior_high * 1.003),
        candle(breakout_index + 2, open_=prior_high * 1.003, high=prior_high * 1.02, low=prior_high * 1.002,
               close=prior_high * 1.01),
        candle(breakout_index + 3, open_=prior_high * 1.01, high=prior_high * 1.02, low=prior_high * 1.005,
               close=prior_high * 1.015),
        candle(breakout_index + 4, open_=prior_high * 1.05, high=prior_high * 1.1, low=prior_high * 1.04,
               close=prior_high * 1.09),
    ])

    result = run(candles, "donchian_breakout_retest")

    assert result["metrics"]["trades"] == 1
    assert result["trades"][0]["signal_date"] == timestamp(breakout_index + 2).isoformat()
    assert result["trades"][0]["entry_date"] == timestamp(breakout_index + 3).isoformat()


def test_rsi2_rebound_uses_five_minute_rsi_and_long_term_trend():
    candles = rising_sessions(220)
    index = len(candles)
    baseline = candles[-1]["close"]
    candles.extend([
        candle(index, open_=baseline - 2, high=baseline - 1.5, low=baseline - 2.5, close=baseline - 2),
        candle(index + 1, open_=baseline - 3.5, high=baseline - 3, low=baseline - 4.5, close=baseline - 4),
        candle(index + 2, open_=baseline - 4, high=baseline + 1, low=baseline - 4.2, close=baseline + 0.5),
        candle(index + 3, open_=baseline + 0.6, high=baseline + 1.5, low=baseline + 0.4, close=baseline + 1.2),
        candle(index + 4, open_=baseline + 30, high=baseline + 50, low=baseline + 29, close=baseline + 45),
    ])

    result = run(candles, "trend_filtered_rsi2_rebound")

    assert result["metrics"]["trades"] == 1
    assert result["trades"][0]["signal_date"] == timestamp(index + 2).isoformat()
    assert result["trades"][0]["entry_date"] == timestamp(index + 3).isoformat()


def test_inside_bar_trend_breakout_is_detected_on_five_minute_candles():
    candles = rising_sessions(56)
    index = len(candles)
    mother_mid = candles[-1]["close"] + 0.2
    mother_high = mother_mid + 2
    candles.extend([
        candle(index, open_=mother_mid, high=mother_high, low=mother_mid - 2, close=mother_mid + 1),
        candle(index + 1, open_=mother_mid, high=mother_mid + 1, low=mother_mid - 1, close=mother_mid + 0.5),
        candle(index + 2, open_=mother_high * 1.001, high=mother_high * 1.02, low=mother_high,
               close=mother_high * 1.01, volume=1500),
        candle(index + 3, open_=mother_high * 1.01, high=mother_high * 1.02, low=mother_high * 1.005,
               close=mother_high * 1.015),
        candle(index + 4, open_=mother_high * 1.05, high=mother_high * 1.1, low=mother_high * 1.04,
               close=mother_high * 1.09),
    ])

    result = run(candles, "inside_bar_trend_breakout")

    assert result["metrics"]["trades"] == 1
    assert result["trades"][0]["signal_date"] == timestamp(index + 2).isoformat()
    assert result["trades"][0]["entry_date"] == timestamp(index + 3).isoformat()


@pytest.mark.parametrize(
    "bad_timestamp",
    [datetime.combine(START, time.min, tzinfo=IST), datetime.combine(START, time(9, 17), tzinfo=IST)],
)
def test_setup_candles_reject_daily_or_misaligned_timestamps(bad_timestamp):
    bar = candle(0)
    bar["timestamp"] = bad_timestamp

    with pytest.raises(ValueError, match="five-minute"):
        prepare_setup_candles([bar])


def test_input_validation_and_catalog_keeps_all_nine_standalone_and_shared_presets():
    assert FUNDAMENTAL_SETUP_CANDLE_INTERVAL == "5m"
    assert set(SETUPS) == {
        "consolidation_sweep_reclaim",
        "consolidation_breakout",
        "pullback_reclaim",
        "volatility_contraction_breakout",
        "high_volume_gap_continuation",
        "52_week_high_breakout",
        "donchian_breakout_retest",
        "trend_filtered_rsi2_rebound",
        "inside_bar_trend_breakout",
        *FILTER_BACKED_SETUP_IDS,
    }
    assert len(set(SETUPS) - set(FILTER_BACKED_SETUP_IDS)) == 9
    with pytest.raises(ValueError, match="unknown setup"):
        run_setup(
            candles=[candle(0)], setup_id="unknown",
            start_date=START, end_date=START, initial_capital=100_000,
        )
