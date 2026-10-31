"""
Trend-Investorr: WebSocket Live Data Stream Manager

Manages real-time market data streaming via Angel One SmartAPI WebSocket.

Architecture:
    - One WebSocket connection (SmartAPI allows up to 3; this manager
      creates exactly one, so capacity is the single-connection limit)
    - A connection can subscribe to 1000 tokens (SmartAPI limit)
    - Token rotation: subscribe/unsubscribe dynamically based on scanner needs
    - Auto-reconnect with exponential backoff on disconnection
    - Thread-safe tick buffer for consumer processing

Feed Modes:
    LTP (1):       Last Traded Price only — minimal bandwidth
    QUOTE (2):     Full quote snapshot — bid/ask, volume, OHLC
    SNAP_QUOTE (3): Full snapshot + depth — most data

Reconnect / resubscribe ownership
---------------------------------
Verified against the SDK the project installs — smartapi-python 1.5.5
(``SmartApi/smartWebSocketV2.py``) on websocket-client 1.9.2
(``websocket/_app.py``; pinned by requirements.txt, not declared by the
SDK) — by driving the real classes' callbacks offline:

  * ``SmartWebSocketV2.connect()`` blocks in ``WebSocketApp.run_forever()``
    and RETURNS NORMALLY on handshake refusal, transport error and clean
    close alike. run_forever swallows the exception, calls ``on_error``,
    then ``teardown()``. A normal return from connect() is therefore
    "the connection is gone", never "success".
  * ``SmartWebSocketV2._on_close(self, wsapp)`` cannot accept websocket-
    client's ``(wsapp, status_code, msg)``: it raises TypeError inside
    ``WebSocketApp._callback``, which logs it and forwards it to
    ``on_error``. The user-level ``on_close`` hook is never invoked in
    practice; every closure surfaces through ``on_error``.
  * ``SmartWebSocketV2._on_error`` is the SDK's own retry loop: while
    ``current_retry_attempt < max_retry_attempt`` (default 1) it
    ``time.sleep(retry_delay=10)`` inside the callback, ``close_connection()``s
    and calls ``connect()`` recursively. Once exhausted it calls
    ``on_error("Max retry attempt reached", "Connection closed")``.
  * ``SmartWebSocketV2._on_open`` calls ``resubscribe()`` instead of the
    user's ``on_open`` whenever ``RESUBSCRIBE_FLAG`` is set (any earlier
    subscribe on that instance without an intervening close_connection()).
    ``resubscribe()`` replays ``input_request_dict``, which ``unsubscribe()``
    never prunes — a replay restores tokens the caller cancelled.

  * Heartbeat: ``connect()`` passes ``ping_interval=10`` but no
    ``ping_timeout``, so websocket-client sends a ping frame every 10 s and
    never checks for a pong. A connection that stops responding (half-open
    TCP) therefore produces NO close callback; the socket thread blocks in
    recv. websocket-client does record ``wsapp.last_pong_tm`` on every pong
    frame. The SDK's own ``last_pong_timestamp`` is never set in practice:
    its ``_on_pong`` only accepts a pong whose payload is "ping", but the
    ping payload is empty, and the text path that could set it
    (``_on_message``) is not registered with WebSocketApp.

Decisions taken here so that only one loop recovers and only one party
decides what is subscribed:

  * The manager owns reconnect. Every SDK client is built with
    ``max_retry_attempt=0`` so the SDK never sleeps or reconnects inside a
    callback; every outcome comes back as connect() returning and is handled
    by ``_run()`` under one bounded, backed-off policy.
  * The manager owns replay. A fresh ``SmartWebSocketV2`` is built for every
    attempt (``RESUBSCRIBE_FLAG`` False, empty ``input_request_dict``) and
    ``connect()`` is never called twice on one instance, so ``resubscribe()``
    is unreachable. ``_on_open`` sends exactly the current requested set.
  * ``on_close`` is wired but not relied upon. The authoritative "connection
    ended" signal is connect() returning (plus ``on_error``).
  * The manager owns liveness. A monitor thread per socket generation
    treats the most recent of {socket opened, last tick, wsapp.last_pong_tm}
    as evidence; no evidence for HEARTBEAT_TIMEOUT retires the generation
    and closes the client, which makes connect() return and hands recovery
    to the same bounded policy. While healthy, the monitor also re-runs the
    subscription sync so a failed change is retried without waiting for the
    next subscribe()/unsubscribe() call.

Raises:
    WebSocketError: On connection failure after all retries.
    SessionExpiredError: If feed token is invalid.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from datetime import datetime
from typing import Any, Callable

from loguru import logger

from core.constants import (
    AuditEventType,
    FeedMode,
    IST,
    SMARTAPI_MAX_TOKENS_PER_CONNECTION,
)
from core.exceptions import WebSocketError, SessionExpiredError
from core.security.audit import AuditTrail
from data.models.market_data import Tick


# Type alias for tick callback
TickCallback = Callable[[Tick], None]


class _Monitor:
    """Health monitor bound to one socket generation."""

    __slots__ = ("gen", "client", "stop", "thread")

    def __init__(self, gen: int, client: Any):
        self.gen = gen
        self.client = client
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None


class WebSocketManager:
    """
    Manages SmartAPI WebSocket connections for live market data.

    Features:
        - Auto-reconnect with exponential backoff (max 5 retries)
        - Thread-safe tick buffer (deque with maxlen for backpressure)
        - Dynamic token subscription/unsubscription
        - Multiple feed modes (LTP, QUOTE, SNAP_QUOTE)
        - Connection health monitoring
        - Graceful shutdown

    Usage:
        ws_mgr = WebSocketManager(session, audit)
        ws_mgr.on_tick(callback_fn)        # Register tick handler
        ws_mgr.subscribe(["2885", "5900"])  # Record request; sent on open
        ws_mgr.start()                     # Start streaming
        ...
        ws_mgr.stop()                      # Graceful shutdown
    """

    # Reconnection config (the manager's loop; the SDK's own retry is off)
    MAX_RECONNECT_ATTEMPTS = 5
    INITIAL_RECONNECT_DELAY = 1.0      # seconds, doubles per consecutive failure
    MAX_RECONNECT_DELAY = 60.0         # seconds
    STABLE_CONNECTION_SECONDS = 30.0   # evidence must keep arriving this long to reset the budget
    SESSION_EXPIRED_WAIT = 5.0         # seconds between checks while re-auth runs
    MAX_SESSION_EXPIRED_WAIT = 300.0   # give up waiting for credentials after this long
    STOP_JOIN_TIMEOUT = 5.0            # seconds stop() waits for the run thread, per try
    MONITOR_JOIN_TIMEOUT = 5.0         # seconds stop() waits for each health monitor thread

    # Health monitoring (per socket generation; see module docstring)
    HEALTH_CHECK_INTERVAL = 5.0        # seconds between checks / pending-change retries
    HEARTBEAT_TIMEOUT = 45.0           # no pong, no tick for this long -> silent failure
                                       # (websocket-client pings every 10 s: >4 missed pongs)

    # Version of the SDK whose behaviour the reconnect policy is written
    # against (see module docstring). Bump only after re-verifying it.
    VERIFIED_SDK_VERSION = "smartapi-python 1.5.5 / websocket-client 1.9.2"

    # Tick buffer config
    TICK_BUFFER_SIZE = 10_000       # Max ticks in buffer before backpressure

    def __init__(
        self,
        session: Any,
        audit: AuditTrail,
        feed_mode: FeedMode = FeedMode.QUOTE,
    ):
        """
        Args:
            session: SmartAPISession for auth tokens.
            audit: AuditTrail for logging connection events.
            feed_mode: Default feed mode for subscriptions.
        """
        self._session = session
        self._audit = audit
        self._feed_mode = feed_mode

        # Identity. Every start() is a *run* (one thread, one retry loop);
        # every SDK client built is a *socket generation*. Shared state may
        # only be changed by the current run / the current generation, so a
        # retired run finishing late, or a retired socket's delayed callback,
        # cannot touch a replacement. All of it is guarded by _sub_lock.
        self._run_id = 0                              # id of the latest run
        self._socket_gen = 0                          # last generation handed out
        self._current_gen: int | None = None          # generation of the attached socket
        self._opened_gen: int | None = None           # generation whose on_open last fired

        # Connection state
        self._ws: Any = None                          # current SmartWebSocketV2 (one per attempt)
        self._is_running = False
        self._is_connected = False
        self._reconnect_count = 0                     # consecutive recovery attempts
        self._reconnect_exhausted = False
        self._connections_opened = 0
        self._last_disconnect_reason: str | None = None
        self._waiting_for_session = False             # blocked on missing credentials
        self._session_wait_s = 0.0                    # how long, on the current run
        self._stale_callbacks_ignored = 0
        self._stale_ticks_dropped = 0

        # Health monitoring. One monitor per socket generation, stopped the
        # moment that generation is retired. Evidence timestamps use
        # _clock (time.time, the same clock websocket-client stamps
        # wsapp.last_pong_tm with); tests substitute a controlled clock.
        self._clock: Callable[[], float] = time.time
        self._monitor: _Monitor | None = None         # monitor of the current generation
        self._monitors: list[_Monitor] = []           # every monitor not yet known to have exited
        self._opened_epoch: float = 0.0               # current generation's open time
        self._last_tick_epoch: float = 0.0
        self._silent_failures = 0
        self._subscription_retries = 0
        self._stop_event = threading.Event()          # per run; wakes the backoff wait
        self._ws_thread: threading.Thread | None = None

        # Subscription state (all guarded by _sub_lock)
        #
        #   _requested_tokens  The latest set the caller wants streamed. This
        #                      is the source of truth: subscribe()/unsubscribe()
        #                      update it immediately, connected or not.
        #
        #   _sent_tokens       Tokens whose subscribe request was handed to the
        #                      SDK without error on the *current* connection.
        #                      "Sent" is not "acknowledged" — SmartWebSocketV2
        #                      surfaces no subscription ack, so this is the
        #                      strongest claim the manager can make. Reset on
        #                      every open/close because a new socket starts
        #                      with nothing subscribed.
        #
        # The socket is brought in line with _requested_tokens by
        # _sync_locked(), which sends the diff (requested - sent to subscribe,
        # sent - requested to unsubscribe). A rejected request leaves the diff
        # untouched, so it is retried on the next change or the next open.
        self._requested_tokens: set[str] = set()
        self._sent_tokens: set[str] = set()
        # Re-entrant: lifecycle sections call _mark_disconnected() while held.
        self._sub_lock = threading.RLock()
        # Fences tick publication against retirement: a tick is buffered and
        # delivered only while holding this and only if its socket generation
        # is still current; retiring a generation takes it first, so it waits
        # for an in-flight delivery and nothing from that generation can be
        # published afterwards. Lock order: _delivery_lock -> _sub_lock.
        self._delivery_lock = threading.RLock()

        # Tick processing
        self._tick_buffer: deque[Tick] = deque(maxlen=self.TICK_BUFFER_SIZE)
        self._tick_callbacks: list[TickCallback] = []
        self._ticks_received: int = 0
        self._ticks_dropped: int = 0

        # Stats
        self._connected_at: datetime | None = None
        self._last_tick_at: datetime | None = None

    def on_tick(self, callback: TickCallback) -> None:
        """
        Register a callback for incoming ticks.

        Callbacks are invoked synchronously in the WebSocket thread.
        Keep them fast — heavy processing should be done by consuming
        from the tick buffer instead.

        Args:
            callback: Function accepting a Tick dataclass.
        """
        self._tick_callbacks.append(callback)
        logger.debug(f"Tick callback registered: {callback.__name__}")

    def subscribe(self, tokens: list[str]) -> None:
        """
        Subscribe to live data for the given instrument tokens.

        Can be called before or after start(). The tokens are recorded as
        requested immediately; the request is sent to the socket now if one
        is open, otherwise when the connection opens.

        Args:
            tokens: List of SmartAPI instrument tokens.

        Raises:
            WebSocketError: If the requested set would exceed the
                single-connection SmartAPI limit. The call is rejected as a
                whole and the previously requested set is left unchanged.
        """
        with self._sub_lock:
            # Capacity is counted over distinct *requested* tokens — pending
            # (not yet sent) and sent alike. Tokens already requested cost
            # nothing, so repeated requests can never push over the limit.
            new_tokens = set(tokens) - self._requested_tokens
            total = len(self._requested_tokens) + len(new_tokens)

            max_allowed = SMARTAPI_MAX_TOKENS_PER_CONNECTION   # one connection
            if total > max_allowed:
                raise WebSocketError(
                    f"Cannot subscribe to {total} tokens — "
                    f"SmartAPI limit is {max_allowed} per connection "
                    f"and this manager holds a single connection",
                    details={"requested": total, "limit": max_allowed},
                )

            self._requested_tokens |= new_tokens
            logger.info(f"Subscription requested for {len(new_tokens)} new tokens (total: {total})")
            self._sync_locked()

    def unsubscribe(self, tokens: list[str]) -> None:
        """
        Unsubscribe from live data for the given tokens.

        Removes the tokens from the requested set immediately. If a request
        for them was already sent on the open socket, an unsubscribe is sent
        now; if not (still offline, or the earlier send failed), there is
        nothing on the wire to undo and they simply will not be sent.

        Args:
            tokens: List of SmartAPI instrument tokens to remove.
        """
        with self._sub_lock:
            removed = set(tokens) & self._requested_tokens
            self._requested_tokens -= removed
            logger.info(f"Subscription cancelled for {len(removed)} tokens")
            self._sync_locked()

    def start(self) -> None:
        """
        Start the WebSocket connection in a background thread.

        Non-blocking: returns immediately, streaming happens in background.
        Idempotent while running. May be called again after stop() or after
        the reconnect budget was exhausted — even if the previous run's
        thread is still finishing its last connect(): that run is retired
        and can no longer change shared state.
        """
        with self._sub_lock:
            if self._is_running:
                logger.warning("WebSocket manager already running")
                return

            self._run_id += 1
            run_id = self._run_id
            self._is_running = True
            self._reconnect_exhausted = False
            self._reconnect_count = 0
            self._stop_event = threading.Event()
            stop_event = self._stop_event
            self._ws_thread = threading.Thread(
                target=self._run,
                args=(run_id, stop_event),
                name="ws-stream",
                daemon=True,
            )
            self._ws_thread.start()
        logger.info(f"WebSocket manager started (run {run_id})")

    def stop(self) -> None:
        """Gracefully stop the WebSocket connection and background thread.

        Retires the current run and socket generation first, so whatever
        the run thread does afterwards (it may still be inside connect())
        cannot affect a later start(). Then asks the SDK to close and waits
        a bounded time for the thread.
        """
        logger.info("Stopping WebSocket manager...")
        with self._sub_lock:
            self._is_running = False
            self._stop_event.set()                    # interrupt any backoff wait
            ws, gen, thread = self._ws, self._current_gen, self._ws_thread
            # Retired generations' monitors may still be exiting (a generation
            # can be retired a moment before stop(), or by the close below).
            # Join every one we have not yet seen finish.
            monitors = list(self._monitors)

        self._close_quietly(ws)
        if thread and thread.is_alive():
            thread.join(timeout=self.STOP_JOIN_TIMEOUT)
            if thread.is_alive() and ws is not None:
                # connect() may have created its WebSocketApp after our first
                # close_connection() (which is a no-op before that). Close again.
                self._close_quietly(ws)
                thread.join(timeout=self.STOP_JOIN_TIMEOUT)
            if thread.is_alive():
                logger.warning(
                    "WebSocket thread still finishing its connection attempt; "
                    "its run and socket are retired and it will exit on its own"
                )

        if gen is not None:
            self._mark_disconnected("stopped", gen)   # also signals the current monitor
        for monitor in monitors:
            monitor.stop.set()                        # retired ones were signalled already
            if monitor.thread is not None and monitor.thread is not threading.current_thread():
                monitor.thread.join(timeout=self.MONITOR_JOIN_TIMEOUT)
                if monitor.thread.is_alive():
                    logger.warning(f"WebSocket health monitor {monitor.gen} still finishing a check")
        with self._sub_lock:
            self._monitors = [m for m in self._monitors if m.thread and m.thread.is_alive()]
        self._audit.log(
            AuditEventType.SCHEDULER_TASK,
            {
                "action": "websocket_stopped",
                "ticks_received": self._ticks_received,
                "ticks_dropped": self._ticks_dropped,
                "reconnect_count": self._reconnect_count,
            },
        )
        logger.info(
            f"WebSocket stopped | ticks: {self._ticks_received} | "
            f"dropped: {self._ticks_dropped} | reconnects: {self._reconnect_count}"
        )

    @staticmethod
    def _close_quietly(ws: Any) -> None:
        if ws is None:
            return
        try:
            ws.close_connection()                     # ends run_forever -> connect() returns
        except Exception as e:
            logger.warning(f"Error closing WebSocket (non-critical): {e}")

    def _run_active(self, run_id: int) -> bool:
        """True while ``run_id`` is the current run and it has not been stopped."""
        return self._is_running and self._run_id == run_id

    def _run(self, run_id: int, stop_event: threading.Event) -> None:
        """Main loop for one run: one bounded, backed-off recovery policy for
        every way a connection can end.

        ``_connect()`` returns when the socket is gone — a clean server
        close, a transport error, a refused handshake and a silent failure
        closed by the monitor all come back the same way (see module
        docstring) — so *every* return while this run is still current is
        treated as a failure to recover from.

        The attempt budget resets only after *genuinely healthy* operation:
        liveness evidence (tick or pong) kept arriving for at least
        ``STABLE_CONNECTION_SECONDS`` after the socket opened. Uptime alone
        does not count — a silent connection lives ``HEARTBEAT_TIMEOUT``
        before the monitor closes it, and repeated silent connections must
        exhaust the budget rather than refresh it.

        A missing session (``SessionExpiredError``) is not a socket failure
        and does not spend the socket budget, but it is bounded too:
        ``MAX_SESSION_EXPIRED_WAIT`` of waiting without credentials ends the
        run as exhausted. stop() interrupts the wait.

        Once this run is retired (stop(), or a newer start()), the loop
        exits without touching counters, flags or the socket in use.
        """
        delay = self.INITIAL_RECONNECT_DELAY
        session_wait_started: float | None = None

        while self._run_active(run_id):
            session: tuple[float, float] | None = None
            try:
                session = self._connect(run_id)
                reason = self._last_disconnect_reason or "connection closed"
            except SessionExpiredError as e:
                now = self._clock()
                if session_wait_started is None:
                    session_wait_started = now
                waited = now - session_wait_started
                with self._sub_lock:
                    if not self._run_active(run_id):
                        break
                    self._waiting_for_session = True
                    self._session_wait_s = waited
                if waited >= self.MAX_SESSION_EXPIRED_WAIT:
                    self._exhaust(
                        run_id,
                        f"session expired: credentials unavailable for {waited:.0f}s "
                        f"(limit {self.MAX_SESSION_EXPIRED_WAIT:.0f}s): {e}",
                    )
                    break
                logger.warning(
                    f"WebSocket session expired — waiting for re-auth "
                    f"({waited:.0f}s/{self.MAX_SESSION_EXPIRED_WAIT:.0f}s): {e}"
                )
                self._backoff_wait(self.SESSION_EXPIRED_WAIT, stop_event)
                continue
            except WebSocketError as e:
                reason = str(e)
            except Exception as e:
                reason = f"unexpected error: {e}"

            session_wait_started = None               # credentials were present this attempt

            with self._sub_lock:
                if not self._run_active(run_id):
                    break                             # stopped or superseded: leave state alone

                healthy_for = (session[1] - session[0]) if session is not None else 0.0
                if healthy_for >= self.STABLE_CONNECTION_SECONDS:
                    self._reconnect_count = 0
                    delay = self.INITIAL_RECONNECT_DELAY

                self._reconnect_count += 1
                exhausted = self._reconnect_count > self.MAX_RECONNECT_ATTEMPTS
                if exhausted:
                    self._reconnect_count = self.MAX_RECONNECT_ATTEMPTS
                attempt = self._reconnect_count

            if exhausted:
                self._exhaust(run_id, reason)
                break

            logger.warning(
                f"WebSocket disconnected ({reason}; healthy for {healthy_for:.0f}s) — "
                f"reconnecting in {delay:.0f}s (attempt {attempt}/{self.MAX_RECONNECT_ATTEMPTS})"
            )
            self._backoff_wait(delay, stop_event)
            delay = min(delay * 2, self.MAX_RECONNECT_DELAY)

    def _exhaust(self, run_id: int, reason: str) -> None:
        """End run ``run_id`` as exhausted (no-op if it is no longer current)."""
        with self._sub_lock:
            if not self._run_active(run_id):
                return
            self._reconnect_exhausted = True
            self._is_running = False
            self._waiting_for_session = False
            self._last_disconnect_reason = reason
            attempts = self._reconnect_count
        logger.error(f"WebSocket recovery exhausted — giving up | {reason}")
        self._audit.log(
            AuditEventType.ERROR,
            {
                "component": "websocket",
                "error": reason,
                "reconnect_attempts": attempts,
            },
        )

    def _backoff_wait(self, delay: float, stop_event: threading.Event | None = None) -> None:
        """Sleep between attempts; returns early when this run is stopped."""
        (stop_event or self._stop_event).wait(delay)

    def _create_sdk_client(self, **kwargs: Any) -> Any:
        """Build a fresh SmartWebSocketV2. Seam for offline tests."""
        from SmartApi.smartWebSocketV2 import SmartWebSocketV2
        return SmartWebSocketV2(**kwargs)

    def _attach_socket(self, ws: Any, run_id: int | None = None) -> int | None:
        """Make ``ws`` the current socket generation and bind its callbacks.

        The SDK hooks are bound to the generation number, not to the socket
        object: SmartWebSocketV2 passes the underlying WebSocketApp (or, for
        on_error, two strings) to the hooks, so identity cannot be recovered
        from the arguments. Returns the generation, or None if ``run_id`` is
        no longer the current run (stop()/start() raced the attempt).
        """
        with self._sub_lock:
            if run_id is not None and not self._run_active(run_id):
                return None
            self._socket_gen += 1
            gen = self._socket_gen
            ws.on_open = lambda wsapp, client=ws: self._on_open(gen, wsapp, client)
            ws.on_data = lambda wsapp, message: self._on_data(gen, wsapp, message)
            ws.on_error = lambda *args: self._on_error(gen, *args)
            ws.on_close = lambda *args: self._on_close(gen, *args)
            self._ws = ws
            self._current_gen = gen
            return gen

    def _connect(self, run_id: int) -> tuple[float, float] | None:
        """
        Establish one WebSocket connection and block until it ends.

        A fresh SDK client is built for every call — never reused — with the
        SDK's internal retry disabled, so the only party that reconnects or
        resubscribes is ``_run()`` / ``_on_open()``. Returns (normally) when
        the socket is gone, with ``(opened, last_evidence)`` epochs for this
        socket, or None if it never opened.

        Raises:
            WebSocketError: If the client cannot be built or the SDK raises
                out of connect() (it normally does not; see module docstring).
            SessionExpiredError: If auth tokens are invalid.
        """
        try:
            api_key = self._session._vault.get_credential("ANGEL_API_KEY")
            client_id = self._session.client_id
            feed_token = self._session.feed_token
            jwt_token = self._session.jwt_token

            if not feed_token or not jwt_token:
                raise SessionExpiredError(
                    "Missing feed/JWT token — session may have expired",
                    details={"has_feed_token": bool(feed_token), "has_jwt": bool(jwt_token)},
                )
            with self._sub_lock:                      # credentials available: wait is over
                self._waiting_for_session = False
                self._session_wait_s = 0.0

            ws = self._create_sdk_client(
                auth_token=jwt_token,
                api_key=api_key,
                client_code=client_id,
                feed_token=feed_token,
                max_retry_attempt=0,      # SDK retry OFF: _run() owns recovery
            )

            gen = self._attach_socket(ws, run_id)
            if gen is None:
                return None               # run retired before we could connect
            if not self._run_active(run_id):
                # stop() landed between attach and here. Its close_connection()
                # was a no-op (no WebSocketApp yet), so do not open at all.
                self._mark_disconnected("retired before connect", gen)
                return None
            try:
                ws.connect()              # blocks in run_forever until the socket ends
            finally:
                self._mark_disconnected("connection closed", gen)   # no-op if retired already

            with self._sub_lock:
                if self._opened_gen != gen:
                    return None
                return (self._opened_epoch, self._evidence_epoch(ws))

        except ImportError:
            raise WebSocketError(
                "SmartApi package not installed. Run: pip install smartapi-python"
            )

        except SessionExpiredError:
            raise

        except Exception as e:
            raise WebSocketError(
                f"WebSocket connection failed: {e}",
                details={"error": type(e).__name__, "message": str(e)},
            )

    def _mark_disconnected(self, reason: str, gen: int) -> bool:
        """Retire socket generation ``gen`` if it is the current one.

        Returns False (and changes nothing) when ``gen`` has already been
        retired — a delayed callback or a late-finishing run must not
        disconnect the replacement or wipe its sent state.
        """
        with self._delivery_lock, self._sub_lock:     # wait out an in-flight delivery
            if gen != self._current_gen:
                return False
            was_connected = self._is_connected
            self._is_connected = False
            self._sent_tokens.clear()                 # nothing is live on a dead socket
            self._last_disconnect_reason = reason
            self._ws = None
            self._current_gen = None
            if self._monitor is not None and self._monitor.gen == gen:
                self._monitor.stop.set()              # retired: its monitor exits
                self._monitor = None
        if was_connected:
            logger.info(f"WebSocket disconnected | reason={reason}")
        return True

    # ── WebSocket Callbacks ──────────────────────────────

    def _on_open(self, gen: int, ws: Any, client: Any = None) -> None:
        """SDK ``on_open(wsapp)`` for socket generation ``gen``.

        This is the only place subscriptions are (re)established after a
        connection opens: exactly the current requested set, nothing from
        any previous socket and nothing from the SDK's own replay.

        A retired generation opening late (stop() closed it before the SDK
        had built its WebSocketApp, so that close was a no-op) is not just
        ignored: it is closed here, otherwise it would stay connected beside
        the replacement and hold a broker slot. ``client`` is the
        SmartWebSocketV2 the hook was bound to — the SDK only passes the
        underlying WebSocketApp.
        """
        stale_client = None
        with self._sub_lock:
            if gen != self._current_gen:
                self._stale_callbacks_ignored += 1
                logger.warning(f"Retired socket generation {gen} opened late — closing it")
                stale_client = client
            else:
                # Flip to connected *inside* the lock so a concurrent subscribe()
                # either sees "offline" (and only records the request) or sees
                # the fully reset per-connection state — never a half-open socket.
                self._is_connected = True
                self._connected_at = datetime.now(IST)
                self._opened_epoch = self._clock()
                self._opened_gen = gen
                self._connections_opened += 1
                self._sent_tokens.clear()   # fresh socket: nothing sent on it yet
                logger.info(f"WebSocket connected (socket generation {gen})")
                self._sync_locked()
                self._start_monitor(gen, client)
        if stale_client is not None:
            self._close_quietly(stale_client)         # outside the lock; ends its run_forever

    def _on_data(self, gen: int, ws: Any, message: Any) -> None:
        """SDK ``on_data(wsapp, message)`` for socket generation ``gen``.

        Parsing touches no shared state and runs unlocked. Publication —
        buffering the tick and delivering it to callbacks — happens under
        ``_delivery_lock`` with the generation re-checked, so a tick whose
        socket was retired while it was being parsed is dropped, and a
        retirement cannot complete in the middle of a delivery.
        """
        try:
            with self._sub_lock:
                if gen != self._current_gen:          # cheap early exit
                    self._stale_ticks_dropped += 1
                    return

            tick = self._parse_tick(message)
            if tick is None:
                return

            with self._delivery_lock:
                with self._sub_lock:
                    if gen != self._current_gen:      # retired while parsing
                        self._stale_ticks_dropped += 1
                        return

                self._ticks_received += 1
                self._last_tick_at = datetime.now(IST)
                self._last_tick_epoch = self._clock()

                # Add to buffer
                if len(self._tick_buffer) >= self.TICK_BUFFER_SIZE:
                    self._ticks_dropped += 1
                self._tick_buffer.append(tick)

                # Invoke callbacks
                for callback in self._tick_callbacks:
                    try:
                        callback(tick)
                    except Exception as e:
                        logger.error(f"Tick callback error: {e}")

        except Exception as e:
            logger.debug(f"Tick parse error (non-critical): {e}")

    def _on_error(self, gen: int, *args: Any) -> None:
        """SDK ``on_error`` for socket generation ``gen``.

        Called as ``on_error(wsapp, error)`` — or, with its retry disabled,
        as ``on_error("Max retry attempt reached", "Connection closed")``
        after the SDK has already closed the socket. This is also how every
        *close* reaches us, because the SDK's own ``_on_close`` wrapper
        cannot accept websocket-client's arguments.
        """
        source, error = (tuple(args) + (None, None))[:2]
        reason = f"{source}: {error}" if isinstance(source, str) else (str(error) or "Unknown error")
        if not self._mark_disconnected(reason, gen):
            with self._sub_lock:
                self._stale_callbacks_ignored += 1
            logger.debug(f"Ignoring on_error from retired socket generation {gen}: {reason}")
            return
        logger.error(f"WebSocket error: {reason}")

    def _on_close(self, gen: int, *args: Any) -> None:
        """SDK ``on_close`` for socket generation ``gen``.

        Wired for completeness: SmartWebSocketV2 would forward this as
        ``on_close(wsapp)`` if its wrapper worked, and websocket-client
        passes ``(wsapp, code, msg)``; both forms are accepted. In practice
        closes arrive via ``_on_error`` and connect() returning, and the
        manager does not depend on this hook firing.
        """
        _, code, msg = (tuple(args) + (None, None, None))[:3]
        reason = f"closed | code={code} | msg={msg}"
        if not self._mark_disconnected(reason, gen):
            with self._sub_lock:
                self._stale_callbacks_ignored += 1
            logger.debug(f"Ignoring on_close from retired socket generation {gen}")
            return
        logger.info(f"WebSocket {reason}")

    # ── Health Monitoring ────────────────────────────────

    def _start_monitor(self, gen: int, client: Any) -> None:
        """Start the health monitor for socket generation ``gen``.
        Caller holds ``_sub_lock`` and ``gen`` is current."""
        if client is None:
            return                                    # nothing to inspect or close
        if self._monitor is not None:                 # defensive: only one at a time
            self._monitor.stop.set()
        monitor = _Monitor(gen, client)
        monitor.thread = threading.Thread(
            target=self._monitor_loop,
            args=(monitor,),
            name=f"ws-health-{gen}",
            daemon=True,
        )
        self._monitor = monitor
        self._monitors = [m for m in self._monitors if m.thread and m.thread.is_alive()]
        self._monitors.append(monitor)
        monitor.thread.start()

    def _monitor_wait(self, stop_event: threading.Event, interval: float) -> bool:
        """Wait for the next check. True means the monitor was stopped."""
        return stop_event.wait(interval)

    def _monitor_loop(self, monitor: _Monitor) -> None:
        while not self._monitor_wait(monitor.stop, self.HEALTH_CHECK_INTERVAL):
            if not self._health_check(monitor.gen, monitor.client):
                break

    def _evidence_epoch(self, client: Any) -> float:
        """Most recent proof that the current socket is alive."""
        wsapp = getattr(client, "wsapp", None)
        pong = float(getattr(wsapp, "last_pong_tm", 0.0) or 0.0)
        sdk_pong = float(getattr(client, "last_pong_timestamp", None) or 0.0)
        return max(self._opened_epoch, self._last_tick_epoch, pong, sdk_pong)

    def _health_check(self, gen: int, client: Any) -> bool:
        """One health check for socket generation ``gen``.

        Returns False when the monitor should exit: the generation is no
        longer current, or it was just declared dead. A retired generation's
        check is a no-op — it can neither close nor reconnect anything.

        While healthy, re-runs the subscription sync so a change that failed
        earlier (rejected send, held-back replacement) is retried without
        waiting for the next subscribe()/unsubscribe(); the capacity guard
        inside ``_sync_locked`` applies unchanged.
        """
        with self._sub_lock:
            if gen != self._current_gen or not self._is_connected:
                return False
            now = self._clock()
            quiet_for = now - self._evidence_epoch(client)
            if quiet_for > self.HEARTBEAT_TIMEOUT:
                reason = (
                    f"heartbeat timeout: no pong or tick for {quiet_for:.0f}s "
                    f"(limit {self.HEARTBEAT_TIMEOUT:.0f}s)"
                )
                self._silent_failures += 1
            else:
                reason = None
                if self._requested_tokens != self._sent_tokens:
                    self._subscription_retries += 1
                    logger.info(
                        f"Retrying {len(self._requested_tokens ^ self._sent_tokens)} "
                        f"pending subscription change(s)"
                    )
                    self._sync_locked()

        if reason is None:
            return True

        logger.error(f"WebSocket silent failure on generation {gen}: {reason}")
        if self._mark_disconnected(reason, gen):      # retire first: nothing else may act on it
            self._close_quietly(client)               # run_forever ends -> _run() recovers
        return False

    # ── Internal Operations ──────────────────────────────

    def _sync_locked(self) -> None:
        """Bring the open socket in line with ``_requested_tokens``.

        Caller must hold ``_sub_lock``. No-op while disconnected. Sends the
        unsubscribe diff first (frees SmartAPI slots), then the subscribe
        diff. A batch that the SDK rejects leaves ``_sent_tokens`` untouched,
        so the same diff is retried on the next call.

        Additions are only sent into slots that are actually free on the
        wire. If an unsubscribe failed, its token still occupies a slot, so
        the corresponding replacement is held back (it stays in
        ``pending_changes``) until a later sync frees the slot. The wire
        therefore never holds more than the per-connection limit, even
        though the *requested* set may already reflect the swap.
        """
        if not self._is_connected or not self._ws:
            return

        to_remove = self._sent_tokens - self._requested_tokens
        if to_remove and self._send_unsubscribe(sorted(to_remove)):
            self._sent_tokens -= to_remove

        to_add = sorted(self._requested_tokens - self._sent_tokens)
        free_slots = SMARTAPI_MAX_TOKENS_PER_CONNECTION - len(self._sent_tokens)
        if len(to_add) > free_slots:
            still_live = len(self._sent_tokens - self._requested_tokens)
            logger.warning(
                f"Holding back {len(to_add) - max(free_slots, 0)} subscription(s): "
                f"connection full, {still_live} unsubscribe(s) not yet confirmed sent"
            )
            to_add = to_add[:max(free_slots, 0)]

        if to_add and self._send_subscribe(to_add):
            self._sent_tokens |= set(to_add)

    def _send_subscribe(self, tokens: list[str]) -> bool:
        """Hand a subscribe request to the SDK. True if it was sent without
        error — this says nothing about whether the broker accepted it."""
        try:
            self._ws.subscribe(
                correlation_id="trend_investorr",
                mode=self._feed_mode.value,
                token_list=[{"exchangeType": 1, "tokens": tokens}],  # NSE
            )
            logger.debug(f"Subscribe request sent for {len(tokens)} tokens")
            return True
        except Exception as e:
            logger.error(f"WebSocket subscribe failed for {len(tokens)} tokens: {e}")
            return False

    def _send_unsubscribe(self, tokens: list[str]) -> bool:
        """Hand an unsubscribe request to the SDK. True if sent without error."""
        try:
            self._ws.unsubscribe(
                correlation_id="trend_investorr",
                mode=self._feed_mode.value,
                token_list=[{"exchangeType": 1, "tokens": tokens}],
            )
            logger.debug(f"Unsubscribe request sent for {len(tokens)} tokens")
            return True
        except Exception as e:
            logger.error(f"WebSocket unsubscribe failed for {len(tokens)} tokens: {e}")
            return False

    def _parse_tick(self, message: Any) -> Tick | None:
        """
        Parse SmartAPI WebSocket message into a Tick dataclass.

        Handles both binary and JSON message formats.
        Returns None for non-tick messages (heartbeats, etc.).
        """
        try:
            if isinstance(message, str):
                data = json.loads(message)
            elif isinstance(message, dict):
                data = message
            else:
                return None  # Binary/heartbeat — skip

            token = str(data.get("token", ""))
            if not token:
                return None

            ltp = float(data.get("last_traded_price", 0)) / 100  # SmartAPI sends in paisa
            timestamp = datetime.now(IST)

            return Tick(
                symbol=data.get("name", ""),
                token=token,
                ltp=ltp,
                timestamp=timestamp,
                volume=int(data.get("volume_trade_for_the_day", 0)),
                open=float(data.get("open_price_of_the_day", 0)) / 100,
                high=float(data.get("high_price_of_the_day", 0)) / 100,
                low=float(data.get("low_price_of_the_day", 0)) / 100,
                close=float(data.get("closed_price", 0)) / 100,
                best_bid=float(data.get("best_5_buy_data", [{}])[0].get("price", 0) if data.get("best_5_buy_data") else 0) / 100,
                best_ask=float(data.get("best_5_sell_data", [{}])[0].get("price", 0) if data.get("best_5_sell_data") else 0) / 100,
                total_buy_qty=int(data.get("total_buy_quantity", 0)),
                total_sell_qty=int(data.get("total_sell_quantity", 0)),
                avg_trade_price=float(data.get("average_traded_price", 0)) / 100,
            )

        except (KeyError, ValueError, TypeError, IndexError) as e:
            logger.debug(f"Tick parse error: {e}")
            return None

    # ── Tick Buffer Access ───────────────────────────────

    def get_latest_ticks(self, count: int = 100) -> list[Tick]:
        """Get the most recent N ticks from the buffer."""
        return list(self._tick_buffer)[-count:]

    def get_latest_tick(self, token: str) -> Tick | None:
        """Get the most recent tick for a specific token."""
        for tick in reversed(self._tick_buffer):
            if tick.token == token:
                return tick
        return None

    def drain_buffer(self) -> list[Tick]:
        """
        Drain all ticks from the buffer (consumer pattern).

        Returns all buffered ticks and clears the buffer.
        Thread-safe via deque's atomic popleft.
        """
        ticks = []
        while self._tick_buffer:
            try:
                ticks.append(self._tick_buffer.popleft())
            except IndexError:
                break
        return ticks

    # ── Status & Monitoring ──────────────────────────────

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def reconnect_exhausted(self) -> bool:
        """True once the recovery budget was spent; start() clears it."""
        return self._reconnect_exhausted

    @property
    def state(self) -> str:
        """One of: stopped | connecting | connected | reconnecting | exhausted."""
        if self._is_connected:
            return "connected"
        if self._reconnect_exhausted:
            return "exhausted"
        if self._is_running:
            if self._waiting_for_session:
                return "waiting_for_session"
            return "reconnecting" if self._reconnect_count else "connecting"
        return "stopped"

    @property
    def requested_count(self) -> int:
        """Tokens the caller currently wants streamed."""
        with self._sub_lock:
            return len(self._requested_tokens)

    @property
    def sent_count(self) -> int:
        """Tokens whose subscribe request was sent on the current connection
        (not a broker acknowledgment — the SDK exposes none)."""
        with self._sub_lock:
            return len(self._sent_tokens)

    @property
    def is_synced(self) -> bool:
        """True when a socket is open *and* it matches the requested set —
        every requested token was sent on it and nothing else is live.
        ``is_connected`` alone flips before the requested set is sent."""
        with self._sub_lock:
            return self._is_connected and self._requested_tokens == self._sent_tokens

    @property
    def pending_changes(self) -> set[str]:
        """Tokens whose socket state does not yet match the request: either
        requested but not sent (offline, or send failed) or sent but no
        longer requested (unsubscribe failed)."""
        with self._sub_lock:
            return self._requested_tokens ^ self._sent_tokens

    @property
    def buffer_size(self) -> int:
        return len(self._tick_buffer)

    @property
    def stats(self) -> dict[str, Any]:
        """Connection statistics for monitoring."""
        with self._sub_lock:
            requested = len(self._requested_tokens)
            sent = len(self._sent_tokens)
            pending = len(self._requested_tokens ^ self._sent_tokens)
            stale_callbacks = self._stale_callbacks_ignored
            synced = self._is_connected and self._requested_tokens == self._sent_tokens
            monitor = self._monitor
            evidence_age = (
                round(self._clock() - self._evidence_epoch(monitor.client), 1)
                if monitor is not None and self._is_connected else None
            )
        return {
            "state": self.state,
            "is_synced": synced,
            "monitor_alive": bool(monitor and monitor.thread and monitor.thread.is_alive()),
            "evidence_age_s": evidence_age,
            "silent_failures": self._silent_failures,
            "subscription_retries": self._subscription_retries,
            "run_id": self._run_id,
            "socket_generation": self._socket_gen,
            "stale_callbacks_ignored": stale_callbacks,
            "stale_ticks_dropped": self._stale_ticks_dropped,
            "is_connected": self._is_connected,
            "is_running": self._is_running,
            "reconnect_exhausted": self._reconnect_exhausted,
            "connections_opened": self._connections_opened,
            "last_disconnect_reason": self._last_disconnect_reason,
            "session_wait_s": round(self._session_wait_s),
            "requested_tokens": requested,
            "sent_tokens": sent,
            "pending_changes": pending,
            "ticks_received": self._ticks_received,
            "ticks_dropped": self._ticks_dropped,
            "buffer_size": len(self._tick_buffer),
            "reconnect_count": self._reconnect_count,
            "connected_at": self._connected_at.isoformat() if self._connected_at else None,
            "last_tick_at": self._last_tick_at.isoformat() if self._last_tick_at else None,
        }

    def __repr__(self) -> str:
        return (
            f"WebSocketManager(connected={self._is_connected}, "
            f"requested={len(self._requested_tokens)}, sent={len(self._sent_tokens)}, "
            f"ticks={self._ticks_received})"
        )
