"""
Offline regression tests for the live 5-minute candle pipeline.

SmartAPI's WebSocket field ``volume_trade_for_the_day`` is the *cumulative*
volume traded so far today — it is NOT the size of the individual trade.
Each 5-minute candle must therefore carry only the volume traded *within*
that interval (the delta of the cumulative counter), and VWAP must weight
each tick by its incremental volume.

Pipeline exercised end-to-end (no broker, no database):

    SmartAPI message dict
      -> WebSocketManager._parse_tick       data/streams/websocket_manager.py
      -> CandleAggregator.on_tick           data/streams/candle_aggregator.py
      -> CandleAggregator.flush_all
      -> CandleStore.insert_batch(FIVE_MINUTE, rows)   storage/models.py

This mirrors the startup wiring in main.py:
    candle_aggregator = CandleAggregator(db=db, interval_min=5)
    ws_manager.on_tick(on_tick_handler)   # -> candle_aggregator.on_tick(tick)
    market_close -> candle_aggregator.flush_all()
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from core.constants import IST, CandleInterval
from data.streams import websocket_manager
from data.streams.candle_aggregator import CandleAggregator
from data.streams.websocket_manager import WebSocketManager


# ── Test doubles ─────────────────────────────────────────────


class FakeCandleStore:
    """In-memory stand-in for storage.models.CandleStore."""

    batches: list[tuple[CandleInterval, list[tuple]]] = []
    dbs: list[object] = []

    def __init__(self, db):
        type(self).dbs.append(db)

    def insert_batch(self, interval, rows):
        type(self).batches.append((interval, list(rows)))
        return len(rows)

    @classmethod
    def reset(cls):
        cls.batches = []
        cls.dbs = []

    @classmethod
    def rows(cls) -> list[tuple]:
        out: list[tuple] = []
        for _, rows in cls.batches:
            out.extend(rows)
        return out


class ControlledClock:
    """Replaces ``datetime`` inside websocket_manager so _parse_tick stamps
    each tick with a time we choose instead of the wall clock."""

    current: datetime = datetime(2025, 1, 6, 9, 15, 0, tzinfo=IST)

    @classmethod
    def now(cls, tz=None):
        return cls.current if tz is None else cls.current.astimezone(tz)


# ── Fixtures ─────────────────────────────────────────────────


TOKEN = "2885"
STOCK_ID = 42


@pytest.fixture
def fake_store(monkeypatch):
    FakeCandleStore.reset()
    monkeypatch.setattr("storage.models.CandleStore", FakeCandleStore)
    return FakeCandleStore


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(websocket_manager, "datetime", ControlledClock)
    return ControlledClock


@pytest.fixture
def ws_manager():
    # __init__ only stores the session/audit objects; nothing connects.
    return WebSocketManager(session=None, audit=None)


@pytest.fixture
def fake_db():
    return object()


@pytest.fixture
def aggregator(fake_db):
    return CandleAggregator(
        db=fake_db,
        interval_min=5,
        token_to_stock_id={TOKEN: STOCK_ID},
    )


# ── Cumulative volume example ───────────────────────────────
#
# Wall-clock   LTP      volume_trade_for_the_day (cumulative)   traded in tick
# 09:15:10     100.00   1_000                                   1_000
# 09:16:30     102.00   1_500                                     500
# 09:18:45     101.00   2_200                                     700
# ---- 5-minute boundary: 09:15 candle completes ----
# 09:20:05     103.00   2_500                                     300
# 09:22:00     104.00   2_900                                     400
#
# Expected candles_5m rows:
#   09:15  O=100 H=102 L=100 C=101  volume=2_200  vwap=100.77
#   09:20  O=103 H=104 L=103 C=104  volume=  700  vwap=103.57
#
# The two candle volumes must add up to the final cumulative counter (2_900).

SMARTAPI_FEED = [
    # (wall-clock,                                 ltp paisa, cumulative volume)
    (datetime(2025, 1, 6, 9, 15, 10, tzinfo=IST), 10_000, 1_000),
    (datetime(2025, 1, 6, 9, 16, 30, tzinfo=IST), 10_200, 1_500),
    (datetime(2025, 1, 6, 9, 18, 45, tzinfo=IST), 10_100, 2_200),
    (datetime(2025, 1, 6, 9, 20, 5, tzinfo=IST), 10_300, 2_500),
    (datetime(2025, 1, 6, 9, 22, 0, tzinfo=IST), 10_400, 2_900),
]

EXPECTED_CANDLES = {
    datetime(2025, 1, 6, 9, 15, tzinfo=IST): {
        "open": 100.0, "high": 102.0, "low": 100.0, "close": 101.0,
        "volume": 2_200,
        # (100*1000 + 102*500 + 101*700) / 2200
        "vwap": round(221_700 / 2_200, 2),
    },
    datetime(2025, 1, 6, 9, 20, tzinfo=IST): {
        "open": 103.0, "high": 104.0, "low": 103.0, "close": 104.0,
        "volume": 700,
        # (103*300 + 104*400) / 700
        "vwap": round(72_500 / 700, 2),
    },
}


def smartapi_message(
    ltp_paisa: int, cumulative_volume: int, token: str = TOKEN, name: str = "RELIANCE"
) -> dict:
    """Minimal SmartAPI QUOTE-mode payload as delivered to _on_data."""
    return {
        "token": token,
        "name": name,
        "last_traded_price": ltp_paisa,
        "volume_trade_for_the_day": cumulative_volume,
        "open_price_of_the_day": 10_000,
        "high_price_of_the_day": 10_400,
        "low_price_of_the_day": 9_900,
        "closed_price": 9_950,
        "total_buy_quantity": 0,
        "total_sell_quantity": 0,
        "average_traded_price": 10_150,
    }


def parse_feed(ws_manager, clock, feed=SMARTAPI_FEED):
    """Parse SmartAPI messages into Ticks under the controlled clock."""
    parsed = []
    for wall_clock, ltp_paisa, cumulative_volume in feed:
        clock.current = wall_clock
        tick = ws_manager._parse_tick(smartapi_message(ltp_paisa, cumulative_volume))
        assert tick is not None
        parsed.append(tick)
    return parsed


def run_live_pipeline(ws_manager, aggregator, clock, feed=SMARTAPI_FEED):
    """Feed SmartAPI messages through parse -> aggregate -> flush."""
    parsed = parse_feed(ws_manager, clock, feed)
    for tick in parsed:
        aggregator.on_tick(tick)  # same call as on_tick_handler in main.py
    flushed = aggregator.flush_all()  # same call as market_close in main.py
    return parsed, flushed


def rows_by_timestamp(rows: list[tuple]) -> dict[datetime, dict]:
    """(stock_id, ts, o, h, l, c, volume, vwap) -> {ts: {...}}"""
    out = {}
    for stock_id, ts, o, h, l, c, volume, vwap in rows:
        out[ts] = {
            "stock_id": stock_id, "open": o, "high": h, "low": l,
            "close": c, "volume": volume, "vwap": vwap,
        }
    return out


# ── Tests ────────────────────────────────────────────────────


def test_parse_tick_exposes_cumulative_day_volume(ws_manager, clock):
    """Documents the input contract: Tick.volume is SmartAPI's running total."""
    parsed = parse_feed(ws_manager, clock)
    assert [t.volume for t in parsed] == [1_000, 1_500, 2_200, 2_500, 2_900]
    assert [t.ltp for t in parsed] == [100.0, 102.0, 101.0, 103.0, 104.0]
    assert [t.timestamp for t in parsed] == [wc for wc, _, _ in SMARTAPI_FEED]


def test_flush_writes_five_minute_rows_through_candle_store(
    ws_manager, aggregator, clock, fake_store, fake_db
):
    """Wiring/storage: two completed candles reach CandleStore.insert_batch
    for the 5-minute table, with the token mapped to its stock_id."""
    _, flushed = run_live_pipeline(ws_manager, aggregator, clock)

    assert flushed == 2
    assert fake_store.dbs == [fake_db]
    assert [interval for interval, _ in fake_store.batches] == [CandleInterval.FIVE_MINUTE]

    candles = rows_by_timestamp(fake_store.rows())
    assert set(candles) == set(EXPECTED_CANDLES)
    assert {c["stock_id"] for c in candles.values()} == {STOCK_ID}
    for ts, expected in EXPECTED_CANDLES.items():
        for key in ("open", "high", "low", "close"):
            assert candles[ts][key] == expected[key], f"{ts:%H:%M} {key}"


def test_candle_volume_is_delta_of_cumulative_day_volume(
    ws_manager, aggregator, clock, fake_store
):
    """Each 5m candle must hold only the volume traded inside its interval.

    SmartAPI sends the cumulative day total, so the 09:20 candle must be
    2_900 - 2_200 = 700, and the candle volumes must sum to the final
    cumulative counter rather than double-counting earlier trades.
    """
    run_live_pipeline(ws_manager, aggregator, clock)
    candles = rows_by_timestamp(fake_store.rows())

    actual_volumes = {ts: c["volume"] for ts, c in candles.items()}
    expected_volumes = {ts: e["volume"] for ts, e in EXPECTED_CANDLES.items()}
    assert actual_volumes == expected_volumes

    final_cumulative_volume = SMARTAPI_FEED[-1][2]
    assert sum(actual_volumes.values()) == final_cumulative_volume


def test_candle_vwap_weights_ticks_by_incremental_volume(
    ws_manager, aggregator, clock, fake_store
):
    """VWAP must weight each tick by the volume traded *since the previous
    tick*, not by the running day total."""
    run_live_pipeline(ws_manager, aggregator, clock)
    candles = rows_by_timestamp(fake_store.rows())

    actual_vwaps = {ts: c["vwap"] for ts, c in candles.items()}
    expected_vwaps = {ts: e["vwap"] for ts, e in EXPECTED_CANDLES.items()}
    assert actual_vwaps == expected_vwaps


# ── Mid-session subscription example ────────────────────────
#
# The stream starts mid-session (e.g. late subscribe / reconnect). The first
# observation carries the day's running total up to that point; none of that
# volume belongs to any candle we build, so it is only a *baseline*.
#
# Wall-clock   LTP      volume_trade_for_the_day   traded since previous tick
# 10:19:00      95.00   1_000                      (baseline — not counted)
# ---- 5-minute boundary ----
# 10:20:00     100.00   1_040                      40
# 10:24:00     110.00   1_050                      10
# ---- 5-minute boundary ----
# 10:25:00     120.00   1_080                      30
#
# Expected:
#   10:20 candle  volume = 40 + 10 = 50   vwap = (100*40 + 110*10) / 50 = 102
#   10:25 candle  starts with volume 30

MID_SESSION_FEED = [
    (datetime(2025, 1, 6, 10, 19, 0, tzinfo=IST), 9_500, 1_000),
    (datetime(2025, 1, 6, 10, 20, 0, tzinfo=IST), 10_000, 1_040),
    (datetime(2025, 1, 6, 10, 24, 0, tzinfo=IST), 11_000, 1_050),
    (datetime(2025, 1, 6, 10, 25, 0, tzinfo=IST), 12_000, 1_080),
]

TS_10_20 = datetime(2025, 1, 6, 10, 20, tzinfo=IST)
TS_10_25 = datetime(2025, 1, 6, 10, 25, tzinfo=IST)


def test_mid_session_first_tick_is_baseline(ws_manager, aggregator, clock, fake_store):
    """A stream that first sees a symbol mid-session must treat that first
    cumulative reading as the baseline, not as volume traded in a candle."""
    parsed = parse_feed(ws_manager, clock, MID_SESSION_FEED)
    for tick in parsed:
        aggregator.on_tick(tick)

    # The 10:25 bucket is still open: it should start with only the 30 shares
    # traded between the 10:24 and 10:25 readings.
    open_bucket = aggregator._buckets[TOKEN]
    assert open_bucket.interval_start == TS_10_25
    assert open_bucket.volume == 30

    aggregator.flush_all()
    candles = rows_by_timestamp(fake_store.rows())

    assert candles[TS_10_20]["volume"] == 50
    assert candles[TS_10_20]["vwap"] == 102.0
    assert candles[TS_10_25]["volume"] == 30


def test_baseline_only_candle_is_not_saved(ws_manager, aggregator, clock, fake_store):
    """The 10:15 bucket holds nothing but the 10:19 baseline reading, so it
    must not be written as a zero-volume (or 1000-volume) candle."""
    _, flushed = run_live_pipeline(ws_manager, aggregator, clock, MID_SESSION_FEED)
    candles = rows_by_timestamp(fake_store.rows())

    assert flushed == 2
    assert set(candles) == {TS_10_20, TS_10_25}


# ── Interleaved two-token stream ────────────────────────────
#
# Both tokens are first seen in the 09:15 opening interval, so their first
# readings count as opening-candle volume. The per-token cumulative counters
# are at very different levels; if the aggregator shared one "previous
# reading" across tokens, every delta below would be wrong.
#
# Wall-clock   token        LTP   cumulative   increment
# 09:15:05     RELIANCE     100   1_000        1_000 (opening)
# 09:15:20     TCS          200   5_000        5_000 (opening)
# 09:17:00     RELIANCE     101   1_300          300
# 09:17:30     TCS          202   5_100          100
# 09:19:00     TCS          201   5_700          600
# 09:20:00     RELIANCE     102   1_500          200   → closes RELIANCE 09:15
# 09:20:30     TCS          203   5_750           50   → closes TCS 09:15

TOKEN_B = "11536"

TWO_TOKEN_FEED = [
    # (wall-clock,                                 token,   name,       ltp paisa, cum vol)
    (datetime(2025, 1, 6, 9, 15, 5, tzinfo=IST), TOKEN, "RELIANCE", 10_000, 1_000),
    (datetime(2025, 1, 6, 9, 15, 20, tzinfo=IST), TOKEN_B, "TCS", 20_000, 5_000),
    (datetime(2025, 1, 6, 9, 17, 0, tzinfo=IST), TOKEN, "RELIANCE", 10_100, 1_300),
    (datetime(2025, 1, 6, 9, 17, 30, tzinfo=IST), TOKEN_B, "TCS", 20_200, 5_100),
    (datetime(2025, 1, 6, 9, 19, 0, tzinfo=IST), TOKEN_B, "TCS", 20_100, 5_700),
    (datetime(2025, 1, 6, 9, 20, 0, tzinfo=IST), TOKEN, "RELIANCE", 10_200, 1_500),
    (datetime(2025, 1, 6, 9, 20, 30, tzinfo=IST), TOKEN_B, "TCS", 20_300, 5_750),
]

TS_09_15 = datetime(2025, 1, 6, 9, 15, tzinfo=IST)
TS_09_20 = datetime(2025, 1, 6, 9, 20, tzinfo=IST)


def test_interleaved_tokens_keep_independent_volume_counters(
    ws_manager, clock, fake_store, fake_db
):
    aggregator = CandleAggregator(
        db=fake_db,
        interval_min=5,
        token_to_stock_id={TOKEN: STOCK_ID, TOKEN_B: 7},
    )

    for wall_clock, token, name, ltp_paisa, cumulative_volume in TWO_TOKEN_FEED:
        clock.current = wall_clock
        tick = ws_manager._parse_tick(
            smartapi_message(ltp_paisa, cumulative_volume, token=token, name=name)
        )
        assert tick is not None
        aggregator.on_tick(tick)

    flushed = aggregator.flush_all()
    assert flushed == 4

    by_stock: dict[int, dict[datetime, dict]] = {}
    for row in fake_store.rows():
        by_stock.setdefault(row[0], {}).update(rows_by_timestamp([row]))

    reliance = by_stock[STOCK_ID]
    tcs = by_stock[7]

    # RELIANCE: opening candle counts the first reading, then +300
    assert reliance[TS_09_15]["volume"] == 1_300
    assert reliance[TS_09_15]["vwap"] == round((100 * 1_000 + 101 * 300) / 1_300, 2)
    assert reliance[TS_09_20]["volume"] == 200

    # TCS: 5_000 + 100 + 600, unaffected by RELIANCE's readings in between
    assert tcs[TS_09_15]["volume"] == 5_700
    assert tcs[TS_09_15]["vwap"] == round(
        (200 * 5_000 + 202 * 100 + 201 * 600) / 5_700, 2
    )
    assert tcs[TS_09_20]["volume"] == 50

    # Per-token candle volumes reconcile with each token's final counter
    assert sum(c["volume"] for c in reliance.values()) == 1_500
    assert sum(c["volume"] for c in tcs.values()) == 5_750


# ── Stale / out-of-order cumulative reading ─────────────────
#
# Wall-clock   LTP   cumulative   note
# 09:15:10     100   1_000        opening reading → 1_000
# 09:16:00     101     900        stale (lower than last reliable counter)
# 09:17:00     102   1_050        +50 relative to 1_000, NOT +150 relative to 900
#
# Expected 09:15 candle volume = 1_050. A lower reading must not replace the
# last reliable counter, otherwise the 50 → 150 jump double-counts volume.

STALE_READING_FEED = [
    (datetime(2025, 1, 6, 9, 15, 10, tzinfo=IST), 10_000, 1_000),
    (datetime(2025, 1, 6, 9, 16, 0, tzinfo=IST), 10_100, 900),
    (datetime(2025, 1, 6, 9, 17, 0, tzinfo=IST), 10_200, 1_050),
]

NEXT_DAY_OPEN = datetime(2025, 1, 7, 9, 15, tzinfo=IST)


def test_stale_lower_cumulative_reading_does_not_double_count(
    ws_manager, aggregator, clock, fake_store
):
    run_live_pipeline(ws_manager, aggregator, clock, STALE_READING_FEED)
    candles = rows_by_timestamp(fake_store.rows())

    assert set(candles) == {TS_09_15}
    assert candles[TS_09_15]["volume"] == 1_050
    # Only the 1_000 (at 100) and the +50 (at 102) carry weight; the stale
    # reading contributes no volume.
    assert candles[TS_09_15]["vwap"] == round((100 * 1_000 + 102 * 50) / 1_050, 2)

    # A genuine new trading day goes through reset_daily(): the counter
    # restarting at 400 is opening volume, not a "stale" reading below 1_050.
    aggregator.reset_daily()
    fake_store.reset()
    run_live_pipeline(
        ws_manager, aggregator, clock,
        [(NEXT_DAY_OPEN.replace(second=5), 10_300, 400)],
    )
    next_day = rows_by_timestamp(fake_store.rows())
    assert set(next_day) == {NEXT_DAY_OPEN}
    assert next_day[NEXT_DAY_OPEN]["volume"] == 400


# ── Repeated baseline reading with no new volume ────────────
#
# Wall-clock   LTP   cumulative   note
# 10:19:00      95   1_000        mid-session first sight → baseline
# 10:19:30      95   1_000        same counter → still no observed volume
# 10:20:00     100   1_040        +40 → first real candle
#
# The 10:15 bucket never observed any new volume, so it must not be saved —
# neither when the 10:20 tick rotates it out nor when flush_all() runs on
# an un-rotated bucket.

REPEATED_BASELINE_FEED = [
    (datetime(2025, 1, 6, 10, 19, 0, tzinfo=IST), 9_500, 1_000),
    (datetime(2025, 1, 6, 10, 19, 30, tzinfo=IST), 9_500, 1_000),
]

TS_10_15 = datetime(2025, 1, 6, 10, 15, tzinfo=IST)


def test_repeated_baseline_reading_does_not_save_zero_volume_candle(
    ws_manager, clock, fake_store, fake_db
):
    # Path 1: interval rotation (10:20 tick closes the 10:15 bucket)
    aggregator = CandleAggregator(db=fake_db, interval_min=5, token_to_stock_id={TOKEN: STOCK_ID})
    _, flushed = run_live_pipeline(
        ws_manager, aggregator, clock,
        REPEATED_BASELINE_FEED + [(TS_10_20, 10_000, 1_040)],
    )
    candles = rows_by_timestamp(fake_store.rows())
    assert TS_10_15 not in candles
    assert flushed == 1
    assert candles[TS_10_20]["volume"] == 40

    # Path 2: flush_all() on the still-open 10:15 bucket
    fake_store.reset()
    aggregator = CandleAggregator(db=fake_db, interval_min=5, token_to_stock_id={TOKEN: STOCK_ID})
    _, flushed = run_live_pipeline(ws_manager, aggregator, clock, REPEATED_BASELINE_FEED)
    assert flushed == 0
    assert fake_store.rows() == []


# ── Startup wiring: token → stock_id mapping in scanner_warmup ──────
#
# main.scanner_warmup() delegates token selection + mapping to
# main.prepare_live_subscription(), which is exercised here with fake
# watchlist / instrument / stock stores.

import main as startup
from storage.models import StockStore


class FakeWatchlistManager:
    def __init__(self, tokens):
        self._tokens = tokens
        self.calls = 0

    def get_subscribe_tokens(self):
        self.calls += 1
        return list(self._tokens)


class FakeInstrument:
    def __init__(self, token):
        self.token = token


class FakeInstrumentLoader:
    def __init__(self, tokens):
        self._tokens = tokens
        self.calls = 0

    def get_all_by_exchange(self, exchange=None):
        self.calls += 1
        return [FakeInstrument(t) for t in self._tokens]


class FakeStockStore:
    """Stands in for storage.models.StockStore.get_ids_by_tokens."""

    def __init__(self, mapping):
        self._mapping = mapping
        self.batches: list[list[str]] = []
        self.exchanges: list[str] = []

    def get_ids_by_tokens(self, tokens, exchange="NSE"):
        self.batches.append(list(tokens))
        self.exchanges.append(exchange)
        return {t: self._mapping[t] for t in tokens if t in self._mapping}


def stock_row(id, token, exchange="NSE", is_active=True):
    return {"id": id, "token": token, "exchange": exchange, "is_active": is_active}


class FakeStocksDB:
    """
    Tiny in-memory ``stocks`` table for the real StockStore batch query.

    Applies the equality predicates found in the WHERE clause
    (``col = %s``, ``col = ANY(%s)``, ``col = TRUE``) in order, consuming
    params positionally. ORDER BY is ignored: rows come back in insertion
    order, so a query that does not filter by exchange sees the first row
    inserted for a shared token.
    """

    def __init__(self, rows):
        self._rows = rows
        self.queries: list[tuple[str, tuple]] = []

    def fetch_all(self, query, params=()):
        import re

        self.queries.append((query, params))
        where = query.split("WHERE", 1)[1].split("ORDER BY")[0] if "WHERE" in query else ""
        params_iter = iter(params)
        predicates = []
        for column, rhs in re.findall(r"(\w+)\s*=\s*(ANY\(%s\)|%s|TRUE)", where):
            if rhs == "TRUE":
                predicates.append((column, lambda v: v is True))
            elif rhs == "%s":
                expected = next(params_iter)
                predicates.append((column, lambda v, e=expected: v == e))
            else:
                allowed = set(next(params_iter))
                predicates.append((column, lambda v, a=allowed: v in a))
        return [
            {"id": r["id"], "token": r["token"]}
            for r in self._rows
            if all(pred(r[col]) for col, pred in predicates)
        ]


def test_stock_store_resolves_tokens_in_one_batch_query():
    db = FakeStocksDB([stock_row(42, "2885"), stock_row(7, "11536")])
    mapping = StockStore(db).get_ids_by_tokens(["2885", "11536", "99999"])

    assert mapping == {"2885": 42, "11536": 7}
    assert len(db.queries) == 1
    query, params = db.queries[0]
    assert "token = ANY(%s)" in query
    assert params[0] == ["2885", "11536", "99999"]

    # Empty selection never hits the database
    assert StockStore(db).get_ids_by_tokens([]) == {}
    assert len(db.queries) == 1


def test_stock_store_prefers_active_nse_row_when_bse_shares_token():
    """SmartAPI token numbers overlap between NSE and BSE; the live feed
    subscribes NSE tokens, so only the active NSE row may be used."""
    db = FakeStocksDB([
        stock_row(5, TOKEN, exchange="BSE"),            # lower id, inserted first
        stock_row(9, TOKEN, exchange="NSE", is_active=False),
        stock_row(42, TOKEN, exchange="NSE"),
        stock_row(7, TOKEN_B, exchange="NSE"),
    ])

    assert StockStore(db).get_ids_by_tokens([TOKEN, TOKEN_B]) == {TOKEN: 42, TOKEN_B: 7}


def test_token_without_active_nse_row_is_left_unmapped_and_candle_held(
    ws_manager, clock, fake_store, fake_db
):
    db = FakeStocksDB([
        stock_row(5, TOKEN, exchange="BSE"),            # active, but wrong exchange
        stock_row(9, TOKEN, exchange="NSE", is_active=False),
        stock_row(7, TOKEN_B, exchange="NSE"),
    ])
    aggregator = CandleAggregator(db=fake_db, interval_min=5)

    startup.prepare_live_subscription(
        watchlist_manager=FakeWatchlistManager([TOKEN, TOKEN_B]),
        instruments=FakeInstrumentLoader([]),
        stock_store=StockStore(db),
        candle_aggregator=aggregator,
    )
    assert aggregator._token_to_stock_id == {TOKEN_B: 7}

    for wall_clock, token, name, ltp_paisa, cumulative_volume in TWO_TOKEN_FEED:
        clock.current = wall_clock
        aggregator.on_tick(
            ws_manager._parse_tick(
                smartapi_message(ltp_paisa, cumulative_volume, token=token, name=name)
            )
        )
    assert aggregator.flush_all() == 2                 # TCS only

    assert {row[0] for row in fake_store.rows()} == {7}
    assert 5 not in {row[0] for row in fake_store.rows()}  # BSE id never used
    assert aggregator.pending_unmapped == 2            # RELIANCE candles held


def test_failed_warmup_lookup_preserves_last_valid_mapping(
    ws_manager, clock, fake_store, fake_db
):
    class BrokenStockStore:
        def get_ids_by_tokens(self, tokens, exchange="NSE"):
            raise RuntimeError("db connection lost")

    aggregator = CandleAggregator(db=fake_db, interval_min=5)
    watchlists = FakeWatchlistManager([TOKEN])
    instruments = FakeInstrumentLoader([])

    # 1. First warm-up succeeds
    startup.prepare_live_subscription(
        watchlist_manager=watchlists, instruments=instruments,
        stock_store=FakeStockStore({TOKEN: STOCK_ID}), candle_aggregator=aggregator,
    )
    assert aggregator._token_to_stock_id == {TOKEN: STOCK_ID}

    # 2. Later warm-up's lookup fails → last valid mapping kept, no exception
    tokens = startup.prepare_live_subscription(
        watchlist_manager=watchlists, instruments=instruments,
        stock_store=BrokenStockStore(), candle_aggregator=aggregator,
    )
    assert tokens == [TOKEN]
    assert aggregator._token_to_stock_id == {TOKEN: STOCK_ID}

    run_live_pipeline(ws_manager, aggregator, clock)   # live candles still saved
    assert {row[0] for row in fake_store.rows()} == {STOCK_ID}
    assert aggregator.pending_unmapped == 0

    # 3. A later successful lookup replaces the mapping as usual
    startup.prepare_live_subscription(
        watchlist_manager=watchlists, instruments=instruments,
        stock_store=FakeStockStore({TOKEN: 77}), candle_aggregator=aggregator,
    )
    assert aggregator._token_to_stock_id == {TOKEN: 77}


def test_warmup_maps_watchlist_tokens_before_live_candles_are_saved(
    ws_manager, clock, fake_store, fake_db
):
    aggregator = CandleAggregator(db=fake_db, interval_min=5)  # no mapping yet, as in main()
    watchlists = FakeWatchlistManager([TOKEN, TOKEN_B])
    instruments = FakeInstrumentLoader(["1", "2", "3"])
    stock_store = FakeStockStore({TOKEN: STOCK_ID, TOKEN_B: 7, "1": 901})

    tokens = startup.prepare_live_subscription(
        watchlist_manager=watchlists,
        instruments=instruments,
        stock_store=stock_store,
        candle_aggregator=aggregator,
    )

    assert tokens == [TOKEN, TOKEN_B]
    assert instruments.calls == 0                      # fallback not used
    assert stock_store.batches == [[TOKEN, TOKEN_B]]   # one batch lookup
    assert stock_store.exchanges == ["NSE"]            # live feed is NSE-only

    # Live ticks after warm-up are saved with the resolved stock_ids
    for wall_clock, token, name, ltp_paisa, cumulative_volume in TWO_TOKEN_FEED:
        clock.current = wall_clock
        aggregator.on_tick(
            ws_manager._parse_tick(
                smartapi_message(ltp_paisa, cumulative_volume, token=token, name=name)
            )
        )
    assert aggregator.flush_all() == 4

    saved = {(row[0], row[1]) for row in fake_store.rows()}
    assert saved == {
        (STOCK_ID, TS_09_15), (STOCK_ID, TS_09_20),
        (7, TS_09_15), (7, TS_09_20),
    }
    assert all(row[0] is not None for row in fake_store.rows())


def test_warmup_maps_instrument_fallback_tokens_when_watchlists_empty(
    ws_manager, clock, fake_store, fake_db
):
    aggregator = CandleAggregator(db=fake_db, interval_min=5)
    watchlists = FakeWatchlistManager([])
    instruments = FakeInstrumentLoader([TOKEN, TOKEN_B, "55555"])
    stock_store = FakeStockStore({TOKEN: STOCK_ID, TOKEN_B: 7, "55555": 3})

    tokens = startup.prepare_live_subscription(
        watchlist_manager=watchlists,
        instruments=instruments,
        stock_store=stock_store,
        candle_aggregator=aggregator,
        fallback_limit=2,
    )

    assert watchlists.calls == 1
    assert tokens == [TOKEN, TOKEN_B]                  # first `fallback_limit` instruments
    assert stock_store.batches == [[TOKEN, TOKEN_B]]

    run_live_pipeline(ws_manager, aggregator, clock)   # RELIANCE market-open feed
    assert {row[0] for row in fake_store.rows()} == {STOCK_ID}

    # Each warm-up refreshes the mapping rather than accumulating stale entries
    startup.prepare_live_subscription(
        watchlist_manager=FakeWatchlistManager([TOKEN_B]),
        instruments=instruments,
        stock_store=stock_store,
        candle_aggregator=aggregator,
    )
    assert aggregator._token_to_stock_id == {TOKEN_B: 7}


def test_unmapped_candle_is_held_until_mapping_available(
    ws_manager, clock, fake_store, fake_db
):
    # Only RELIANCE is mapped; TCS candles must not be written with stock_id=None
    aggregator = CandleAggregator(
        db=fake_db, interval_min=5, token_to_stock_id={TOKEN: STOCK_ID}
    )
    for wall_clock, token, name, ltp_paisa, cumulative_volume in TWO_TOKEN_FEED:
        clock.current = wall_clock
        aggregator.on_tick(
            ws_manager._parse_tick(
                smartapi_message(ltp_paisa, cumulative_volume, token=token, name=name)
            )
        )

    flushed = aggregator.flush_all()

    assert flushed == 2                                # RELIANCE candles written normally
    assert {row[0] for row in fake_store.rows()} == {STOCK_ID}
    assert all(row[0] is not None for row in fake_store.rows())
    assert aggregator.pending_candles == 2             # TCS candles held, not dropped
    assert aggregator.pending_unmapped == 2

    # Still unmapped on a later flush: nothing new written, still pending
    assert aggregator._do_flush() == 0
    assert aggregator.pending_candles == 2

    # Mapping becomes available (e.g. next warm-up) → held candles flush
    aggregator.set_token_mapping({TOKEN: STOCK_ID, TOKEN_B: 7})
    assert aggregator._do_flush() == 2
    assert aggregator.pending_candles == 0

    tcs_rows = [row for row in fake_store.rows() if row[0] == 7]
    assert {row[1] for row in tcs_rows} == {TS_09_15, TS_09_20}
    assert {row[6] for row in tcs_rows} == {5_700, 50}   # volumes preserved while held


# ── Database write failure recovery in _do_flush ────────────


class FlakyCandleStore(FakeCandleStore):
    """CandleStore double whose first ``fail_first`` insert_batch calls raise.

    A failed attempt records nothing — exactly like a DB error that rolls
    back the batch."""

    fail_first: int = 1
    attempts: int = 0

    def insert_batch(self, interval, rows):
        FlakyCandleStore.attempts += 1
        if FlakyCandleStore.attempts <= FlakyCandleStore.fail_first:
            raise RuntimeError("connection reset by peer")
        return super().insert_batch(interval, rows)

    @classmethod
    def reset(cls, fail_first: int = 1):
        super().reset()
        cls.fail_first = fail_first
        cls.attempts = 0


@pytest.fixture
def flaky_store(monkeypatch):
    FlakyCandleStore.reset(fail_first=1)
    monkeypatch.setattr("storage.models.CandleStore", FlakyCandleStore)
    return FlakyCandleStore


def feed_two_tokens(ws_manager, aggregator, clock):
    for wall_clock, token, name, ltp_paisa, cumulative_volume in TWO_TOKEN_FEED:
        clock.current = wall_clock
        aggregator.on_tick(
            ws_manager._parse_tick(
                smartapi_message(ltp_paisa, cumulative_volume, token=token, name=name)
            )
        )


def test_failed_write_keeps_mapped_candles_pending_for_retry(
    ws_manager, aggregator, clock, flaky_store
):
    """Fully mapped batch: the first DB write fails, nothing may be lost."""
    for tick in parse_feed(ws_manager, clock):
        aggregator.on_tick(tick)

    # Attempt 1 (market_close path): write fails
    assert aggregator.flush_all() == 0
    assert flaky_store.attempts == 1
    assert flaky_store.rows() == []
    assert aggregator.pending_candles == 2
    assert aggregator.pending_unmapped == 0
    assert aggregator.stats["candles_flushed"] == 0
    assert aggregator.stats["flush_failures"] == 1

    # Attempt 2 (background loop path): the same candles are written intact
    assert aggregator._do_flush() == 2
    assert flaky_store.attempts == 2
    assert aggregator.pending_candles == 0
    assert aggregator.stats["candles_flushed"] == 2
    assert aggregator.stats["flush_failures"] == 1

    candles = rows_by_timestamp(flaky_store.rows())
    assert set(candles) == set(EXPECTED_CANDLES)
    for ts, expected in EXPECTED_CANDLES.items():
        assert candles[ts]["stock_id"] == STOCK_ID
        assert candles[ts]["volume"] == expected["volume"]
        assert candles[ts]["vwap"] == expected["vwap"]

    # Nothing left to retry; no duplicate write
    assert aggregator._do_flush() == 0
    assert flaky_store.attempts == 2
    assert len(flaky_store.rows()) == 2


def test_failed_write_with_mixed_mapping_retries_only_mapped_candles(
    ws_manager, clock, flaky_store, fake_db
):
    """Batch with a mapped (RELIANCE) and an unmapped (TCS) token: after a
    failed write all four stay pending; the retry writes the two mapped
    candles, the unmapped ones keep waiting for a mapping."""
    aggregator = CandleAggregator(db=fake_db, interval_min=5, token_to_stock_id={TOKEN: STOCK_ID})
    feed_two_tokens(ws_manager, aggregator, clock)

    # Attempt 1 fails
    assert aggregator.flush_all() == 0
    assert flaky_store.rows() == []
    assert aggregator.pending_candles == 4
    assert aggregator.pending_unmapped == 2
    assert aggregator.stats["candles_flushed"] == 0
    assert aggregator.stats["flush_failures"] == 1

    # Attempt 2 succeeds for the mapped candles only
    assert aggregator._do_flush() == 2
    assert aggregator.pending_candles == 2
    assert aggregator.pending_unmapped == 2
    assert aggregator.stats["candles_flushed"] == 2

    saved = rows_by_timestamp(flaky_store.rows())
    assert {c["stock_id"] for c in saved.values()} == {STOCK_ID}
    assert saved[TS_09_15]["volume"] == 1_300
    assert saved[TS_09_15]["vwap"] == round((100 * 1_000 + 101 * 300) / 1_300, 2)
    assert saved[TS_09_20]["volume"] == 200

    # Mapping arrives later → held TCS candles flush with their values intact
    aggregator.set_token_mapping({TOKEN: STOCK_ID, TOKEN_B: 7})
    assert aggregator._do_flush() == 2
    assert aggregator.pending_candles == 0
    assert aggregator.stats["candles_flushed"] == 4
    tcs = {row[1]: row for row in flaky_store.rows() if row[0] == 7}
    assert tcs[TS_09_15][6] == 5_700
    assert tcs[TS_09_15][7] == round((200 * 5_000 + 202 * 100 + 201 * 600) / 5_700, 2)
    assert tcs[TS_09_20][6] == 50
    assert len(flaky_store.rows()) == 4


def test_concurrent_flushes_neither_lose_nor_duplicate_candles(
    ws_manager, clock, flaky_store, fake_db
):
    """Background _flush_loop and a manual flush_all can overlap. With the
    first write failing, every candle must still be written exactly once."""
    import threading

    aggregator = CandleAggregator(
        db=fake_db, interval_min=5, token_to_stock_id={TOKEN: STOCK_ID, TOKEN_B: 7}
    )
    feed_two_tokens(ws_manager, aggregator, clock)

    results = []
    threads = [
        threading.Thread(target=lambda: results.append(aggregator.flush_all())),
        threading.Thread(target=lambda: results.append(aggregator._do_flush())),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    # One attempt failed, so whatever remains is retried here
    results.append(aggregator._do_flush())

    assert sum(results) == 4
    assert aggregator.pending_candles == 0
    assert aggregator.stats["candles_flushed"] == 4
    saved = [(row[0], row[1]) for row in flaky_store.rows()]
    assert sorted(saved) == sorted([
        (STOCK_ID, TS_09_15), (STOCK_ID, TS_09_20), (7, TS_09_15), (7, TS_09_20),
    ])


# ── Daily reset must not discard unsaved candles ────────────

DAY2_OPEN = datetime(2025, 1, 7, 9, 15, tzinfo=IST)
DAY2_0920 = datetime(2025, 1, 7, 9, 20, tzinfo=IST)


def test_reset_daily_keeps_unsaved_candles_and_restarts_volume_counters(
    ws_manager, clock, flaky_store, fake_db
):
    """End of day 1: RELIANCE is mapped but the DB write fails; TCS has no
    mapping; the 09:20 buckets are still open. reset_daily() must try to
    flush, keep whatever could not be saved, and start day 2's counters
    from scratch so the opening reading counts as opening volume."""
    aggregator = CandleAggregator(db=fake_db, interval_min=5, token_to_stock_id={TOKEN: STOCK_ID})
    feed_two_tokens(ws_manager, aggregator, clock)
    assert aggregator.active_buckets == 2              # 09:20 buckets still open

    # ── Pre-market reset for day 2: the flush attempt fails ──
    aggregator.reset_daily()

    assert flaky_store.attempts == 1                   # it did try to write
    assert flaky_store.rows() == []
    assert aggregator.active_buckets == 0
    assert aggregator.pending_candles == 4             # nothing discarded
    assert aggregator.pending_unmapped == 2
    assert aggregator.stats["flush_failures"] == 1
    assert aggregator._last_cum_volume == {}           # counters reset

    # ── Day 2 opening: counter restarts at 400, well below day 1's 1_500 ──
    run_live_pipeline(
        ws_manager, aggregator, clock,
        [(DAY2_OPEN.replace(second=5), 10_500, 400), (DAY2_0920, 10_600, 450)],
    )
    # That flush_all succeeded: day-1 RELIANCE candles + day-2 opening candle
    assert aggregator.pending_candles == 2             # only unmapped TCS left
    assert aggregator.pending_unmapped == 2
    assert aggregator.stats["candles_flushed"] == 4

    reliance = rows_by_timestamp([r for r in flaky_store.rows() if r[0] == STOCK_ID])
    assert set(reliance) == {TS_09_15, TS_09_20, DAY2_OPEN, DAY2_0920}
    assert reliance[TS_09_15]["volume"] == 1_300       # yesterday, saved late
    assert reliance[TS_09_20]["volume"] == 200         # was an open bucket at reset
    assert reliance[DAY2_OPEN]["volume"] == 400        # fresh opening volume
    assert reliance[DAY2_0920]["volume"] == 50

    # ── Mapping for TCS arrives on day 2 → yesterday's TCS candles saved ──
    aggregator.set_token_mapping({TOKEN: STOCK_ID, TOKEN_B: 7})
    assert aggregator._do_flush() == 2
    assert aggregator.pending_candles == 0
    tcs = rows_by_timestamp([r for r in flaky_store.rows() if r[0] == 7])
    assert tcs[TS_09_15]["volume"] == 5_700
    assert tcs[TS_09_20]["volume"] == 50
    assert len(flaky_store.rows()) == 6


# ── Shutdown: pending candles must survive a process restart ───


def test_stop_after_failed_write_persists_pending_and_restart_saves_them_once(
    ws_manager, clock, flaky_store, fake_db, tmp_path
):
    """Process 1 stops after its final write fails (RELIANCE mapped, TCS
    not). Process 2 restarts with a fresh aggregator and must save exactly
    those candles — RELIANCE immediately, TCS once its mapping exists —
    with no duplicates."""
    spill = tmp_path / "candle_spill_5m.json"

    # ── Process 1: final flush fails at shutdown ──
    agg1 = CandleAggregator(
        db=fake_db, interval_min=5, token_to_stock_id={TOKEN: STOCK_ID}, spill_path=spill
    )
    feed_two_tokens(ws_manager, agg1, clock)
    agg1.stop()

    assert flaky_store.attempts == 1
    assert flaky_store.rows() == []
    assert agg1.stats["pending_candles"] == 4
    assert agg1.stats["pending_unmapped"] == 2
    assert agg1.stats["spilled_candles"] == 4
    assert spill.exists()

    # ── Process 2: fresh aggregator, same mapping, DB healthy again ──
    agg2 = CandleAggregator(
        db=fake_db, interval_min=5, token_to_stock_id={TOKEN: STOCK_ID}, spill_path=spill
    )
    agg2.start()
    assert agg2.pending_candles == 4                   # recovered, not yet written
    assert agg2.pending_unmapped == 2
    assert spill.exists()                              # kept until actually saved
    agg2.stop()

    reliance = rows_by_timestamp(flaky_store.rows())
    assert {c["stock_id"] for c in reliance.values()} == {STOCK_ID}
    assert reliance[TS_09_15]["volume"] == 1_300
    assert reliance[TS_09_15]["vwap"] == round((100 * 1_000 + 101 * 300) / 1_300, 2)
    assert reliance[TS_09_20]["volume"] == 200
    assert agg2.stats["candles_flushed"] == 2
    assert agg2.stats["spilled_candles"] == 2          # TCS spilled again
    assert spill.exists()

    # ── Process 3: TCS is now mapped ──
    agg3 = CandleAggregator(
        db=fake_db, interval_min=5,
        token_to_stock_id={TOKEN: STOCK_ID, TOKEN_B: 7}, spill_path=spill,
    )
    agg3.start()
    agg3.stop()

    assert agg3.stats["candles_flushed"] == 2
    assert agg3.stats["pending_candles"] == 0
    assert agg3.stats["spilled_candles"] == 0
    assert not spill.exists()

    saved = [(row[0], row[1]) for row in flaky_store.rows()]
    assert len(saved) == 4 and len(set(saved)) == 4   # each candle exactly once
    tcs = rows_by_timestamp([r for r in flaky_store.rows() if r[0] == 7])
    assert tcs[TS_09_15]["volume"] == 5_700
    assert tcs[TS_09_20]["volume"] == 50

    # ── Process 4: nothing to recover, nothing written ──
    agg4 = CandleAggregator(db=fake_db, interval_min=5, spill_path=spill)
    agg4.start()
    agg4.stop()
    assert flaky_store.attempts == 3
    assert len(flaky_store.rows()) == 4


PROJECT_ROOT = str(Path(__file__).resolve().parents[2])

# Child process used to simulate a real restart. It recovers the spill file,
# attempts one flush against a file-backed CandleStore double, and exits via
# os._exit() (no stop(), no atexit, no destructors) unless clean_stop is set.
CHILD_SCRIPT = r"""
import json, os, sys
params = json.loads(sys.argv[1])
sys.path.insert(0, params["root"])
import storage.models
from data.streams.candle_aggregator import CandleAggregator

class FileCandleStore:
    def __init__(self, db):
        pass
    def insert_batch(self, interval, rows):
        with open(params["attempts"], "a") as f:
            f.write("attempt\n")
        if params["fail"]:
            raise RuntimeError("db down")
        with open(params["saved"], "a") as f:
            for r in rows:
                f.write(json.dumps([r[0], r[1].isoformat(), r[6], r[7]]) + "\n")
        return len(rows)

storage.models.CandleStore = FileCandleStore
agg = CandleAggregator(db=object(), interval_min=5,
                       token_to_stock_id=params["mapping"], spill_path=params["spill"])
agg.start()
out = {"recovered": agg.pending_candles}
out["flushed"] = agg._do_flush()
out["pending_after_flush"] = agg.pending_candles
if params["clean_stop"]:
    out["stop_returned"] = agg.stop()
print("RESULT " + json.dumps(out)); sys.stdout.flush()
os._exit(0)
"""


def run_restart(spill, saved, attempts, mapping, fail=False, clean_stop=False):
    """Run one aggregator lifetime in a separate Python process."""
    import json, subprocess, sys

    params = {
        "root": PROJECT_ROOT, "spill": str(spill), "saved": str(saved),
        "attempts": str(attempts), "mapping": mapping, "fail": fail, "clean_stop": clean_stop,
    }
    proc = subprocess.run(
        [sys.executable, "-c", CHILD_SCRIPT, json.dumps(params)],
        cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    line = [l for l in proc.stdout.splitlines() if l.startswith("RESULT ")][-1]
    return json.loads(line[len("RESULT "):])


def read_saved(saved):
    import json

    if not saved.exists():
        return []
    return [json.loads(l) for l in saved.read_text().splitlines() if l.strip()]


def test_spill_file_survives_crash_after_recovery_until_candles_are_saved(
    ws_manager, clock, flaky_store, fake_db, tmp_path
):
    """The spill file must be kept until its candles are actually written,
    so a process that recovers them and then dies before a successful write
    does not lose them. Processes 2-4 are real child processes; 2 and 3
    exit abruptly via os._exit() without stop()."""
    import json

    spill = tmp_path / "candle_spill_5m.json"
    saved = tmp_path / "saved.jsonl"
    attempts = tmp_path / "attempts.log"

    def spilled_keys():
        payload = json.loads(spill.read_text())
        return sorted((c["token"], c["interval_start"]) for c in payload["candles"])

    # ── Process 1 (in-process): clean stop, final write fails → 4 spilled ──
    agg1 = CandleAggregator(
        db=fake_db, interval_min=5, token_to_stock_id={TOKEN: STOCK_ID}, spill_path=spill
    )
    feed_two_tokens(ws_manager, agg1, clock)
    agg1.stop()
    assert flaky_store.attempts == 1 and flaky_store.rows() == []
    assert len(spilled_keys()) == 4

    # ── Process 2: recovers, write fails, exits WITHOUT stop() ──
    r2 = run_restart(spill, saved, attempts, {TOKEN: STOCK_ID}, fail=True)
    assert r2 == {"recovered": 4, "flushed": 0, "pending_after_flush": 4}
    assert spill.exists() and len(spilled_keys()) == 4
    assert read_saved(saved) == []

    # ── Process 3: recovers again, write succeeds, exits WITHOUT stop() ──
    r3 = run_restart(spill, saved, attempts, {TOKEN: STOCK_ID}, fail=False)
    assert r3 == {"recovered": 4, "flushed": 2, "pending_after_flush": 2}
    rows = read_saved(saved)
    assert sorted(rows) == sorted([
        [STOCK_ID, TS_09_15.isoformat(), 1_300, round((100 * 1_000 + 101 * 300) / 1_300, 2)],
        [STOCK_ID, TS_09_20.isoformat(), 200, 102.0],
    ])
    # Spill file now holds only the candles still waiting for a stock_id
    assert spilled_keys() == sorted((TOKEN_B, ts.isoformat()) for ts in (TS_09_15, TS_09_20))

    # ── Process 4: TCS mapped now; recovers 2, clean stop ──
    r4 = run_restart(spill, saved, attempts, {TOKEN: STOCK_ID, TOKEN_B: 7}, clean_stop=True)
    assert r4 == {"recovered": 2, "flushed": 2, "pending_after_flush": 0, "stop_returned": 0}
    assert not spill.exists()                          # everything saved → file gone

    rows = read_saved(saved)
    assert len(rows) == 4 and len({(r[0], r[1]) for r in rows}) == 4   # once each
    tcs = {r[1]: r for r in rows if r[0] == 7}
    assert tcs[TS_09_15.isoformat()][2] == 5_700
    assert tcs[TS_09_20.isoformat()][2] == 50
    assert attempts.read_text().count("attempt") == 3  # + 1 in-process = 4 total


def test_market_close_alert_reports_pending_candles_accurately():
    """main.market_close() uses this to tell the operator what happened to
    candles that could not be saved at close."""
    base = {"pending_unmapped": 1, "flush_failures": 1,
            "last_flush_error": "connection reset by peer",
            "spill_path": "/data/candle_spill_5m.json"}

    assert startup.candle_close_alert(0, {**base, "spilled_candles": 0}) is None

    ok = startup.candle_close_alert(3, {**base, "spilled_candles": 3})
    assert ok is not None
    assert "3 five-minute candle(s)" in ok
    assert "1 awaiting stock_id" in ok and "2 after failed DB write" in ok
    assert "/data/candle_spill_5m.json" in ok and "next start" in ok
    assert "lost" not in ok.lower()

    lost = startup.candle_close_alert(3, {**base, "spilled_candles": 0})
    assert lost is not None
    assert "3 five-minute candle(s)" in lost
    assert "could not be written" in lost and "LOST" in lost
    assert "connection reset by peer" in lost

    # A spill file that could not be loaded must be reported even when
    # nothing new is pending.
    blocked = startup.candle_close_alert(
        0, {**base, "spilled_candles": 0,
            "blocked_spill_files": ["/data/candle_spill_5m.json"]}
    )
    assert blocked is not None
    assert "/data/candle_spill_5m.json" in blocked
    assert "could not be loaded" in blocked and "untouched" in blocked

    both = startup.candle_close_alert(
        2, {**base, "spilled_candles": 2, "pending_unmapped": 2,
            "spill_path": "/data/candle_spill_5m.json.unsaved-x",
            "blocked_spill_files": ["/data/candle_spill_5m.json"]}
    )
    assert "2 five-minute candle(s)" in both and "unsaved-x" in both
    assert "/data/candle_spill_5m.json could not be loaded" in both


def test_failed_recovery_leaves_spill_file_untouched_through_flush_and_stop(
    ws_manager, clock, flaky_store, fake_db, tmp_path
):
    """A spill file that cannot be loaded must never be deleted or
    overwritten by a later flush or stop, and the failure must be reported
    rather than 'nothing pending'."""
    import json

    spill = tmp_path / "candle_spill_5m.json"

    # ── Process 1: clean stop after a failed write → 4 candles spilled ──
    agg1 = CandleAggregator(
        db=fake_db, interval_min=5, token_to_stock_id={TOKEN: STOCK_ID}, spill_path=spill
    )
    feed_two_tokens(ws_manager, agg1, clock)
    agg1.stop()
    good_payload = json.loads(spill.read_text())
    assert len(good_payload["candles"]) == 4

    # The file gets damaged: one candle entry is corrupt
    bad_payload = json.loads(spill.read_text())
    bad_payload["candles"][1] = {"token": TOKEN, "garbage": True}
    spill.write_text(json.dumps(bad_payload))
    damaged = spill.read_bytes()

    # ── Process 2: recovery fails; the file must be left exactly as is ──
    agg2 = CandleAggregator(
        db=fake_db, interval_min=5, token_to_stock_id={TOKEN: STOCK_ID}, spill_path=spill
    )
    assert agg2.start() is None
    assert agg2.pending_candles == 0                   # nothing partially loaded
    assert agg2.stats["recovered_candles"] == 0
    assert agg2.stats["blocked_spill_files"] == [str(spill)]
    assert spill.read_bytes() == damaged

    # A successful flush of new candles must not delete/rewrite the file
    for tick in parse_feed(ws_manager, clock):
        agg2.on_tick(tick)
    assert agg2.flush_all() == 2
    assert spill.read_bytes() == damaged

    # stop() with nothing pending: file untouched, failure still reported
    assert agg2.stop() == 0
    assert spill.read_bytes() == damaged
    assert agg2.stats["blocked_spill_files"] == [str(spill)]
    alert = startup.candle_close_alert(0, agg2.stats)
    assert alert is not None and str(spill) in alert and "could not be loaded" in alert

    # ── Process 3: new unmapped candles must still be persisted somewhere
    #    else, without touching the blocked file ──
    agg3 = CandleAggregator(
        db=fake_db, interval_min=5, token_to_stock_id={TOKEN: STOCK_ID}, spill_path=spill
    )
    agg3.start()
    feed_two_tokens(ws_manager, agg3, clock)
    assert agg3.stop() == 2                            # 2 TCS candles pending
    assert spill.read_bytes() == damaged
    assert agg3.stats["spilled_candles"] == 2
    overflow = Path(agg3.stats["spill_path"])
    assert overflow != spill and overflow.exists()
    assert overflow.parent == spill.parent and overflow.name.startswith(spill.name)

    # ── Process 4: overflow file recovers normally; damaged file stays ──
    agg4 = CandleAggregator(
        db=fake_db, interval_min=5,
        token_to_stock_id={TOKEN: STOCK_ID, TOKEN_B: 7}, spill_path=spill,
    )
    agg4.start()
    assert agg4.pending_candles == 2
    assert agg4.stats["blocked_spill_files"] == [str(spill)]
    assert agg4.stop() == 0
    assert not overflow.exists()
    assert spill.read_bytes() == damaged

    tcs = rows_by_timestamp([r for r in flaky_store.rows() if r[0] == 7])
    assert tcs[TS_09_15]["volume"] == 5_700 and tcs[TS_09_20]["volume"] == 50


# ── reset_daily() vs. a concurrent on_tick() ─────────────────


class PausableCandleStore(FakeCandleStore):
    """CandleStore double whose insert_batch blocks until released, so a
    test can inject ticks while a flush is in progress."""

    import threading as _threading

    entered = _threading.Event()
    release = _threading.Event()

    def insert_batch(self, interval, rows):
        type(self).entered.set()
        assert type(self).release.wait(timeout=5), "flush was never released"
        return super().insert_batch(interval, rows)

    @classmethod
    def reset(cls):
        super().reset()
        cls.entered = cls._threading.Event()
        cls.release = cls._threading.Event()


@pytest.fixture
def pausable_store(monkeypatch):
    PausableCandleStore.reset()
    monkeypatch.setattr("storage.models.CandleStore", PausableCandleStore)
    return PausableCandleStore


def test_tick_arriving_during_reset_daily_flush_is_not_lost(
    ws_manager, aggregator, clock, pausable_store
):
    """While reset_daily() is blocked in the DB write, the first tick of the
    new day arrives. The reset must not wipe that bucket or its volume
    counter afterwards."""
    import threading

    for tick in parse_feed(ws_manager, clock):        # day 1: 09:15 done, 09:20 open
        aggregator.on_tick(tick)
    assert aggregator.active_buckets == 1

    reset_result = []
    reset_thread = threading.Thread(target=lambda: reset_result.append(aggregator.reset_daily()))
    reset_thread.start()
    assert pausable_store.entered.wait(timeout=5)     # reset is inside insert_batch

    # Day-2 opening tick lands mid-reset
    day2_open = DAY2_OPEN.replace(second=5)
    (tick,) = parse_feed(ws_manager, clock, [(day2_open, 10_500, 400)])
    aggregator.on_tick(tick)

    pausable_store.release.set()
    reset_thread.join(timeout=5)
    assert not reset_thread.is_alive()
    assert reset_result == [2]                         # day-1 candles written

    # The mid-reset tick survived as the day-2 opening bucket ...
    assert aggregator.active_buckets == 1
    bucket = aggregator._buckets[TOKEN]
    assert bucket.interval_start == DAY2_OPEN
    assert bucket.open == bucket.close == 105.0
    assert bucket.volume == 400                        # opening reading counts in full
    assert bucket.tick_count == 1
    # ... and so did its volume counter
    assert aggregator._last_cum_volume == {TOKEN: 400}

    # Next tick closes the opening candle with only the increment
    (tick,) = parse_feed(ws_manager, clock, [(DAY2_0920, 10_600, 450)])
    aggregator.on_tick(tick)
    pausable_store.release.set()                       # keep the store unblocked
    assert aggregator._do_flush() == 1                 # background-loop path
    candles = rows_by_timestamp(pausable_store.rows())
    assert set(candles) == {TS_09_15, TS_09_20, DAY2_OPEN}
    assert candles[DAY2_OPEN]["open"] == 105.0
    assert candles[DAY2_OPEN]["volume"] == 400
    assert candles[DAY2_OPEN]["vwap"] == 105.0
    assert aggregator._buckets[TOKEN].volume == 50     # 450 - 400, open 09:20 bucket


# ── Flow review: warm-up must map tokens of recovered candles ────


def test_warmup_maps_tokens_of_recovered_candles_dropped_from_watchlist(
    ws_manager, clock, flaky_store, fake_db, tmp_path
):
    """Day 1: RELIANCE and TCS candles fail to write and are spilled. Day 2:
    TCS is no longer on the watchlist. Warm-up must still resolve TCS so
    the recovered candles can be saved instead of being held forever."""
    spill = tmp_path / "candle_spill_5m.json"

    agg1 = CandleAggregator(
        db=fake_db, interval_min=5,
        token_to_stock_id={TOKEN: STOCK_ID, TOKEN_B: 7}, spill_path=spill,
    )
    feed_two_tokens(ws_manager, agg1, clock)
    assert agg1.stop() == 4                            # write failed → spilled

    # Day 2 boot: aggregator constructed, warm-up runs before start()
    agg2 = CandleAggregator(db=fake_db, interval_min=5, spill_path=spill)
    stock_store = FakeStockStore({TOKEN: STOCK_ID, TOKEN_B: 7})
    tokens = startup.prepare_live_subscription(
        watchlist_manager=FakeWatchlistManager([TOKEN]),   # TCS dropped
        instruments=FakeInstrumentLoader([]),
        stock_store=stock_store,
        candle_aggregator=agg2,
    )
    assert tokens == [TOKEN]                           # subscription unchanged
    assert sorted(stock_store.batches[-1]) == sorted([TOKEN, TOKEN_B])
    assert agg2._token_to_stock_id == {TOKEN: STOCK_ID, TOKEN_B: 7}

    agg2.start()
    assert agg2.stop() == 0
    saved = [(row[0], row[1]) for row in flaky_store.rows()]
    assert sorted(saved) == sorted([
        (STOCK_ID, TS_09_15), (STOCK_ID, TS_09_20), (7, TS_09_15), (7, TS_09_20),
    ])
    assert not spill.exists()


# ── Flow review: signal shutdown must flush / spill like market close ──


def test_signal_shutdown_stops_candle_aggregator(ws_manager, clock, flaky_store, fake_db, tmp_path):
    """main.shutdown_live_components() is what the SIGINT/SIGTERM handler
    calls; it must stop the aggregator so pending candles are flushed or
    spilled instead of vanishing with the process."""
    spill = tmp_path / "candle_spill_5m.json"
    aggregator = CandleAggregator(
        db=fake_db, interval_min=5, token_to_stock_id={TOKEN: STOCK_ID}, spill_path=spill
    )
    feed_two_tokens(ws_manager, aggregator, clock)

    class FakeWriter:
        stopped = False
        def stop(self):
            self.stopped = True

    alerts = []

    class FakeNotifier:
        def system_alert(self, title, message):
            alerts.append((title, message))

    writer = FakeWriter()
    pending = startup.shutdown_live_components(
        candle_aggregator=aggregator, tick_writer=writer, notifier=FakeNotifier()
    )

    assert writer.stopped
    assert pending == 4                                # write failed → spilled, not lost
    assert spill.exists()
    assert any("4 five-minute candle(s)" in msg for _, msg in alerts)
