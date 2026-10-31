"""
Offline regression tests for WebSocketManager subscription bookkeeping.

No broker, no network, no background thread. A fake socket stands in for
``SmartWebSocketV2``; it records every subscribe/unsubscribe request and
fires the manager's ``on_open`` / ``on_close`` hooks with the same
arguments the SDK uses, so the manager's callbacks run through their
genuine code paths.

Production call order (main.py) is always:

    ws_manager.subscribe(all_tokens)   # before the socket exists
    ws_manager.start()                 # -> _connect -> SmartWebSocketV2.connect
                                       # -> on_open -> sync requested set

The manager keeps two distinct sets:

    requested   what the caller wants streamed (source of truth)
    sent        what was handed to the SDK without error on the *current*
                connection — NOT a broker acknowledgment; the SDK exposes
                none, and the fake models none.

SDK hook contract (smartapi-python 1.5.5, SmartApi/smartWebSocketV2.py):

    on_open(wsapp)
    on_data(wsapp, message)
    on_error(wsapp, error)          # or ("Max retry attempt reached", "Connection closed")
    on_close(wsapp)                 # declared form; in practice never reached, because
                                    # the SDK's own _on_close(wsapp) wrapper rejects
                                    # websocket-client's (wsapp, code, msg) and the
                                    # close is forwarded to on_error instead.

These tests drive the hooks directly to exercise subscription bookkeeping;
reconnect ownership is covered in test_websocket_reconnect.py.
"""

from __future__ import annotations

import pytest

from core.constants import SMARTAPI_MAX_TOKENS_PER_CONNECTION
from core.exceptions import WebSocketError
from data.streams import websocket_manager
from data.streams.websocket_manager import WebSocketManager


TOKEN = "2885"      # RELIANCE
TOKEN_B = "11536"   # TCS
TOKEN_C = "1594"    # INFY


# ── Test doubles ─────────────────────────────────────────────


class FakeSocket:
    """Minimal stand-in for SmartApi.smartWebSocketV2.SmartWebSocketV2.

    Only the surface WebSocketManager touches is implemented.

    * ``connect()`` invokes ``on_open(wsapp)`` synchronously (the real client
      does so from its socket thread once the handshake completes).
    * ``close_connection()`` invokes ``on_close(wsapp)`` — one argument, as
      the SDK's ``_on_close(wsapp, code, msg)`` forwards only ``wsapp``.
    * ``subscribe``/``unsubscribe`` record the request and return, or raise
      when told to. Returning is "sent"; no acknowledgment is modelled
      because the SDK provides none.
    """

    def __init__(self, fail_subscribes: int = 0, fail_unsubscribes: int = 0):
        self.calls: list[tuple[str, list[str]]] = []   # ("subscribe"|"unsubscribe", tokens)
        self.fail_subscribes = fail_subscribes          # raise on the next N subscribe calls
        self.fail_unsubscribes = fail_unsubscribes      # raise on the next N unsubscribe calls
        self.close_args: list[tuple] = []               # what on_close was invoked with
        self.live: set[str] = set()                     # tokens actually on the wire
        self.peak_live = 0                              # most tokens ever live at once
        self.on_open = None
        self.on_data = None
        self.on_error = None
        self.on_close = None

    # -- SmartWebSocketV2 API used by the manager --

    def connect(self):
        self.live.clear()                               # a new socket starts empty
        self.on_open(self)

    def close_connection(self):
        args = (self,)
        self.close_args.append(args)
        self.on_close(*args)                            # SDK contract: on_close(wsapp)

    def subscribe(self, correlation_id, mode, token_list):
        tokens = [t for entry in token_list for t in entry["tokens"]]
        self.calls.append(("subscribe", sorted(tokens)))
        if self.fail_subscribes > 0:
            self.fail_subscribes -= 1
            raise RuntimeError("simulated send failure")   # nothing reached the wire
        self.live |= set(tokens)
        self.peak_live = max(self.peak_live, len(self.live))

    def unsubscribe(self, correlation_id, mode, token_list):
        tokens = [t for entry in token_list for t in entry["tokens"]]
        self.calls.append(("unsubscribe", sorted(tokens)))
        if self.fail_unsubscribes > 0:
            self.fail_unsubscribes -= 1
            raise RuntimeError("simulated send failure")   # slot NOT freed
        self.live -= set(tokens)

    # -- assertion helpers --

    def tokens_sent(self, kind: str) -> list[str]:
        return sorted(t for k, toks in self.calls if k == kind for t in toks)


def open_connection(ws_manager: WebSocketManager, socket: FakeSocket) -> None:
    """What WebSocketManager._connect() does once it has built a client:
    attach it as the current socket generation (binding the hooks) and let
    the SDK open it. Minus the SmartApi import and credential lookup."""
    ws_manager._attach_socket(socket)
    socket.connect()


class FakeAudit:
    def log(self, event_type, payload):
        pass


@pytest.fixture
def ws_manager():
    # __init__ only stores session/audit; nothing connects. Opening a socket
    # (open_connection) starts that generation's health monitor, so the
    # manager is stopped at teardown like any real one.
    mgr = WebSocketManager(session=None, audit=FakeAudit())
    yield mgr
    mgr.stop()


@pytest.fixture
def small_limit(monkeypatch):
    """Shrink the per-connection cap so capacity scenarios stay readable.
    The real value is asserted separately in the single-connection test."""
    monkeypatch.setattr(websocket_manager, "SMARTAPI_MAX_TOKENS_PER_CONNECTION", 3)
    return 3


def tokens(n: int, prefix: str = "T") -> list[str]:
    return [f"{prefix}{i}" for i in range(n)]


# ── Cancel before the socket opens ───────────────────────────


def test_token_cancelled_before_connection_opens_is_never_subscribed(ws_manager):
    """subscribe([A, B]) then unsubscribe([A]) while still offline.

    When the socket opens only B may be sent; A was cancelled before it was
    ever on the wire and must not be resurrected by on_open.
    """
    ws_manager.subscribe([TOKEN, TOKEN_B])
    ws_manager.unsubscribe([TOKEN])
    assert ws_manager.requested_count == 1
    assert ws_manager.sent_count == 0               # nothing on the wire yet
    assert ws_manager.pending_changes == {TOKEN_B}

    socket = FakeSocket()
    open_connection(ws_manager, socket)

    assert ws_manager.is_connected
    assert socket.calls == [("subscribe", [TOKEN_B])]
    assert socket.tokens_sent("unsubscribe") == []  # nothing to undo for A
    assert ws_manager.sent_count == 1
    assert ws_manager.pending_changes == set()


# ── Opening a connection sends each request exactly once ─────


def test_open_sends_requested_set_once_and_reconnect_sends_it_once_again(ws_manager):
    """The old on_open sent the pending batch and then immediately re-sent
    the same tokens through its "re-subscribe on reconnect" branch. Each
    open must send the requested set exactly once."""
    ws_manager.subscribe([TOKEN, TOKEN_B])

    socket = FakeSocket()
    open_connection(ws_manager, socket)
    assert socket.calls == [("subscribe", [TOKEN_B, TOKEN])]

    socket.close_connection()
    assert ws_manager.sent_count == 0               # per-connection, gone with the socket
    assert ws_manager.requested_count == 2          # the request itself survives

    open_connection(ws_manager, socket)
    assert socket.calls == [
        ("subscribe", [TOKEN_B, TOKEN]),
        ("subscribe", [TOKEN_B, TOKEN]),
    ]
    assert ws_manager.sent_count == 2


# ── Failed subscribe stays retryable ─────────────────────────


def test_failed_subscribe_on_open_is_retried_on_next_open(ws_manager):
    """The SDK raises on the subscribe issued from on_open. The token was
    requested and never sent, so the next open must send it again."""
    ws_manager.subscribe([TOKEN])

    socket = FakeSocket(fail_subscribes=1)
    open_connection(ws_manager, socket)

    assert socket.calls == [("subscribe", [TOKEN])]
    assert ws_manager.sent_count == 0
    assert ws_manager.pending_changes == {TOKEN}    # still owed to the caller

    socket.close_connection()
    open_connection(ws_manager, socket)

    assert socket.calls == [("subscribe", [TOKEN]), ("subscribe", [TOKEN])]
    assert ws_manager.sent_count == 1
    assert ws_manager.pending_changes == set()


def test_failed_subscribe_is_retried_on_next_change_while_still_connected(ws_manager):
    """No reconnect needed: the next subscription change on the same
    connection re-sends the token that failed earlier alongside the new one."""
    ws_manager.subscribe([TOKEN])
    socket = FakeSocket(fail_subscribes=1)
    open_connection(ws_manager, socket)
    assert ws_manager.pending_changes == {TOKEN}

    ws_manager.subscribe([TOKEN_B])                 # connected: sent immediately

    assert socket.calls == [
        ("subscribe", [TOKEN]),                     # rejected
        ("subscribe", [TOKEN_B, TOKEN]),            # retried together with B
    ]
    assert ws_manager.sent_count == 2
    assert ws_manager.pending_changes == set()


# ── Failed unsubscribe stays retryable ───────────────────────


def test_failed_unsubscribe_keeps_token_as_sent_and_retries_on_next_change(ws_manager):
    """The token is no longer requested but is still live on the socket
    because the unsubscribe send failed. It must stay counted as sent, and
    the next change must retry the unsubscribe before doing anything else."""
    ws_manager.subscribe([TOKEN, TOKEN_B])
    socket = FakeSocket(fail_unsubscribes=1)
    open_connection(ws_manager, socket)

    ws_manager.unsubscribe([TOKEN])

    assert socket.calls[-1] == ("unsubscribe", [TOKEN])
    assert ws_manager.requested_count == 1
    assert ws_manager.sent_count == 2               # still on the wire
    assert ws_manager.pending_changes == {TOKEN}

    ws_manager.subscribe([TOKEN_C])

    assert socket.calls[-2:] == [
        ("unsubscribe", [TOKEN]),                   # retried, now succeeds
        ("subscribe", [TOKEN_C]),
    ]
    assert ws_manager.sent_count == 2               # B and C
    assert ws_manager.pending_changes == set()


def test_failed_unsubscribe_is_dropped_on_reconnect_not_resent(ws_manager):
    """After a failed unsubscribe the token is 'sent but not requested'.
    A fresh connection starts empty, so the only thing to do is not send it."""
    ws_manager.subscribe([TOKEN, TOKEN_B])
    socket = FakeSocket(fail_unsubscribes=1)
    open_connection(ws_manager, socket)
    ws_manager.unsubscribe([TOKEN])
    assert ws_manager.pending_changes == {TOKEN}

    socket.close_connection()
    open_connection(ws_manager, socket)

    assert socket.calls[-1] == ("subscribe", [TOKEN_B])
    assert socket.calls.count(("unsubscribe", [TOKEN])) == 1   # not retried on a new socket
    assert ws_manager.sent_count == 1
    assert ws_manager.pending_changes == set()


# ── Changes while disconnected ───────────────────────────────


def test_changes_while_disconnected_are_not_sent_and_are_applied_on_reopen(ws_manager):
    """Between a close and the next open, subscribe/unsubscribe must only
    update the requested set. Nothing may be written to the dead socket,
    and the reopen must send exactly the final requested set."""
    ws_manager.subscribe([TOKEN, TOKEN_B])
    socket = FakeSocket()
    open_connection(ws_manager, socket)
    socket.close_connection()
    assert not ws_manager.is_connected
    calls_before = list(socket.calls)

    ws_manager.unsubscribe([TOKEN])                 # drop A
    ws_manager.subscribe([TOKEN_C])                 # add C
    ws_manager.subscribe([TOKEN_B])                 # B already requested: no-op

    assert socket.calls == calls_before             # dead socket untouched
    assert ws_manager.requested_count == 2
    assert ws_manager.sent_count == 0
    assert ws_manager.pending_changes == {TOKEN_B, TOKEN_C}

    open_connection(ws_manager, socket)

    assert socket.calls[len(calls_before):] == [("subscribe", [TOKEN_B, TOKEN_C])]
    assert ws_manager.pending_changes == set()


def test_subscribe_while_connected_is_sent_immediately(ws_manager):
    socket = FakeSocket()
    open_connection(ws_manager, socket)
    assert socket.calls == []                       # nothing requested yet

    ws_manager.subscribe([TOKEN])
    assert socket.calls == [("subscribe", [TOKEN])]
    assert ws_manager.sent_count == 1

    ws_manager.unsubscribe([TOKEN])
    assert socket.calls[-1] == ("unsubscribe", [TOKEN])
    assert ws_manager.sent_count == 0
    assert ws_manager.requested_count == 0


# ── SDK close-callback contract ──────────────────────────────


def test_on_close_accepts_sdk_single_argument_form(ws_manager):
    """The SDK's declared hook form is ``on_close(wsapp)``. A hook that
    requires code/msg would raise TypeError inside the SDK. (With the SDK
    actually installed the hook never fires at all — see module docstring —
    which is why the manager also treats on_error / connect() returning as
    the close signal; that path is covered in test_websocket_reconnect.py.)"""
    ws_manager.subscribe([TOKEN])
    socket = FakeSocket()
    open_connection(ws_manager, socket)
    assert ws_manager.is_connected

    socket.close_connection()                       # fake calls on_close(wsapp)

    assert socket.close_args == [(socket,)]         # contract: exactly one argument
    assert not ws_manager.is_connected
    assert ws_manager.sent_count == 0
    assert ws_manager.requested_count == 1


def test_on_close_still_accepts_websocket_client_three_argument_form(ws_manager):
    """Defensive: if the hook is ever wired straight to websocket-client,
    (wsapp, close_status_code, close_msg) must also work."""
    socket = FakeSocket()
    open_connection(ws_manager, socket)

    socket.on_close(socket, 1006, "abnormal closure")   # via the bound hook

    assert not ws_manager.is_connected


# ── Sent is not acknowledged ─────────────────────────────────


def test_sent_means_handed_to_sdk_on_this_connection_not_broker_ack(ws_manager):
    """A request that the SDK accepted without error counts as *sent*. That
    is all the manager can know (no ack exists); it is scoped to the current
    connection and reported under that name, not as 'subscribed'."""
    ws_manager.subscribe([TOKEN])
    socket = FakeSocket()
    open_connection(ws_manager, socket)

    stats = ws_manager.stats
    assert stats["requested_tokens"] == 1
    assert stats["sent_tokens"] == 1
    assert stats["pending_changes"] == 0
    assert "subscribed_tokens" not in stats         # no claim of confirmation

    socket.close_connection()
    stats = ws_manager.stats
    assert stats["requested_tokens"] == 1           # the caller's intent persists
    assert stats["sent_tokens"] == 0                # but nothing is sent on a closed socket
    assert stats["pending_changes"] == 1


# ── Capacity: one connection, distinct tokens ────────────────


def test_capacity_is_the_single_connection_limit(ws_manager):
    """_connect() creates exactly one SmartWebSocketV2, so the cap is the
    per-connection limit — not per-connection × max connections."""
    full = tokens(SMARTAPI_MAX_TOKENS_PER_CONNECTION)
    ws_manager.subscribe(full)                      # offline: all pending, all count
    assert ws_manager.requested_count == SMARTAPI_MAX_TOKENS_PER_CONNECTION

    with pytest.raises(WebSocketError) as exc:
        ws_manager.subscribe(["one-too-many"])
    assert exc.value.details["limit"] == SMARTAPI_MAX_TOKENS_PER_CONNECTION


def test_pending_and_sent_requests_both_count_toward_capacity(ws_manager, small_limit):
    """Two sent on the open socket + one requested while offline = full.
    The offline request occupies a slot even though it is not on the wire."""
    socket = FakeSocket()
    open_connection(ws_manager, socket)
    ws_manager.subscribe(tokens(2))                 # sent
    socket.close_connection()
    ws_manager.subscribe(["T2"])                    # pending (offline)
    assert ws_manager.sent_count == 0
    assert ws_manager.requested_count == 3

    with pytest.raises(WebSocketError):
        ws_manager.subscribe(["T3"])


def test_repeated_requests_do_not_consume_extra_capacity(ws_manager, small_limit):
    """Re-requesting tokens already requested (pending or sent), or listing
    a token twice in one call, must neither raise nor re-send."""
    ws_manager.subscribe(tokens(3))                 # full, all pending
    ws_manager.subscribe(tokens(3))                 # same again: fine
    ws_manager.subscribe(["T0", "T0", "T1"])        # duplicates within a call: fine
    assert ws_manager.requested_count == 3

    socket = FakeSocket()
    open_connection(ws_manager, socket)
    ws_manager.subscribe(tokens(3))                 # now all sent: still fine
    assert socket.calls == [("subscribe", tokens(3))]
    assert ws_manager.sent_count == 3


def test_rejected_oversized_request_leaves_requested_set_unchanged(ws_manager, small_limit):
    """An over-limit call must be rejected as a whole: no token from it may
    be recorded, queued or sent — including the ones that would have fit."""
    ws_manager.subscribe(tokens(2))
    socket = FakeSocket()
    open_connection(ws_manager, socket)
    before = list(socket.calls)

    with pytest.raises(WebSocketError):
        ws_manager.subscribe(["X", "Y"])            # 2 + 2 > 3

    assert ws_manager.requested_count == 2
    assert ws_manager.pending_changes == set()
    assert socket.calls == before                   # nothing partial went out
    assert socket.live == {"T0", "T1"}


# ── Full connection: replacement must wait for a freed slot ──


def test_replacement_is_not_sent_until_failed_unsubscribe_frees_its_slot(
    ws_manager, small_limit
):
    """Connection is full. unsubscribe(A) fails, so A still occupies a slot.
    Requesting replacement Z is legal (the *requested* set is within the
    limit) but Z must not be sent while A is still live; the wire must
    never hold more than the limit. Once the unsubscribe succeeds, Z goes."""
    ws_manager.subscribe(["A", "B", "C"])
    socket = FakeSocket(fail_unsubscribes=2)
    open_connection(ws_manager, socket)
    assert socket.live == {"A", "B", "C"}

    ws_manager.unsubscribe(["A"])                   # fails (1/2): A still live
    ws_manager.subscribe(["Z"])                     # accepted: requested = {B, C, Z}

    # sync retried the unsubscribe (fails 2/2) and must then hold Z back
    assert socket.calls[-1] == ("unsubscribe", ["A"])
    assert socket.tokens_sent("subscribe").count("Z") == 0
    assert socket.live == {"A", "B", "C"}
    assert socket.peak_live <= small_limit
    assert ws_manager.requested_count == 3
    assert ws_manager.sent_count == 3               # A, B, C
    assert ws_manager.pending_changes == {"A", "Z"}

    ws_manager.subscribe(["Z"])                     # any change retries the sync

    assert socket.calls[-2:] == [("unsubscribe", ["A"]), ("subscribe", ["Z"])]
    assert socket.live == {"B", "C", "Z"}
    assert socket.peak_live <= small_limit
    assert ws_manager.pending_changes == set()
