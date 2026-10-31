import pytest

from trading.charges import DeliveryChargeConfig


def test_default_angel_one_delivery_schedule_calculates_buy_and_sell_costs():
    config = DeliveryChargeConfig()

    buy = config.calculate_order("BUY", 100, 500)
    sell = config.calculate_order("SELL", 110, 500)

    assert buy.brokerage == pytest.approx(0.0)
    assert buy.stt == pytest.approx(50.0)
    assert buy.stamp_duty == pytest.approx(8.0)
    assert buy.dp_charges == 0
    assert sell.stt == pytest.approx(55.0)
    assert sell.stamp_duty == 0
    assert sell.dp_charges == pytest.approx(12.5)
    assert sell.gst_on_dp == pytest.approx(2.25)
    assert buy.total > buy.stt + buy.stamp_duty
    assert sell.total > sell.stt + sell.dp_charges


def test_buy_quantity_includes_buy_side_charges_in_wallet_limit():
    config = DeliveryChargeConfig()

    quantity = config.max_buy_quantity(1_000, 100)

    assert quantity == 9
    assert quantity * 100 + config.calculate_order("BUY", 100, quantity).total <= 1_000
    next_quantity = quantity + 1
    assert next_quantity * 100 + config.calculate_order("BUY", 100, next_quantity).total > 1_000


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_total_only_path_matches_full_breakdown(side):
    config = DeliveryChargeConfig()

    for price, quantity in ((0.01, 1), (100, 500), (1234.567, 37), (98765.43, 1200)):
        assert config.total_for_order(side, price, quantity) == pytest.approx(
            config.calculate_order(side, price, quantity).total
        )
