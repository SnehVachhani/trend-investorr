"""
Offline regression tests: callbacks and stop/start across connection
generations.

Every SDK client the manager builds is one *socket generation*; every
start() is one *run*. Only the current generation may change connection
state or deliver ticks, and only the current run may drive recovery.
Callbacks from a retired socket can arrive late (the SDK's close and error
hooks fire from the socket thread during teardown), and a retired run's
thread can still be finishing its connect() when a new run begins.

Uses the SDK-faithful fake from tests/data/ws_fakes.py and the manager's
real start()/_run()/_connect() path.
"""

from __future__ import annotations

import sys
import threading
import types

import pytest

from data.streams.websocket_manager import WebSocketManager
from tests.data.ws_fakes import FakeAudit, FakeSession, FakeSmartWebSocketV2, wait_until


TOKEN = "2885"
TOKEN_B = "11536"


def smartapi_message(token: str = TOKEN, ltp_paisa: int = 10_000) -> dict:
    return {"token": token, "name": "RELIANCE", "last_traded_price": ltp_paisa,
            "volume_trade_for_the_day": 1_000}


# ── Fixtures ─────────────────────────────────────────────────


@pytest.fixture
def sdk(monkeypatch):
    FakeSmartWebSocketV2.reset([])
    module = types.ModuleType("SmartApi.smartWebSocketV2")
    module.SmartWebSocketV2 = FakeSmartWebSocketV2
    package = types.ModuleType("SmartApi")
    package.smartWebSocketV2 = module
    monkeypatch.setitem(sys.modules, "SmartApi", package)
    monkeypatch.setitem(sys.modules, "SmartApi.smartWebSocketV2", module)
    return FakeSmartWebSocketV2


@pytest.fixture
def ws_manager(sdk, monkeypatch):
    monkeypatch.setattr(WebSocketManager, "STOP_JOIN_TIMEOUT", 0.2, raising=False)
    mgr = WebSocketManager(session=FakeSession(), audit=FakeAudit())
    yield mgr
    for client in sdk.instances:          # never leave a session blocked
        client.release_close()
    mgr.stop()
    if mgr._ws_thread is not None:
        mgr._ws_thread.join(timeout=5)


@pytest.fixture
def backoff(ws_manager, monkeypatch):
    delays: list[float] = []
    gate = threading.Event()
    gate.set()

    def fake_wait(delay, stop_event=None):
        delays.append(delay)
        gate.wait(timeout=5)

    monkeypatch.setattr(ws_manager, "_backoff_wait", fake_wait, raising=False)
    return types.SimpleNamespace(delays=delays, gate=gate)


def connect_then_replace(ws_manager, sdk, backoff):
    """Connection 1 comes up, the server drops it, connection 2 comes up.
    Returns (first, second) SDK clients; first is retired, second current."""
    sdk.reset(["stay_open", "stay_open"])
    ws_manager.subscribe([TOKEN])
    ws_manager.start()
    assert wait_until(lambda: len(sdk.instances) == 1 and ws_manager.is_synced)
    first = sdk.instances[0]
    first.drop("server_close")
    assert wait_until(lambda: len(sdk.instances) == 2 and ws_manager.is_synced)
    second = sdk.instances[1]
    assert first.frames == [("subscribe", [TOKEN])]
    assert second.frames == [("subscribe", [TOKEN])]
    assert ws_manager.sent_count == 1
    return first, second


# ── Delayed callbacks from a retired socket ──────────────────


def test_delayed_close_and_error_from_old_socket_do_not_touch_replacement(
    ws_manager, sdk, backoff
):
    first, second = connect_then_replace(ws_manager, sdk, backoff)

    first.on_close(first.wsapp)                                   # SDK declared form
    first.on_close(first.wsapp, 1006, "abnormal")                 # websocket-client form
    first.on_error("Max retry attempt reached", "Connection closed")
    first.on_error(first.wsapp, ConnectionResetError("late"))

    assert ws_manager.is_connected
    assert ws_manager.sent_count == 1                             # sent state intact
    assert ws_manager.pending_changes == set()
    assert ws_manager.stats["state"] == "connected"
    assert ws_manager.stats["stale_callbacks_ignored"] == 4
    assert len(sdk.instances) == 2                                # no reconnect triggered


def test_delayed_open_from_old_socket_cannot_mark_connected_or_resend(
    ws_manager, sdk, backoff
):
    first, second = connect_then_replace(ws_manager, sdk, backoff)

    first.on_open(first.wsapp)

    assert first.frames == [("subscribe", [TOKEN])]               # nothing new on old
    assert second.frames == [("subscribe", [TOKEN])]              # nothing doubled on new
    assert ws_manager.stats["connections_opened"] == 2
    assert ws_manager.sent_count == 1
    assert ws_manager.stats["stale_callbacks_ignored"] == 1


def test_stale_ticks_from_old_socket_are_dropped_and_current_ones_delivered(
    ws_manager, sdk, backoff
):
    first, second = connect_then_replace(ws_manager, sdk, backoff)
    delivered = []
    ws_manager.on_tick(delivered.append)

    first.on_data(first.wsapp, smartapi_message(ltp_paisa=9_000))     # stale
    assert delivered == []
    assert ws_manager.stats["ticks_received"] == 0
    assert ws_manager.stats["stale_ticks_dropped"] == 1
    assert ws_manager.get_latest_tick(TOKEN) is None

    second.on_data(second.wsapp, smartapi_message(ltp_paisa=10_100))  # current
    assert [t.ltp for t in delivered] == [101.0]
    assert ws_manager.stats["ticks_received"] == 1
    assert ws_manager.get_latest_tick(TOKEN).ltp == 101.0


# ── stop() / start() while the old attempt is still finishing ─


def test_stop_then_start_while_old_connect_is_still_finishing(ws_manager, sdk, backoff):
    """stop() asks connection 1 to close but its run_forever has not
    returned yet. start() must create run 2 / connection 2 immediately.
    When connection 1 finally ends, the old run's cleanup must neither
    disconnect connection 2, wipe its sent state, nor resume looping."""
    sdk.reset(["stay_open", "stay_open"])
    ws_manager.subscribe([TOKEN])
    ws_manager.start()
    assert wait_until(lambda: ws_manager.is_synced)
    first = sdk.instances[0]
    first.close_blocks = True
    old_thread = ws_manager._ws_thread

    ws_manager.stop()                                             # join times out
    assert first.close_requested.is_set()
    assert old_thread.is_alive()                                  # attempt still finishing
    assert ws_manager.stats["state"] == "stopped"
    assert not ws_manager.is_connected

    ws_manager.subscribe([TOKEN_B])                               # change while stopped
    ws_manager.start()
    assert wait_until(lambda: len(sdk.instances) == 2 and ws_manager.is_synced)
    second = sdk.instances[1]
    assert second.frames == [("subscribe", [TOKEN_B, TOKEN])]
    assert ws_manager.sent_count == 2
    assert ws_manager.stats["run_id"] == 2
    new_thread = ws_manager._ws_thread
    assert new_thread is not old_thread

    first.release_close()                                         # old attempt now ends
    assert wait_until(lambda: not old_thread.is_alive())

    assert ws_manager.is_connected                                # replacement untouched
    assert ws_manager.sent_count == 2
    assert ws_manager.pending_changes == set()
    assert ws_manager.stats["state"] == "connected"
    assert ws_manager.stats["reconnect_count"] == 0
    assert len(sdk.instances) == 2                                # old loop did not revive
    assert backoff.delays == []
    assert new_thread.is_alive()
    assert ws_manager._ws is second


def test_old_run_cannot_exhaust_or_stop_the_new_run(ws_manager, sdk, backoff):
    """Variant: the old attempt ends with an error after the restart. The
    old run must not count it against the new run's budget."""
    sdk.reset(["stay_open", "stay_open"])
    ws_manager.subscribe([TOKEN])
    ws_manager.start()
    assert wait_until(lambda: ws_manager.is_synced)
    first = sdk.instances[0]
    first.close_blocks = True
    ws_manager.stop()
    ws_manager.start()
    assert wait_until(lambda: len(sdk.instances) == 2 and ws_manager.is_synced)
    old_thread = [t for t in threading.enumerate() if t.name == "ws-stream" and t is not ws_manager._ws_thread]

    first.drop("transport_error")                                 # old session errors out
    first.release_close()
    assert wait_until(lambda: all(not t.is_alive() for t in old_thread))

    assert ws_manager.is_connected
    assert ws_manager.stats["reconnect_count"] == 0
    assert ws_manager.stats["reconnect_exhausted"] is False
    assert ws_manager.is_running
    assert backoff.delays == []


# ── Concurrent / repeated start() ────────────────────────────


def test_concurrent_start_calls_spawn_exactly_one_run(ws_manager, sdk, backoff):
    sdk.reset(["stay_open"] * 8)
    ws_manager.subscribe([TOKEN])
    n = 8
    barrier = threading.Barrier(n)

    def call_start():
        barrier.wait()
        ws_manager.start()

    threads = [threading.Thread(target=call_start) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert wait_until(lambda: ws_manager.is_synced)

    assert [t.name for t in threading.enumerate()].count("ws-stream") == 1
    assert len(sdk.instances) == 1
    assert ws_manager.stats["run_id"] == 1
    assert sdk.instances[0].frames == [("subscribe", [TOKEN])]


def test_start_while_running_is_a_no_op_and_restart_after_stop_gets_new_run(
    ws_manager, sdk, backoff
):
    sdk.reset(["stay_open", "stay_open"])
    ws_manager.start()
    assert wait_until(lambda: ws_manager.is_synced)
    ws_manager.start()                                            # already running
    assert ws_manager.stats["run_id"] == 1
    assert len(sdk.instances) == 1

    ws_manager.stop()
    assert wait_until(lambda: not ws_manager._ws_thread.is_alive())
    ws_manager.start()
    assert wait_until(lambda: len(sdk.instances) == 2 and ws_manager.is_synced)
    assert ws_manager.stats["run_id"] == 2
    assert ws_manager.stats["socket_generation"] == 2


# ── Retired client that opens late must be closed ────────────


def test_retired_client_that_opens_late_is_closed_without_touching_replacement(
    ws_manager, sdk, backoff
):
    """Run 1 attaches client 1 but is descheduled before the SDK builds its
    WebSocketApp. stop() runs now: close_connection() is a no-op (nothing
    to close yet), the join times out, generation 1 is retired. start()
    brings up client 2. Then client 1 finally opens. Ignoring its on_open
    is not enough — it would stay connected beside client 2, holding a
    broker slot. It must be closed, and client 2 must not notice."""
    gate1 = threading.Event()
    sdk.reset(["stay_open", "stay_open"])
    sdk.connect_gates.append(gate1)
    ws_manager.subscribe([TOKEN])
    ws_manager.start()
    assert wait_until(lambda: len(sdk.instances) == 1 and sdk.instances[0].connect_calls == 1)
    first = sdk.instances[0]
    old_thread = ws_manager._ws_thread
    assert first.wsapp is None                                    # held before WebSocketApp

    ws_manager.stop()
    assert first.close_calls >= 1 and first.wsapp is None         # closes were no-ops
    assert old_thread.is_alive()

    ws_manager.start()
    assert wait_until(lambda: len(sdk.instances) == 2 and ws_manager.is_synced)
    second = sdk.instances[1]
    assert second.frames == [("subscribe", [TOKEN])]

    gate1.set()                                                   # client 1 opens now
    assert wait_until(lambda: not old_thread.is_alive())

    assert first.closed_while_open                                # it was live, and got closed
    assert not first._open
    assert first.frames == []                                     # never subscribed
    assert ws_manager.stats["stale_callbacks_ignored"] >= 1
    # replacement untouched
    assert ws_manager.is_connected and ws_manager.is_synced
    assert ws_manager._ws is second and second._open
    assert second.frames == [("subscribe", [TOKEN])]
    assert ws_manager.sent_count == 1
    assert ws_manager.stats["reconnect_count"] == 0
    assert len(sdk.instances) == 2


# ── Tick in flight across retirement ─────────────────────────


def test_tick_paused_after_generation_check_is_not_published_after_replacement(
    ws_manager, sdk, backoff
):
    """on_data from socket 1 passes the generation check, then the socket
    thread is descheduled inside parsing. Meanwhile socket 1 is dropped and
    socket 2 is current. When parsing resumes, the tick must not reach the
    buffer or the callbacks: publication itself is guarded, not just entry."""
    sdk.reset(["stay_open", "stay_open"])
    ws_manager.subscribe([TOKEN])
    ws_manager.start()
    assert wait_until(lambda: len(sdk.instances) == 1 and ws_manager.is_synced)
    first = sdk.instances[0]
    delivered = []
    ws_manager.on_tick(delivered.append)

    paused, resume = threading.Event(), threading.Event()
    real_parse = ws_manager._parse_tick

    def slow_parse(message):
        tick = real_parse(message)
        if not paused.is_set():                                   # first tick only
            paused.set()
            assert resume.wait(timeout=5)
        return tick

    ws_manager._parse_tick = slow_parse
    socket_thread = threading.Thread(
        target=first.on_data, args=(first.wsapp, smartapi_message(ltp_paisa=9_000))
    )
    socket_thread.start()
    assert paused.wait(timeout=5)                                 # passed the entry check

    first.drop("server_close")                                    # retire socket 1 ...
    assert wait_until(lambda: len(sdk.instances) == 2 and ws_manager.is_synced)
    second = sdk.instances[1]

    resume.set()                                                  # ... then parsing resumes
    socket_thread.join(timeout=5)
    assert not socket_thread.is_alive()

    assert delivered == []
    assert ws_manager.stats["ticks_received"] == 0
    assert ws_manager.stats["stale_ticks_dropped"] == 1
    assert ws_manager.get_latest_tick(TOKEN) is None

    second.on_data(second.wsapp, smartapi_message(ltp_paisa=10_100))
    assert [t.ltp for t in delivered] == [101.0]


# ── Waiting for subscriptions to be sent is explicit ─────────


def test_is_synced_is_true_only_once_subscriptions_are_sent(ws_manager, sdk, backoff):
    """is_connected flips when the socket opens, *before* the requested set
    is sent. Tests (and readiness checks) that inspect what was sent must
    wait on is_synced, which is True only when the socket matches the
    requested set."""
    sdk.reset(["stay_open"])
    sdk.subscribe_delay = 0.1                                     # sending takes a while
    ws_manager.subscribe([TOKEN, TOKEN_B])

    ws_manager.start()
    assert wait_until(lambda: ws_manager.is_connected)
    client = sdk.instances[0]
    frames_at_connected = list(client.frames)

    assert wait_until(lambda: ws_manager.is_synced)
    assert client.frames == [("subscribe", [TOKEN_B, TOKEN])]
    assert frames_at_connected == []                              # connected != sent
    assert ws_manager.stats["is_synced"] is True
