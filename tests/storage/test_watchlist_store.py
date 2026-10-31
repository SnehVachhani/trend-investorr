"""Ordering contracts for manually managed watchlists."""

from contextlib import contextmanager

from storage.models import WatchlistStore


class _Cursor:
    def __init__(self, *, inserted: bool = True):
        self.calls: list[tuple[str, tuple]] = []
        self.rowcount = 1 if inserted else 0
        self._fetches = iter(({"id": 4, "max_stocks": 0},))

    def execute(self, query: str, params: tuple = ()):
        self.calls.append((query, params))

    def fetchone(self):
        return next(self._fetches)


class _Database:
    def __init__(self, *, inserted: bool = True):
        self.cursor = _Cursor(inserted=inserted)

    def fetch_one(self, query: str, params: tuple = ()):  # WatchlistStore.get_watchlist
        assert "FROM watchlists" in query
        return {"id": 4, "name": "best", "max_stocks": 0}

    @contextmanager
    def transaction(self):
        yield self.cursor


def test_new_manual_watchlist_stock_receives_highest_priority():
    db = _Database()
    store = WatchlistStore(db)

    assert store.add_stock("best", 99) is True

    insert_query, insert_params = db.cursor.calls[-1]
    assert "COALESCE(MAX(priority), 0) + 1" in insert_query
    assert "ON CONFLICT (watchlist_id, stock_id) DO NOTHING" in insert_query
    assert insert_params == (4, 99, "manual", "", 4)


def test_duplicate_watchlist_stock_is_not_reported_as_a_new_top_entry():
    db = _Database(inserted=False)

    assert WatchlistStore(db).add_stock("best", 99) is False
