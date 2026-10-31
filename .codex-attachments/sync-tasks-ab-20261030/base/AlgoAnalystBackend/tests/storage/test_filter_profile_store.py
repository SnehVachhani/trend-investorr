"""Unit tests for versioned filter-profile persistence."""

from unittest.mock import Mock

import pytest

from storage.models import FilterProfileStore


def _store():
    db = Mock()
    db.execute_returning.side_effect = [
        {"id": 7, "name": "Trend profile", "is_active": True},
        {"id": 11, "profile_id": 7, "version_number": 1, "status": "draft"},
    ]
    return FilterProfileStore(db), db


def test_create_profile_creates_profile_and_initial_draft_version():
    store, db = _store()

    result = store.create_profile(
        name="Trend profile",
        description="SMA and volume",
        mode="screen",
        expression={"operator": "ALL", "children": []},
        requirements={"1d": {"minimum_candles": 200}},
    )

    assert result["id"] == 7
    assert result["latest_version"]["version_number"] == 1
    assert db.execute_returning.call_count == 2


def test_update_draft_creates_new_version_after_published_version():
    db = Mock()
    db.fetch_one.side_effect = [
        None,
        {"version_number": 1, "status": "published"},
    ]
    db.execute_returning.return_value = {
        "id": 12,
        "profile_id": 7,
        "version_number": 2,
        "status": "draft",
    }
    store = FilterProfileStore(db)

    result = store.update_draft(
        profile_id=7,
        expression={"operator": "ALL", "children": []},
        requirements={},
    )

    assert result["version_number"] == 2
    insert_sql = db.execute_returning.call_args.args[0]
    assert "filter_profile_versions" in insert_sql


def test_binding_rejects_unpublished_version():
    db = Mock()
    db.fetch_one.return_value = {"status": "draft"}
    store = FilterProfileStore(db)

    with pytest.raises(ValueError, match="published"):
        store.upsert_binding(
            profile_id=7,
            version_id=12,
            target_type="watchlist",
            target_id="manual_picks",
        )

    db.execute_returning.assert_not_called()


def test_binding_writes_published_version():
    db = Mock()
    db.fetch_one.return_value = {"status": "published"}
    db.execute_returning.return_value = {"id": 21, "enabled": True}
    store = FilterProfileStore(db)

    result = store.upsert_binding(
        profile_id=7,
        version_id=12,
        target_type="algorithm",
        target_id="scanner_confluence",
        direction="BUY",
    )

    assert result["id"] == 21
    db.execute_returning.assert_called_once()
