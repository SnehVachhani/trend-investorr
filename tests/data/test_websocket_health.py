"""
Offline regression tests: connection health monitoring and periodic retry
of pending subscription changes.

Heartbeat evidence available from the installed stack (smartapi-python
1.5.5 / websocket-client 1.9.2):

  * websocket-client sends a ping frame every HEART_BEAT_INTERVAL (10 s)
    but the SDK passes no ``ping_timeout``, so a dead connection never
    produces a close callback — the socket thread just blocks in recv.
  * websocket-client records ``wsapp.last_pong_tm`` on every pong frame.
    The SDK's own ``last_pong_timestamp`` is never set in practice (its
    ``_on_pong`` only accepts a pong whose payload equals "ping", but the
    ping payload is empty).
  * Ticks arrive through ``on_data``.

Health = the most recent of {socket opened, last tick, last pong}. A quiet
market with healthy pongs is healthy; no pong and no tick for
HEARTBEAT_TIMEOUT is a silent failure.

Timing is controlled: ``clock`` replaces the manager's clock, and ``health``
replaces the monitor's periodic wait with an explicit pulse.
"""

from __future__ import annotations

import sys
import threading
import types

from datetime import datetime

import pytest

from core.constants import IST
from data.streams import websocket_manager
from data.streams.websocket_manager import WebSocketManager
from tests.data.ws_fakes import FakeAudit, FakeSession, FakeSmartWebSocketV2, wait_until


TOKEN = "2885"
TOKEN_B = "11536"
TOKEN_C = "1594"


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


class Clock:
    def __init__(self, start: float = 1_700_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    """One controlled timeline for everything the manager times: its
    ``_clock`` (epoch seconds, as websocket-client stamps pongs) and the
    ``datetime`` it uses for wall-clock stamps."""
    clk = Clock()

    class ControlledDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(clk.now, tz or IST)

    monkeypatch.setattr(websocket_manager, "datetime", ControlledDatetime)
    return clk


@pytest.fixture
def ws_manager(sdk, clock, monkeypatch):
    monkeypatch.setattr(WebSocketManager, "STOP_JOIN_TIMEOUT", 0.2, raising=False)
    mgr = WebSocketManager(session=FakeSession(), audit=FakeAudit())
    monkeypatch.setattr(mgr, "_clock", clock, raising=False)
    yield mgr
    for client in sdk.instances:
        client.release_close()
    mgr.stop()
    if mgr._ws_thread is not None:
        mgr._ws_thread.join(timeout=5)


@pytest.fixture
def backoff(ws_manager, monkeypatch):
    delays: list[float] = []

    def fake_wait(delay, stop_event=None):
        delays.append(delay)

    monkeypatch.setattr(ws_manager, "_backoff_wait", fake_wait, raising=False)
    return types.SimpleNamespace(delays=delays)


@pytest.fixture
def health(ws_manager, monkeypatch):
    """Replace the monitor's periodic wait with an explicit pulse. Each
    pulse() lets exactly one health check run; a stopped monitor returns
    immediately so the thread can exit."""
    pulses = threading.Semaphore(0)

    def fake_wait(stop_event, interval):
        while not pulses.acquire(timeout=0.02):
            if stop_event.is_set():
                return True                          # "stopped": exit loop
        return stop_event.is_set()

    monkeypatch.setattr(ws_manager, "_monitor_wait", fake_wait, raising=False)

    def pulse():
        pulses.release()

    return types.SimpleNamespace(pulse=pulse)


def monitor_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith("ws-health-")]


def connect(ws_manager, sdk, script=("stay_open",), tokens=(TOKEN,)):
    sdk.reset(list(script))
    ws_manager.subscribe(list(tokens))
    ws_manager.start()
    assert wait_until(lambda: len(sdk.instances) == 1 and ws_manager.is_synced)
    return sdk.instances[0]


# ── Silent failure ───────────────────────────────────────────


def test_silent_connection_is_detected_and_recovered_through_bounded_policy(
    ws_manager, sdk, clock, backoff, health
):
    """No close callback ever arrives. No tick, no pong for longer than
    HEARTBEAT_TIMEOUT: the monitor must retire and close the socket so the
    run loop recovers it like any other disconnect — with backoff."""
    first = connect(ws_manager, sdk, script=["stay_open", "stay_open"])
    assert len(monitor_threads()) == 1

    clock.advance(WebSocketManager.HEARTBEAT_TIMEOUT - 1)
    health.pulse()
    assert not wait_until(lambda: first.closed_while_open, timeout=0.2)  # not yet silent

    clock.advance(2)                                                    # now past the timeout
    health.pulse()
    assert wait_until(lambda: first.closed_while_open)
    assert wait_until(lambda: len(sdk.instances) == 2 and ws_manager.is_synced)

    assert "heartbeat" in ws_manager.stats["last_disconnect_reason"]
    assert ws_manager.stats["silent_failures"] == 1
    assert backoff.delays == [1.0]                                      # bounded policy
    assert ws_manager.stats["reconnect_count"] == 1
    assert sdk.instances[1].frames == [("subscribe", [TOKEN])]


# ── Quiet but healthy ────────────────────────────────────────


def test_quiet_connection_with_healthy_pongs_stays_connected(
    ws_manager, sdk, clock, backoff, health
):
    """No ticks at all (pre-market), but websocket-client keeps recording
    pongs every 10 s. Well past HEARTBEAT_TIMEOUT the connection must still
    be the same, open socket."""
    first = connect(ws_manager, sdk)
    ping_interval = 10.0
    steps = int(WebSocketManager.HEARTBEAT_TIMEOUT * 3 / ping_interval)

    for _ in range(steps):
        clock.advance(ping_interval)
        first.wsapp.last_pong_tm = clock.now                            # pong frame arrived
        health.pulse()
        assert not wait_until(lambda: first.closed_while_open, timeout=0.05)

    assert ws_manager.is_connected and ws_manager.is_synced
    assert first._open and not first.closed_while_open
    assert len(sdk.instances) == 1
    assert ws_manager.stats["silent_failures"] == 0
    assert backoff.delays == []
    assert ws_manager.stats["ticks_received"] == 0


def test_ticks_alone_also_count_as_evidence(ws_manager, sdk, clock, backoff, health):
    first = connect(ws_manager, sdk)
    for _ in range(5):
        clock.advance(WebSocketManager.HEARTBEAT_TIMEOUT - 1)
        first.on_data(first.wsapp, smartapi_message())                  # tick, no pong
        health.pulse()
        assert not wait_until(lambda: first.closed_while_open, timeout=0.05)
    assert ws_manager.is_connected and len(sdk.instances) == 1


# ── Periodic retry of pending subscription changes ───────────


def test_failed_subscription_is_retried_by_monitor_without_another_change(
    ws_manager, sdk, clock, backoff, health
):
    """The subscribe sent from on_open fails. Nobody calls subscribe() or
    unsubscribe() again and the connection stays up. The monitor must
    retry the pending change on its own and succeed."""
    sdk.subscribe_failures = 1
    sdk.reset(["stay_open"])
    sdk.subscribe_failures = 1
    ws_manager.subscribe([TOKEN])
    ws_manager.start()
    assert wait_until(lambda: len(sdk.instances) == 1 and ws_manager.is_connected)
    first = sdk.instances[0]
    assert wait_until(lambda: first.frames == [("subscribe", [TOKEN])])
    assert ws_manager.pending_changes == {TOKEN}
    assert not ws_manager.is_synced

    health.pulse()                                                      # healthy: retry

    assert wait_until(lambda: ws_manager.is_synced)
    assert first.frames == [("subscribe", [TOKEN]), ("subscribe", [TOKEN])]
    assert first.live == {TOKEN}
    assert ws_manager.stats["subscription_retries"] == 1
    assert len(sdk.instances) == 1                                      # no reconnect


def test_monitor_retry_respects_capacity_and_frees_slot_before_adding(
    ws_manager, sdk, clock, backoff, health, monkeypatch
):
    """Full connection; unsubscribe(A) fails; replacement Z requested and
    held back. The monitor's retry must unsubscribe A first and only then
    send Z — the wire never exceeds the limit."""
    monkeypatch.setattr(websocket_manager, "SMARTAPI_MAX_TOKENS_PER_CONNECTION", 3)
    first = connect(ws_manager, sdk, tokens=["A", "B", "C"])
    assert first.live == {"A", "B", "C"}

    sdk.unsubscribe_failures = 2                                        # fails now and on Z's sync
    ws_manager.unsubscribe(["A"])                                       # fails, A still live
    ws_manager.subscribe(["Z"])                                         # accepted, held back
    assert ws_manager.pending_changes == {"A", "Z"}
    assert first.live == {"A", "B", "C"}

    health.pulse()

    assert wait_until(lambda: ws_manager.is_synced)
    assert first.frames[-2:] == [("unsubscribe", ["A"]), ("subscribe", ["Z"])]
    assert first.live == {"B", "C", "Z"}
    assert first.peak_live <= 3


# ── Ownership: monitor belongs to the current run / generation ─


def test_monitor_stops_promptly_on_stop(ws_manager, sdk, clock, health):
    connect(ws_manager, sdk)
    assert len(monitor_threads()) == 1
    ws_manager.stop()
    assert wait_until(lambda: monitor_threads() == [], timeout=2)


def test_old_monitor_exits_on_replacement_and_cannot_close_the_new_socket(
    ws_manager, sdk, clock, backoff, health
):
    first = connect(ws_manager, sdk, script=["stay_open", "stay_open"])
    (old_monitor,) = monitor_threads()

    first.drop("server_close")
    assert wait_until(lambda: len(sdk.instances) == 2 and ws_manager.is_synced)
    second = sdk.instances[1]
    assert wait_until(lambda: not old_monitor.is_alive(), timeout=2)   # retired with gen 1
    new_monitors = [t for t in monitor_threads() if t.is_alive()]
    assert len(new_monitors) == 1 and new_monitors[0] is not old_monitor

    # Even if a retired generation's check ran now, past the timeout, it
    # must neither close nor reconnect anything.
    clock.advance(WebSocketManager.HEARTBEAT_TIMEOUT * 2)
    before = (ws_manager.stats["last_disconnect_reason"], list(backoff.delays))
    assert ws_manager._health_check(gen=1, client=first) is False

    assert ws_manager.is_connected and ws_manager._ws is second and second._open
    assert not second.closed_while_open
    assert (ws_manager.stats["last_disconnect_reason"], backoff.delays) == before
    assert len(sdk.instances) == 2

    # The current generation's check, on the other hand, sees the silence.
    second.wsapp.last_pong_tm = clock.now                               # keep it healthy
    assert ws_manager._health_check(gen=2, client=second) is True
    assert second._open


# ── Bounded recovery vs. silent failures ─────────────────────


def silent_cycle(ws_manager, sdk, clock, health, index: int):
    """Connection ``index`` (1-based) comes up, produces nothing, and is
    declared silent by the monitor."""
    assert wait_until(lambda: len(sdk.instances) == index and ws_manager.is_synced)
    client = sdk.instances[index - 1]
    clock.advance(WebSocketManager.HEARTBEAT_TIMEOUT + 1)
    health.pulse()
    assert wait_until(lambda: client.closed_while_open)
    return client


def test_repeated_silent_connections_exhaust_recovery(
    ws_manager, sdk, clock, backoff, health
):
    """Each silent connection lives HEARTBEAT_TIMEOUT (45 s) before it is
    closed — longer than STABLE_CONNECTION_SECONDS (30 s). Uptime alone must
    not reset the budget: a connection that never produced a tick or pong
    was never healthy, so the run must exhaust after MAX attempts."""
    attempts = WebSocketManager.MAX_RECONNECT_ATTEMPTS
    sdk.reset(["stay_open"] * (attempts + 3))
    ws_manager.subscribe([TOKEN])
    ws_manager.start()

    for i in range(1, attempts + 2):                 # first try + MAX retries
        silent_cycle(ws_manager, sdk, clock, health, i)

    assert wait_until(lambda: ws_manager.stats["state"] == "exhausted")
    ws_manager._ws_thread.join(timeout=5)
    assert not ws_manager._ws_thread.is_alive()
    assert backoff.delays == [1.0, 2.0, 4.0, 8.0, 16.0][:attempts]
    assert ws_manager.stats["silent_failures"] == attempts + 1
    assert ws_manager.stats["reconnect_count"] == attempts
    assert len(sdk.instances) == attempts + 1        # no further attempt
    assert ws_manager.stats["is_running"] is False


def test_budget_resets_only_after_genuinely_healthy_operation(
    ws_manager, sdk, clock, backoff, health
):
    """Healthy = evidence kept arriving for at least STABLE_CONNECTION_SECONDS.
    A single early pong followed by silence, or no evidence at all, does
    not count however long the socket stayed open."""
    sdk.reset(["stay_open"] * 5)
    ws_manager.subscribe([TOKEN])
    ws_manager.start()

    # 1: healthy for 60 s (pong every 10 s), then silent -> budget reset, attempt 1
    assert wait_until(lambda: len(sdk.instances) == 1 and ws_manager.is_synced)
    first = sdk.instances[0]
    for _ in range(6):
        clock.advance(10)
        first.wsapp.last_pong_tm = clock.now
        health.pulse()
    clock.advance(WebSocketManager.HEARTBEAT_TIMEOUT + 1)
    health.pulse()
    assert wait_until(lambda: first.closed_while_open)
    assert wait_until(lambda: len(sdk.instances) == 2 and ws_manager.is_synced)
    assert ws_manager.stats["reconnect_count"] == 1

    # 2: no evidence at all, open 46 s -> attempt 2 (no reset)
    silent_cycle(ws_manager, sdk, clock, health, 2)
    assert wait_until(lambda: len(sdk.instances) == 3 and ws_manager.is_synced)
    assert ws_manager.stats["reconnect_count"] == 2

    # 3: one pong at +10 s, then silent -> healthy for 10 s only -> attempt 3
    third = sdk.instances[2]
    clock.advance(10)
    third.wsapp.last_pong_tm = clock.now
    health.pulse()
    clock.advance(WebSocketManager.HEARTBEAT_TIMEOUT + 1)
    health.pulse()
    assert wait_until(lambda: third.closed_while_open)
    assert wait_until(lambda: len(sdk.instances) == 4 and ws_manager.is_synced)
    assert ws_manager.stats["reconnect_count"] == 3

    # 4: healthy for 40 s, then silent -> reset, attempt 1 again
    fourth = sdk.instances[3]
    for _ in range(4):
        clock.advance(10)
        fourth.wsapp.last_pong_tm = clock.now
        health.pulse()
    clock.advance(WebSocketManager.HEARTBEAT_TIMEOUT + 1)
    health.pulse()
    assert wait_until(lambda: fourth.closed_while_open)
    assert wait_until(lambda: len(sdk.instances) == 5 and ws_manager.is_synced)
    assert ws_manager.stats["reconnect_count"] == 1
    assert backoff.delays == [1.0, 2.0, 4.0, 1.0]  # reset, 2nd, 3rd, reset
