"""
Offline regression tests for WebSocketManager reconnect / resubscribe ownership.

The fake below reproduces the behaviour of the SDK the project actually
installs — verified by driving the real classes' callbacks offline:

    smartapi-python 1.5.5   SmartApi/smartWebSocketV2.py
    websocket-client 1.9.2  websocket/_app.py  (pinned by requirements.txt;
                            the SDK does not declare it)

Facts reproduced here:

  * ``connect()`` blocks in ``WebSocketApp.run_forever()`` and RETURNS
    NORMALLY on handshake refusal, transport error and clean close alike.
    run_forever swallows the exception, calls ``on_error``, then
    ``teardown()`` -> ``on_close(wsapp, code, msg)``.
  * ``SmartWebSocketV2._on_close(self, wsapp)`` cannot accept those three
    arguments -> TypeError inside ``WebSocketApp._callback`` -> forwarded to
    ``on_error(wsapp, TypeError)``. The user-level ``on_close`` hook is
    therefore never invoked; every closure surfaces as an error.
  * ``SmartWebSocketV2._on_error`` is the SDK's own retry loop: while
    ``current_retry_attempt < max_retry_attempt`` (default 1) it
    ``time.sleep(retry_delay=10)`` inside the callback, ``close_connection()``s
    and calls ``connect()`` recursively. Once exhausted it calls
    ``on_error("Max retry attempt reached", "Connection closed")``.
  * ``_on_open`` calls ``resubscribe()`` instead of ``on_open`` whenever
    ``RESUBSCRIBE_FLAG`` is set. ``resubscribe()`` replays
    ``input_request_dict``, which ``unsubscribe()`` never prunes — a replay
    restores tokens the caller cancelled.

Every test runs the manager's real ``start()`` / ``_run()`` / ``_connect()``
path; only the SDK class, the session, the audit trail and the backoff
sleep are substituted.
"""

from __future__ import annotations

import sys
import threading
import time
import types

import pytest

from core.constants import AuditEventType
from data.streams.websocket_manager import WebSocketManager
from tests.data.ws_fakes import FakeAudit, FakeSession, FakeSmartWebSocketV2, wait_until


TOKEN = "2885"
TOKEN_B = "11536"
TOKEN_C = "1594"


@pytest.fixture
def sdk(monkeypatch):
    """Serve the fake wherever the manager imports SmartWebSocketV2 from."""
    FakeSmartWebSocketV2.reset([])
    module = types.ModuleType("SmartApi.smartWebSocketV2")
    module.SmartWebSocketV2 = FakeSmartWebSocketV2
    package = types.ModuleType("SmartApi")
    package.smartWebSocketV2 = module
    monkeypatch.setitem(sys.modules, "SmartApi", package)
    monkeypatch.setitem(sys.modules, "SmartApi.smartWebSocketV2", module)
    return FakeSmartWebSocketV2


@pytest.fixture
def audit():
    return FakeAudit()


@pytest.fixture
def ws_manager(sdk, audit):
    mgr = WebSocketManager(session=FakeSession(), audit=audit)
    yield mgr
    mgr.stop()
    if mgr._ws_thread is not None:
        mgr._ws_thread.join(timeout=5)
        assert not mgr._ws_thread.is_alive(), "ws thread did not stop"


@pytest.fixture
def backoff(ws_manager, monkeypatch):
    """Record the manager's backoff waits instead of sleeping. ``gate`` lets a
    test hold the manager in the disconnected state between attempts."""
    class Backoff:
        delays: list[float] = []
        gate = threading.Event()
        gate.set()

    def fake_wait(delay, stop_event=None):
        Backoff.delays.append(delay)
        Backoff.gate.wait(timeout=5)

    monkeypatch.setattr(ws_manager, "_backoff_wait", fake_wait, raising=False)
    return Backoff


def run_to_completion(ws_manager, timeout: float = 5.0):
    ws_manager.start()
    ws_manager._ws_thread.join(timeout=timeout)
    assert not ws_manager._ws_thread.is_alive(), "manager loop did not finish"


# ── Ownership: the SDK's own retry and replay must be off ────


def test_sdk_internal_retry_is_disabled_and_each_attempt_uses_a_fresh_client(
    ws_manager, sdk, backoff
):
    """Two retry loops must not compete. The manager owns reconnect, so the
    SDK is constructed with its retry off, never sleeps inside a callback,
    never reconnects an instance itself, and resubscribe() is unreachable."""
    sdk.reset(["server_close", "server_close", "stay_open"])
    ws_manager.subscribe([TOKEN])

    ws_manager.start()
    assert wait_until(lambda: len(sdk.instances) == 3 and ws_manager.is_synced)

    for client in sdk.instances:
        assert client.kwargs["max_retry_attempt"] == 0
        assert client.sdk_sleeps == []               # SDK loop never ran
        assert client.connect_calls == 1             # one connect per instance
        assert client.replayed == []                 # manager did the resubscribe
    assert [c.frames for c in sdk.instances] == [[("subscribe", [TOKEN])]] * 3


# ── Bounded recovery with backoff ────────────────────────────


def test_clean_server_close_is_recovered_with_backoff(ws_manager, sdk, backoff):
    """connect() returns normally on a clean close; that is not success and
    must not produce an immediate reconnect. Each attempt backs off."""
    sdk.reset(["server_close", "server_close", "stay_open"])
    ws_manager.subscribe([TOKEN])

    ws_manager.start()
    # the first two sessions open briefly, so wait for the third client
    assert wait_until(lambda: len(sdk.instances) == 3 and ws_manager.is_synced)

    assert backoff.delays == [1.0, 2.0]
    assert ws_manager.stats["connections_opened"] == 3
    assert ws_manager.stats["state"] == "connected"


def test_transport_error_is_recovered_with_backoff(ws_manager, sdk, backoff):
    sdk.reset(["transport_error", "transport_error", "stay_open"])
    ws_manager.subscribe([TOKEN])

    ws_manager.start()
    assert wait_until(lambda: len(sdk.instances) == 3 and ws_manager.is_synced)

    assert backoff.delays == [1.0, 2.0]
    assert ws_manager.stats["state"] == "connected"
    assert "Connection closed" in ws_manager.stats["last_disconnect_reason"]


def test_handshake_refusals_are_bounded_and_exhaustion_leaves_status_accurate(
    ws_manager, sdk, audit, backoff
):
    """Repeated refusals: MAX_RECONNECT_ATTEMPTS retries with doubling
    delay, then the loop gives up. Afterwards nothing may claim to be
    running, connected or sent; the caller's request survives."""
    attempts = WebSocketManager.MAX_RECONNECT_ATTEMPTS
    sdk.reset(["handshake_refused"] * (attempts + 1) + ["stay_open"] * 3)
    ws_manager.subscribe([TOKEN, TOKEN_B])

    run_to_completion(ws_manager)

    assert backoff.delays == [1.0, 2.0, 4.0, 8.0, 16.0][:attempts]
    assert len(sdk.instances) == attempts + 1          # first try + retries, no more
    stats = ws_manager.stats
    assert stats["state"] == "exhausted"
    assert stats["is_running"] is False
    assert stats["is_connected"] is False
    assert stats["reconnect_exhausted"] is True
    assert stats["reconnect_count"] == attempts
    assert stats["connections_opened"] == 0
    assert stats["sent_tokens"] == 0
    assert stats["requested_tokens"] == 2              # intent is not lost
    assert any(
        et == AuditEventType.ERROR and p.get("component") == "websocket"
        for et, p in audit.events
    )


def test_start_after_exhaustion_resumes_with_requested_set(ws_manager, sdk, backoff):
    sdk.reset(["handshake_refused"] * (WebSocketManager.MAX_RECONNECT_ATTEMPTS + 1))
    ws_manager.subscribe([TOKEN])
    run_to_completion(ws_manager)
    assert ws_manager.stats["state"] == "exhausted"

    sdk.reset(["stay_open"])
    ws_manager.start()
    assert wait_until(lambda: ws_manager.is_synced)

    assert ws_manager.stats["state"] == "connected"
    assert ws_manager.stats["reconnect_exhausted"] is False
    assert ws_manager.stats["reconnect_count"] == 0
    assert sdk.instances[-1].frames == [("subscribe", [TOKEN])]


def test_stop_during_backoff_returns_promptly(ws_manager, sdk, monkeypatch):
    """A real (un-patched) backoff wait must be interruptible by stop()."""
    monkeypatch.setattr(WebSocketManager, "INITIAL_RECONNECT_DELAY", 30.0)
    sdk.reset(["server_close", "stay_open"])

    ws_manager.start()
    assert wait_until(lambda: ws_manager.stats["reconnect_count"] == 1)
    assert not ws_manager.is_connected

    t0 = time.monotonic()
    ws_manager.stop()
    ws_manager._ws_thread.join(timeout=5)
    assert not ws_manager._ws_thread.is_alive()
    assert time.monotonic() - t0 < 2.0
    assert ws_manager.stats["state"] == "stopped"


# ── Replacement connection gets only the latest requested set ─


def test_replacement_connection_receives_only_latest_requested_subscriptions(
    ws_manager, sdk, backoff
):
    """Live on connection 1 with {A, B}. The server drops it. While the
    manager is backing off the caller cancels A and requests C. Connection 2
    must receive exactly {B, C}: no replay of A from the SDK's
    input_request_dict, no double send, nothing from the old connection."""
    sdk.reset(["stay_open", "stay_open"])
    ws_manager.subscribe([TOKEN, TOKEN_B])
    ws_manager.start()
    assert wait_until(lambda: ws_manager.is_synced)
    first = sdk.instances[0]
    assert first.frames == [("subscribe", [TOKEN_B, TOKEN])]

    backoff.gate.clear()                              # hold the manager in backoff
    first.drop("server_close")
    assert wait_until(lambda: backoff.delays == [1.0])
    assert not ws_manager.is_connected
    assert ws_manager.sent_count == 0

    ws_manager.unsubscribe([TOKEN])                   # cancel A while disconnected
    ws_manager.subscribe([TOKEN_C])                   # request C while disconnected
    assert first.frames == [("subscribe", [TOKEN_B, TOKEN])]   # dead socket untouched
    assert TOKEN in first.input_request_dict[2][1]    # SDK would still replay A ...

    backoff.gate.set()
    assert wait_until(lambda: len(sdk.instances) == 2 and ws_manager.is_synced)
    second = sdk.instances[1]

    assert second is not first                        # ... but it never gets the chance
    assert second.frames == [("subscribe", [TOKEN_B, TOKEN_C])]
    assert second.replayed == [] and first.replayed == []
    assert first.connect_calls == 1
    assert ws_manager.sent_count == 2
    assert ws_manager.pending_changes == set()


# ── Session expiry: bounded wait, recovery, interruptible ────


class MutableSession(FakeSession):
    def __init__(self, feed_token=None):
        self.feed_token = feed_token


@pytest.fixture
def clock(ws_manager, monkeypatch):
    class Clock:
        now = 1_700_000_000.0
        def __call__(self):
            return self.now
        def advance(self, s):
            self.now += s
    clk = Clock()
    monkeypatch.setattr(ws_manager, "_clock", clk, raising=False)
    return clk


@pytest.fixture
def session_backoff(ws_manager, clock, monkeypatch):
    """Record waits and let time pass on the controlled clock instead of
    sleeping. In stepped mode each wait blocks until ``step()`` releases
    exactly one cycle."""
    class Backoff:
        delays: list[float] = []
        stepped = False
        steps = threading.Semaphore(0)

        @classmethod
        def step(cls):
            cls.steps.release()

    def fake_wait(delay, stop_event=None):
        Backoff.delays.append(delay)
        clock.advance(delay)
        if Backoff.stepped:
            assert Backoff.steps.acquire(timeout=5), "wait never released"

    monkeypatch.setattr(ws_manager, "_backoff_wait", fake_wait, raising=False)
    return Backoff


def test_session_expired_wait_is_bounded_and_reports_exhausted(
    ws_manager, sdk, audit, clock, session_backoff
):
    """Credentials never appear. Waiting must stop after
    MAX_SESSION_EXPIRED_WAIT with a clear 'exhausted' outcome, without
    ever touching the socket budget."""
    ws_manager._session = MutableSession(feed_token=None)
    sdk.reset(["stay_open"])
    ws_manager.subscribe([TOKEN])

    run_to_completion(ws_manager)

    limit = WebSocketManager.MAX_SESSION_EXPIRED_WAIT
    expected_waits = -(-limit // WebSocketManager.SESSION_EXPIRED_WAIT)   # ceil
    assert session_backoff.delays == [WebSocketManager.SESSION_EXPIRED_WAIT] * int(expected_waits)
    stats = ws_manager.stats
    assert stats["state"] == "exhausted"
    assert stats["is_running"] is False
    assert stats["reconnect_exhausted"] is True
    assert "session expired" in stats["last_disconnect_reason"]
    assert stats["reconnect_count"] == 0                # socket budget untouched
    assert len(sdk.instances) == 0                      # never built a client
    assert stats["requested_tokens"] == 1
    assert any(
        et == AuditEventType.ERROR and "session" in p.get("error", "")
        for et, p in audit.events
    )


def test_session_expired_recovers_when_credentials_become_available(
    ws_manager, sdk, clock, session_backoff
):
    session = MutableSession(feed_token=None)
    ws_manager._session = session
    sdk.reset(["stay_open"])
    ws_manager.subscribe([TOKEN])
    session_backoff.stepped = True

    ws_manager.start()
    assert wait_until(lambda: len(session_backoff.delays) == 1)
    assert ws_manager.stats["state"] == "waiting_for_session"
    assert ws_manager.stats["session_wait_s"] == 0
    session_backoff.step()                              # one more wait cycle ...
    assert wait_until(lambda: len(session_backoff.delays) == 2)
    assert ws_manager.stats["session_wait_s"] == WebSocketManager.SESSION_EXPIRED_WAIT
    session.feed_token = "feed"                         # ... then re-auth completes
    session_backoff.step()

    assert wait_until(lambda: ws_manager.is_synced)
    assert ws_manager.stats["state"] == "connected"
    assert ws_manager.stats["reconnect_count"] == 0
    assert ws_manager.stats["session_wait_s"] == 0
    assert sdk.instances[0].frames == [("subscribe", [TOKEN])]


def test_stop_interrupts_session_expired_wait(ws_manager, sdk, monkeypatch):
    """Un-patched wait: stop() must wake it immediately."""
    monkeypatch.setattr(WebSocketManager, "SESSION_EXPIRED_WAIT", 30.0)
    ws_manager._session = MutableSession(feed_token=None)

    ws_manager.start()
    assert wait_until(lambda: ws_manager.stats["state"] == "waiting_for_session")

    t0 = time.monotonic()
    ws_manager.stop()
    ws_manager._ws_thread.join(timeout=5)
    assert not ws_manager._ws_thread.is_alive()
    assert time.monotonic() - t0 < 2.0
    assert ws_manager.stats["state"] == "stopped"
    assert len(sdk.instances) == 0
