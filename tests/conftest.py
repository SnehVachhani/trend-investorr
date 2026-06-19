"""
Trend-Investorr: Shared pytest fixtures.

Provides pre-built instances of core objects for all test files.
"""

import sys
from pathlib import Path

import pytest

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.factories import (
    make_stock,
    make_candle,
    make_candle_series,
    make_tick,
    make_signal,
    make_analysis_result,
    make_order_request,
    make_position,
    make_volume_snapshot,
)
from core.constants import (
    AnalysisAction,
    IST,
    ProductType,
    SignalStrength,
    SignalType,
    TransactionType,
)


# ── Model Fixtures ───────────────────────────────────────────


@pytest.fixture
def sample_stock():
    return make_stock()


@pytest.fixture
def sample_candle():
    return make_candle()


@pytest.fixture
def sample_candle_series_60():
    """60 daily candles with uptrend."""
    return make_candle_series(count=60, base_price=2800.0, daily_change=5.0)


@pytest.fixture
def sample_candle_series_30():
    """30 daily candles — short series."""
    return make_candle_series(count=30, base_price=2800.0, daily_change=3.0)


@pytest.fixture
def flat_candle_series():
    """60 daily candles with flat prices (all same)."""
    return make_candle_series(count=60, base_price=1000.0, daily_change=0.0)


@pytest.fixture
def sample_tick():
    return make_tick()


@pytest.fixture
def sample_signal():
    return make_signal()


@pytest.fixture
def buy_signal():
    return make_signal(signal_type=SignalType.BUY, score=0.65)


@pytest.fixture
def sell_signal():
    return make_signal(signal_type=SignalType.SELL, score=0.55)


@pytest.fixture
def weak_signal():
    return make_signal(strength=SignalStrength.WEAK, score=0.30)


@pytest.fixture
def buy_analysis():
    return make_analysis_result(action=AnalysisAction.BUY, confidence=0.75)


@pytest.fixture
def sell_analysis():
    return make_analysis_result(action=AnalysisAction.SELL, confidence=0.60)


@pytest.fixture
def skip_analysis():
    return make_analysis_result(action=AnalysisAction.SKIP, confidence=0.0, reasoning="Skipped")


@pytest.fixture
def sample_order_request():
    return make_order_request()


@pytest.fixture
def sell_order_request():
    return make_order_request(transaction_type=TransactionType.SELL)


@pytest.fixture
def sample_position():
    return make_position()


@pytest.fixture
def multiple_positions():
    """5 positions in different stocks."""
    symbols = ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK"]
    return [
        make_position(symbol=s, token=str(i), avg_buy_price=1000 + (i * 100))
        for i, s in enumerate(symbols)
    ]


# ── Temp Dir Fixture ─────────────────────────────────────────


@pytest.fixture
def temp_dir(tmp_path):
    """Provide a temp directory for file-based tests."""
    return tmp_path
