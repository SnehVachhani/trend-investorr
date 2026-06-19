"""Tests for trading.paper_trader — virtual trading engine."""

import pytest

from trading.paper_trader import PaperTrader
from core.constants import ProductType, TransactionType, OrderStatus
from tests.factories import make_order_request


@pytest.fixture
def paper():
    """Paper trader with 1L capital for easy math."""
    return PaperTrader(capital=1_00_000.0, slippage_pct=0.1)


class TestPaperBuy:
    def test_buy_deducts_cash(self, paper):
        order = make_order_request(price=100.0, quantity=10)
        result = paper.place_order(order)

        assert result.success is True
        assert result.status == OrderStatus.FILLED
        assert result.is_paper is True
        # Cash reduced (with slippage)
        assert paper.cash < 1_00_000.0

    def test_buy_with_slippage(self, paper):
        """Fill price should be price * (1 + 0.1%)."""
        order = make_order_request(price=1000.0, quantity=10)
        result = paper.place_order(order)

        expected_fill = 1000.0 * 1.001  # 0.1% slippage
        assert result.avg_fill_price == pytest.approx(expected_fill, rel=0.001)

    def test_buy_creates_position(self, paper):
        order = make_order_request(symbol="TCS", price=3500.0, quantity=5)
        paper.place_order(order)

        pos = paper.get_position("TCS")
        assert pos is not None
        assert pos.quantity == 5
        assert pos.symbol == "TCS"

    def test_insufficient_cash_rejected(self, paper):
        """1L capital, trying to buy 1000 shares at 1000 each = 10L."""
        order = make_order_request(price=1000.0, quantity=1000)
        result = paper.place_order(order)

        assert result.success is False
        assert result.status == OrderStatus.REJECTED
        assert "capital" in result.message.lower() or "insufficient" in result.message.lower()


class TestPaperSell:
    def test_sell_releases_cash(self, paper):
        # Buy first
        paper.place_order(make_order_request(price=100.0, quantity=10))
        cash_after_buy = paper.cash

        # Sell
        sell = make_order_request(
            transaction_type=TransactionType.SELL, price=110.0, quantity=10
        )
        result = paper.place_order(sell)

        assert result.success is True
        assert paper.cash > cash_after_buy

    def test_sell_records_profit(self, paper):
        paper.place_order(make_order_request(price=100.0, quantity=10))
        sell = make_order_request(
            transaction_type=TransactionType.SELL, price=120.0, quantity=10
        )
        result = paper.place_order(sell)

        assert result.success is True
        assert "P&L" in result.message
        summary = paper.get_daily_summary()
        assert summary.realized_pnl > 0
        assert summary.winning_trades == 1

    def test_sell_records_loss(self, paper):
        paper.place_order(make_order_request(price=100.0, quantity=10))
        sell = make_order_request(
            transaction_type=TransactionType.SELL, price=80.0, quantity=10
        )
        paper.place_order(sell)

        summary = paper.get_daily_summary()
        assert summary.realized_pnl < 0
        assert summary.losing_trades == 1

    def test_sell_no_position_rejected(self, paper):
        sell = make_order_request(
            symbol="NONEXISTENT", transaction_type=TransactionType.SELL,
            price=100.0, quantity=10,
        )
        result = paper.place_order(sell)
        assert result.success is False


class TestPositionAveraging:
    def test_two_buys_average_price(self, paper):
        """Buy 10@100, then 10@120 → avg should be ~110."""
        paper.place_order(make_order_request(price=100.0, quantity=10))
        paper.place_order(make_order_request(price=120.0, quantity=10))

        pos = paper.get_position("RELIANCE")
        assert pos is not None
        assert pos.quantity == 20
        # Avg with slippage: (100.1*10 + 120.12*10) / 20 ≈ 110.11
        assert 105 < pos.avg_buy_price < 115  # Rough range

    def test_partial_sell_keeps_remaining(self, paper):
        paper.place_order(make_order_request(price=100.0, quantity=20))

        sell = make_order_request(
            transaction_type=TransactionType.SELL, price=110.0, quantity=10
        )
        paper.place_order(sell)

        pos = paper.get_position("RELIANCE")
        assert pos is not None
        assert pos.quantity == 10  # 20 - 10


class TestPortfolioValue:
    def test_portfolio_value_equals_cash_plus_positions(self, paper):
        paper.place_order(make_order_request(price=1000.0, quantity=10))
        paper.update_prices({"RELIANCE": 1100.0})

        expected = paper.cash + (1100.0 * 10)
        assert paper.portfolio_value == pytest.approx(expected, rel=0.01)

    def test_total_pnl_from_initial(self, paper):
        # No trades → PnL should be 0
        assert paper.total_pnl == pytest.approx(0.0, abs=1.0)

    def test_unrealized_pnl_updates(self, paper):
        paper.place_order(make_order_request(price=1000.0, quantity=10))
        paper.update_prices({"RELIANCE": 1100.0})
        assert paper.unrealized_pnl > 0


class TestSquareOff:
    def test_intraday_squared_off_cnc_kept(self, paper):
        # Buy intraday
        intra = make_order_request(
            symbol="RELIANCE", price=500.0, quantity=10,
            product_type=ProductType.INTRADAY,
        )
        paper.place_order(intra)

        # Buy delivery (CNC)
        delivery = make_order_request(
            symbol="TCS", token="11536", price=3500.0, quantity=2,
            product_type=ProductType.DELIVERY,
        )
        paper.place_order(delivery)

        paper.update_prices({"RELIANCE": 510.0, "TCS": 3600.0})

        results = paper.square_off_intraday()

        # RELIANCE (intraday) should be squared off
        assert paper.get_position("RELIANCE") is None
        # TCS (delivery) should be kept
        assert paper.get_position("TCS") is not None


class TestOrderHistory:
    def test_orders_recorded(self, paper):
        paper.place_order(make_order_request(price=100.0, quantity=5))
        paper.place_order(make_order_request(price=200.0, quantity=3))

        assert len(paper.order_history) == 2
