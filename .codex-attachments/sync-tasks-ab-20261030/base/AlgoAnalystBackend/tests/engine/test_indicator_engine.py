"""Regression tests for indicator edge cases used by reusable filters."""

import numpy as np

from engine.indicator_engine import IndicatorEngine


def test_rsi_marks_continuous_gain_as_overbought():
    closes = np.arange(1.0, 31.0)

    rsi = IndicatorEngine.compute_rsi(closes, period=14)

    assert rsi[-1] == 100.0


def test_rsi_marks_flat_series_as_neutral():
    closes = np.full(30, 100.0)

    rsi = IndicatorEngine.compute_rsi(closes, period=14)

    assert rsi[-1] == 50.0
