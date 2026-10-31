"""API contract tests for filter catalog and profile endpoints."""

from unittest.mock import Mock

import pytest

import api.server as server


PROFILE_EXPRESSION = {
    "schema_version": 1,
    "operator": "ALL",
    "children": [
        {
            "type": "condition",
            "filter_id": "price_above_sma",
            "timeframe": "1d",
            "params": {"period": 20, "relation": "above"},
        }
    ],
    "veto": [],
}


def test_filter_catalog_endpoint_returns_parameter_metadata():
    response = server.list_filter_catalog()

    assert response["filters"]
    assert any(item["filter_id"] == "price_above_sma" for item in response["filters"])
    assert any(item["parameter_schema"] for item in response["filters"] if item["availability"] == "available")
    assert response["available_count"] > 0
    assert response["planned_count"] > 0
    assert response["available_count"] + response["planned_count"] == len(response["filters"])


def test_filter_backtest_presets_endpoint_returns_short_term_combinations():
    response = server.list_filter_backtest_presets()

    assert len(response["presets"]) >= 4
    assert any(item["preset_id"] == "momentum_breakout" for item in response["presets"])
    assert all(item["expression"]["children"] for item in response["presets"])


def test_filter_requirements_endpoint_returns_max_history():
    response = server.get_filter_requirements(
        server.FilterRequirementsRequest(
            mode="screen",
            expression=PROFILE_EXPRESSION,
        )
    )

    assert response["valid"] is True
    assert response["requirements"]["1d"]["minimum_candles"] >= 20


def test_create_profile_validates_expression_before_persistence(monkeypatch):
    fake_store = Mock()
    fake_store.create_profile.return_value = {"id": 4, "name": "Trend"}
    monkeypatch.setattr(server, "store", lambda name: fake_store)

    response = server.create_filter_profile(
        server.FilterProfileCreateRequest(
            name="Trend",
            description="Trend continuation",
            mode="signal",
            expression=PROFILE_EXPRESSION,
        )
    )

    assert response["profile"]["id"] == 4
    fake_store.create_profile.assert_called_once()
    assert fake_store.create_profile.call_args.kwargs["requirements"]["1d"]["minimum_candles"] >= 20


def test_validate_profile_returns_actionable_unknown_filter_error():
    request = server.FilterRequirementsRequest(
        mode="screen",
        expression={
            "operator": "ALL",
            "children":[{"type": "condition", "filter_id": "not_real", "timeframe": "1d"}],
        },
    )

    response = server.validate_filter_profile(request)

    assert response["valid"] is False
    assert "unknown filter" in response["errors"][0]


def test_stock_alias_search_resolves_apollo_micro_systems():
    assert server._stock_alias_symbols("Apollo Micro Systems") == ["APOLLO-EQ"]


def test_stock_serialization_exposes_distinct_company_name():
    serialized = server._serialize_stock_row({"symbol": "APOLLO-EQ", "name": "APOLLO"})

    assert serialized["symbol"] == "APOLLO-EQ"
    assert serialized["name"] == "APOLLO"
    assert serialized["company_name"] == "Apollo Micro Systems Limited"


def test_old_ticker_name_is_resolved_through_the_company_directory():
    assert server._stock_company_name("AHL", "AHLADA") == "Ahlada Engineers Limited"


def test_saved_scanner_name_lookup_uses_the_stock_id_for_old_symbols(monkeypatch):
    class FakeDatabase:
        def fetch_all(self, query, params):
            assert "WHERE id = ANY(%s)" in query
            assert params == ([42],)
            return [{"id": 42, "symbol": "AHLADA-EQ", "name": "AHLADA"}]

    monkeypatch.setattr(server, "get_db", lambda: FakeDatabase())

    response = server.get_stock_company_names(
        server.StockCompanyNamesRequest(symbols=["AHL"], stock_ids=[42])
    )

    assert response["company_names_by_stock_id"]["42"] == "Ahlada Engineers Limited"


def test_company_directory_covers_apollo_hospitals():
    assert len(server.STOCK_DISPLAY_METADATA) > 2_000
    assert server._stock_alias_symbols("Apollo Hospitals") == ["APOLLOHOSP-EQ"]

    serialized = server._serialize_stock_row({"symbol": "APOLLOHOSP-EQ", "name": "APOLLOHOSP"})

    assert serialized["company_name"] == "Apollo Hospitals Enterprise Limited"
