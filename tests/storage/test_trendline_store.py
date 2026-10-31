"""Persistence contracts for manually drawn trendlines."""

from unittest.mock import Mock

from storage.models import TrendlineStore


def test_trendline_store_creates_and_lists_drawings():
    db = Mock()
    db.execute_returning.return_value = {"id": 9, "symbol": "TCS", "interval": "1d", "points_json": [{"time": 1, "price": 100}]}
    store = TrendlineStore(db)

    created = store.create("TCS", "1d", [{"time": 1, "price": 100}, {"time": 2, "price": 105}])
    listed = store.list("TCS", "1d")

    assert created["id"] == 9
    assert listed == [] or listed is db.fetch_all.return_value
    db.execute_returning.assert_called_once()
    db.fetch_all.assert_called_once()


def test_trendline_store_rejects_less_than_two_points():
    store = TrendlineStore(Mock())
    try:
        store.create("TCS", "1d", [{"time": 1, "price": 100}])
    except ValueError as exc:
        assert "two" in str(exc).lower()
    else:
        raise AssertionError("expected a two-point validation error")
