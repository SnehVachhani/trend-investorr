"""Integration tests: full signal-to-trade pipeline.

Uses freezegun to simulate market hours for risk manager gate.
freezegun works in UTC — IST is UTC+5:30, so 10:30 IST = 05:00 UTC.
"""

import pytest
from freezegun import freeze_time

from analyzers.static_rule_analyzer import StaticRuleAnalyzer
from trading.risk_manager import RiskManager, RiskConfig
from trading.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
from trading.paper_trader import PaperTrader
from trading.order_manager import OrderManager, TradingMode
from core.constants import AnalysisAction
from core.security.audit import AuditTrail
from tests.factories import (
    make_signal,
    make_analysis_result,
)


# 10:30 IST = 05:00 UTC
MARKET_HOURS_UTC = "2026-05-02 05:00:00"


@pytest.fixture
def pipeline(temp_dir):
    """Full trading pipeline with all components wired together."""
    audit = AuditTrail(log_dir=temp_dir)
    capital = 10_00_000.0  # 10L

    risk_mgr = RiskManager(
        config=RiskConfig(
            total_capital=capital,
            max_capital_per_trade_pct=10.0,
            max_daily_loss_pct=5.0,
            max_open_positions=5,
        ),
        audit=audit,
    )

    cb = CircuitBreaker(
        config=CircuitBreakerConfig(
            max_daily_loss_pct=5.0,
            max_consecutive_losses=3,
        ),
        audit=audit,
        total_capital=capital,
    )

    paper = PaperTrader(capital=capital, slippage_pct=0.0, audit=audit)

    order_mgr = OrderManager(
        risk_manager=risk_mgr,
        circuit_breaker=cb,
        paper_trader=paper,
        audit=audit,
        mode=TradingMode.PAPER,
    )

    return {
        "risk_mgr": risk_mgr,
        "cb": cb,
        "paper": paper,
        "order_mgr": order_mgr,
        "audit": audit,
        "analyzer": StaticRuleAnalyzer(),
    }


class TestSignalToTrade:
    @freeze_time(MARKET_HOURS_UTC)
    def test_end_to_end_buy(self, pipeline):
        """Signal → analysis → risk → paper trade → filled."""
        signal = make_signal(price=500.0, score=0.65)
        analysis = make_analysis_result(action=AnalysisAction.BUY, confidence=0.75)

        result = pipeline["order_mgr"].execute_signal(signal, analysis)

        assert result is not None
        assert result.success is True
        assert result.is_paper is True
        assert len(pipeline["paper"].positions) > 0

    def test_skip_analysis_no_trade(self, pipeline):
        """SKIP analysis → no trade placed."""
        signal = make_signal()
        analysis = make_analysis_result(action=AnalysisAction.SKIP, confidence=0.0)

        result = pipeline["order_mgr"].execute_signal(signal, analysis)
        assert result is None

    def test_circuit_breaker_blocks_trade(self, pipeline):
        """After CB trips → trade blocked."""
        pipeline["cb"].trip("Test halt")

        signal = make_signal(price=500.0)
        analysis = make_analysis_result(action=AnalysisAction.BUY)

        result = pipeline["order_mgr"].execute_signal(signal, analysis)
        assert result is None

    @freeze_time(MARKET_HOURS_UTC)
    def test_risk_rejection_max_positions(self, pipeline):
        """Fill up to max positions → next trade rejected."""
        for i in range(5):
            signal = make_signal(symbol=f"STOCK_{i}", token=str(i), price=1000.0)
            analysis = make_analysis_result(action=AnalysisAction.BUY)
            pipeline["order_mgr"].execute_signal(signal, analysis)

        # 6th should be rejected
        signal = make_signal(symbol="STOCK_6", token="6", price=1000.0)
        analysis = make_analysis_result(action=AnalysisAction.BUY)
        result = pipeline["order_mgr"].execute_signal(signal, analysis)

        assert result is not None
        assert result.success is False


class TestCircuitBreakerIntegration:
    def test_consecutive_losses_halt_trading(self, pipeline):
        """3 consecutive losses → CB trips → next trade blocked."""
        cb = pipeline["cb"]

        cb.record_trade_result(-10_000)
        cb.record_trade_result(-10_000)
        cb.record_trade_result(-10_000)

        assert cb.is_open

        signal = make_signal(price=500.0)
        analysis = make_analysis_result(action=AnalysisAction.BUY)
        result = pipeline["order_mgr"].execute_signal(signal, analysis)
        assert result is None


class TestDailyReset:
    @freeze_time(MARKET_HOURS_UTC)
    def test_reset_clears_all(self, pipeline):
        """Reset restores all components to fresh state."""
        pipeline["cb"].record_trade_result(-5000)
        pipeline["risk_mgr"].update_daily_pnl(realized=-5000, unrealized=0)

        pipeline["cb"].reset_daily()
        pipeline["risk_mgr"].reset_daily()

        signal = make_signal(price=500.0)
        analysis = make_analysis_result(action=AnalysisAction.BUY)
        result = pipeline["order_mgr"].execute_signal(signal, analysis)

        assert result is not None
        assert result.success is True
