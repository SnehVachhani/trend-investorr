"""Published profile loading tests."""

import pytest

from strategies.profile_loader import ProfileLoader


def _version(status="published"):
    return {
        "id": 12,
        "version_number": 2,
        "status": status,
        "expression_json": {
            "operator": "ALL",
            "children": [
                {
                    "type": "condition",
                    "filter_id": "price_above_sma",
                    "timeframe": "1d",
                    "params": {"period": 20},
                }
            ],
        },
        "requirements_json": {"1d": {"minimum_candles": 20}},
        "risk_config_json": {"stop_loss_pct": 1},
        "timeframe_config_json": {"entry": "1d"},
    }


def test_loader_returns_immutable_published_snapshot():
    store = type("Store", (), {"get_version": lambda self, profile_id, version: _version()})()

    loaded = ProfileLoader(store).load(7, 2)

    assert loaded.profile_id == 7
    assert loaded.version_id == 12
    assert loaded.version_number == 2
    assert loaded.expression.children[0].filter_id == "price_above_sma"


def test_loader_rejects_draft_for_automatic_execution():
    store = type("Store", (), {"get_version": lambda self, profile_id, version: _version("draft")})()

    with pytest.raises(ValueError, match="published"):
        ProfileLoader(store).load(7, 2)
