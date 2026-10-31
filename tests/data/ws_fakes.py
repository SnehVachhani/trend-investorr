"""
Offline test doubles for WebSocketManager.

FakeSmartWebSocketV2 reproduces the behaviour of the SDK the project installs
— smartapi-python 1.5.5 on websocket-client 1.9.2 — verified by driving the
real classes' callbacks offline. See test_websocket_reconnect.py for the
list of reproduced facts.
"""

from __future__ import annotations

import threading
import time
import types
from collections import deque

from core.constants import AuditEventType


# ── SDK-faithful fake ─────────────────────────────────────────


class WebSocketConnectionClosedException(Exception):
    """websocket._exceptions.WebSocketConnectionClosedException stand-in."""


class FakeSmartWebSocketV2:
    """Behavioural replica of smartapi-python 1.5.5 ``SmartWebSocketV2`` on
    top of websocket-client 1.9.2 ``WebSocketApp``, for the surface the
    manager touches. One instance == one SDK object; ``connect()`` runs the
    next scripted session outcome synchronously (it "is" run_forever).

    Session outcomes (``FakeSmartWebSocketV2.script``):
        "handshake_refused"  run_forever: on_error(ConnectionRefusedError)
                             then teardown -> on_close(None, None); returns.
        "server_close"       open, then teardown -> on_close(1000, msg).
        "transport_error"    open, then on_error(ConnectionResetError) and
                             teardown -> on_close(None, None).
        "stay_open"          open and block until close_connection() or
                             drop(<outcome>) is called from the test.
    """

    script: deque[str] = deque()
    instances: list["FakeSmartWebSocketV2"] = []
    # Test hooks consumed per connect() call, in order. A gate holds
    # connect() *before* it builds its WebSocketApp — the thread is
    # "descheduled" between the manager attaching the client and the SDK
    # opening it — which is exactly when close_connection() is a no-op.
    connect_gates: deque[threading.Event | None] = deque()
    subscribe_delay: float = 0.0                     # seconds each subscribe() takes
    subscribe_failures: int = 0                      # next N subscribe() calls raise
    unsubscribe_failures: int = 0                    # next N unsubscribe() calls raise

    def __init__(self, auth_token, api_key, client_code, feed_token,
                 max_retry_attempt=1, retry_strategy=0, retry_delay=10,
                 retry_multiplier=2, retry_duration=60):
        assert all([auth_token, api_key, client_code, feed_token])  # SDK _sanity_check
        self.kwargs = dict(max_retry_attempt=max_retry_attempt, retry_delay=retry_delay)
        self.RESUBSCRIBE_FLAG = False
        self.DISCONNECT_FLAG = True
        self.MAX_RETRY_ATTEMPT = max_retry_attempt
        self.retry_delay = retry_delay
        self.current_retry_attempt = 0
        self.input_request_dict: dict = {}
        self.wsapp = None
        # user hooks (SDK defaults are no-ops)
        self.on_open = lambda wsapp: None
        self.on_data = lambda wsapp, data: None
        self.on_close = lambda wsapp: None
        self.on_error = lambda *args: None
        # observability
        self.connect_calls = 0
        self.close_calls = 0
        self.closed_while_open = False                   # close_connection() hit a live session
        self.frames: list[tuple[str, list[str]]] = []   # frames actually sent
        self.live: set[str] = set()                      # tokens live on the wire
        self.peak_live = 0
        self.last_pong_timestamp = None                  # SDK attr; never set in practice
        self.replayed: list[str] = []                    # tokens resubscribe() replayed
        self.sdk_sleeps: list[float] = []                # SDK-internal retry sleeps
        self._open = False
        self._drop: str | None = None
        self._session_over = threading.Event()
        # Test hook: when True, close_connection() is acknowledged but the
        # session (run_forever) only ends once release_close() is called —
        # i.e. the connection attempt is "still finishing".
        self.close_blocks = False
        self.close_requested = threading.Event()
        type(self).instances.append(self)

    @classmethod
    def reset(cls, script: list[str]):
        cls.script = deque(script)
        cls.instances = []
        cls.connect_gates = deque()
        cls.subscribe_delay = 0.0
        cls.subscribe_failures = 0
        cls.unsubscribe_failures = 0

    # -- websocket-client WebSocketApp._callback --------------------------

    def _wc_callback(self, callback, *args):
        try:
            callback(self.wsapp, *args)
        except Exception as e:                       # "error from callback ..."
            if callback is not self._on_error:
                self._on_error(self.wsapp, e)

    # -- SDK wrappers registered with WebSocketApp ------------------------

    def _on_open(self, wsapp):
        if self.RESUBSCRIBE_FLAG:
            self.resubscribe()
        else:
            self.on_open(wsapp)

    def _on_close(self, wsapp):                      # one parameter, as in the SDK
        self.on_close(wsapp)

    def _on_error(self, wsapp, error):
        self.RESUBSCRIBE_FLAG = True
        if self.current_retry_attempt < self.MAX_RETRY_ATTEMPT:
            self.current_retry_attempt += 1
            self.sdk_sleeps.append(self.retry_delay)  # time.sleep(retry_delay)
            try:
                self.close_connection()
                self.connect()                       # recursive reconnect
            except Exception as e:
                self.on_error("Reconnect Error", str(e) or "Unknown error")
        else:
            self.close_connection()
            self.on_error("Max retry attempt reached", "Connection closed")

    # -- public SDK API ----------------------------------------------------

    def connect(self):
        self.connect_calls += 1
        gate = type(self).connect_gates.popleft() if type(self).connect_gates else None
        if gate is not None:
            assert gate.wait(timeout=5), "connect gate never released"
        # a fresh websocket.WebSocketApp: last_pong_tm/last_ping_tm are the
        # only heartbeat evidence the stack exposes (set on every pong frame)
        self.wsapp = types.SimpleNamespace(last_pong_tm=0.0, last_ping_tm=0.0)
        self.DISCONNECT_FLAG = False
        self._session_over.clear()
        outcome = type(self).script.popleft() if type(self).script else "stay_open"

        if outcome == "handshake_refused":
            self._wc_callback(self._on_error, ConnectionRefusedError("refused"))
            self._wc_callback(self._on_close, None, None)
            return

        self._open = True
        self._wc_callback(self._on_open)

        if outcome == "stay_open":
            assert self._session_over.wait(timeout=5), "session never ended"
            outcome = self._drop or "client_close"

        self._open = False
        if outcome == "client_close":
            self._wc_callback(self._on_close, 1000, "client closed")
        elif outcome == "server_close":
            self._wc_callback(self._on_close, 1000, "server going away")
        elif outcome == "transport_error":
            self._wc_callback(self._on_error, ConnectionResetError("peer reset"))
            self._wc_callback(self._on_close, None, None)
        else:
            raise AssertionError(f"unknown outcome {outcome!r}")

    def close_connection(self):
        self.RESUBSCRIBE_FLAG = False
        self.DISCONNECT_FLAG = True
        self.close_calls += 1
        self.close_requested.set()
        if self.wsapp is None:
            return                                   # SDK: nothing to close yet
        if self._open:
            self.closed_while_open = True
        if not self.close_blocks:
            self._session_over.set()

    def release_close(self):
        """Test hook: let a blocked close_connection() finish the session."""
        self._session_over.set()

    def drop(self, outcome: str):
        """Test hook: the server/transport ends a 'stay_open' session."""
        self._drop = outcome
        self._session_over.set()

    def _send(self, action: str, token_list):
        if not self._open:
            raise WebSocketConnectionClosedException("Connection is already closed.")
        tokens = [t for entry in token_list for t in entry["tokens"]]
        self.frames.append((action, sorted(tokens)))
        cls = type(self)
        if action == "subscribe" and cls.subscribe_failures > 0:
            cls.subscribe_failures -= 1
            raise WebSocketConnectionClosedException("simulated send failure")
        if action == "unsubscribe" and cls.unsubscribe_failures > 0:
            cls.unsubscribe_failures -= 1
            raise WebSocketConnectionClosedException("simulated send failure")
        if action == "subscribe":
            self.live |= set(tokens)
            self.peak_live = max(self.peak_live, len(self.live))
        else:
            self.live -= set(tokens)

    def subscribe(self, correlation_id, mode, token_list):
        for entry in token_list:                     # SDK records for replay ...
            bucket = self.input_request_dict.setdefault(mode, {})
            bucket.setdefault(entry["exchangeType"], []).extend(entry["tokens"])
        if type(self).subscribe_delay:
            time.sleep(type(self).subscribe_delay)
        self._send("subscribe", token_list)
        self.RESUBSCRIBE_FLAG = True

    def unsubscribe(self, correlation_id, mode, token_list):
        # ... and never prunes on unsubscribe.
        self._send("unsubscribe", token_list)
        self.RESUBSCRIBE_FLAG = True

    def resubscribe(self):
        for mode, by_exchange in self.input_request_dict.items():
            for tokens in by_exchange.values():
                self.replayed.extend(tokens)
                self.frames.append(("subscribe", sorted(tokens)))


# ── Other doubles ─────────────────────────────────────────────


class FakeVault:
    def get_credential(self, name):
        return "api-key"


class FakeSession:
    _vault = FakeVault()
    client_id = "C123"
    feed_token = "feed"
    jwt_token = "jwt"


class FakeAudit:
    def __init__(self):
        self.events: list[tuple[AuditEventType, dict]] = []

    def log(self, event_type, payload):
        self.events.append((event_type, payload))


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()
