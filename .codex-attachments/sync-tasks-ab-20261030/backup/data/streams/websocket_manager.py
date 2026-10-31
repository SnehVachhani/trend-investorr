"""
Trend-Investorr: WebSocket Live Data Stream Manager

Manages real-time market data streaming via Angel One SmartAPI WebSocket.

Architecture:
    - Up to 3 concurrent WebSocket connections (SmartAPI limit)
    - Each connection can subscribe to 1000 tokens (SmartAPI limit)
    - Token rotation: subscribe/unsubscribe dynamically based on scanner needs
    - Auto-reconnect with exponential backoff on disconnection
    - Thread-safe tick buffer for consumer processing

Feed Modes:
    LTP (1):       Last Traded Price only — minimal bandwidth
    QUOTE (2):     Full quote snapshot — bid/ask, volume, OHLC
    SNAP_QUOTE (3): Full snapshot + depth — most data

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
    SMARTAPI_MAX_WEBSOCKET_CONNECTIONS,
)
from core.exceptions import WebSocketError, SessionExpiredError
from core.security.audit import AuditTrail
from data.models.market_data import Tick


# Type alias for tick callback
TickCallback = Callable[[Tick], None]


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
        ws_mgr.subscribe(["2885", "5900"])  # Subscribe to tokens
        ws_mgr.start()                     # Start streaming
        ...
        ws_mgr.stop()                      # Graceful shutdown
    """

    # Reconnection config
    MAX_RECONNECT_ATTEMPTS = 5
    INITIAL_RECONNECT_DELAY = 1.0  # seconds
    MAX_RECONNECT_DELAY = 60.0     # seconds

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

        # Connection state
        self._ws: Any = None                          # SmartWebSocket instance
        self._is_running = False
        self._is_connected = False
        self._reconnect_count = 0
        self._ws_thread: threading.Thread | None = None

        # Subscription state
        self._subscribed_tokens: set[str] = set()
        self._pending_subscribes: set[str] = set()
        self._pending_unsubscribes: set[str] = set()
        self._sub_lock = threading.Lock()

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

        Can be called before or after start(). If called before,
        tokens are queued and subscribed on connection.

        Args:
            tokens: List of SmartAPI instrument tokens.

        Raises:
            WebSocketError: If total subscriptions exceed SmartAPI limits.
        """
        with self._sub_lock:
            new_tokens = set(tokens) - self._subscribed_tokens
            total = len(self._subscribed_tokens) + len(new_tokens)

            max_allowed = SMARTAPI_MAX_TOKENS_PER_CONNECTION * SMARTAPI_MAX_WEBSOCKET_CONNECTIONS
            if total > max_allowed:
                raise WebSocketError(
                    f"Cannot subscribe to {total} tokens — "
                    f"SmartAPI limit is {max_allowed} "
                    f"({SMARTAPI_MAX_WEBSOCKET_CONNECTIONS} connections × "
                    f"{SMARTAPI_MAX_TOKENS_PER_CONNECTION} tokens each)",
                    details={"requested": total, "limit": max_allowed},
                )

            if self._is_connected and self._ws:
                self._do_subscribe(list(new_tokens))
            else:
                self._pending_subscribes.update(new_tokens)

            logger.info(f"Subscribed to {len(new_tokens)} new tokens (total: {total})")

    def unsubscribe(self, tokens: list[str]) -> None:
        """
        Unsubscribe from live data for the given tokens.

        Args:
            tokens: List of SmartAPI instrument tokens to remove.
        """
        with self._sub_lock:
            tokens_to_remove = set(tokens) & self._subscribed_tokens

            if self._is_connected and self._ws:
                self._do_unsubscribe(list(tokens_to_remove))
            else:
                self._pending_unsubscribes.update(tokens_to_remove)

            self._subscribed_tokens -= tokens_to_remove
            logger.info(f"Unsubscribed from {len(tokens_to_remove)} tokens")

    def start(self) -> None:
        """
        Start the WebSocket connection in a background thread.

        Non-blocking: returns immediately, streaming happens in background.

        Raises:
            WebSocketError: If connection cannot be established.
        """
        if self._is_running:
            logger.warning("WebSocket manager already running")
            return

        self._is_running = True
        self._ws_thread = threading.Thread(
            target=self._run,
            name="ws-stream",
            daemon=True,
        )
        self._ws_thread.start()
        logger.info("WebSocket manager started")

    def stop(self) -> None:
        """Gracefully stop the WebSocket connection and background thread."""
        logger.info("Stopping WebSocket manager...")
        self._is_running = False

        if self._ws:
            try:
                self._ws.close_connection()
            except Exception as e:
                logger.warning(f"Error closing WebSocket (non-critical): {e}")

        if self._ws_thread and self._ws_thread.is_alive():
            self._ws_thread.join(timeout=5.0)

        self._is_connected = False
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

    def _run(self) -> None:
        """Main WebSocket loop with auto-reconnect."""
        reconnect_delay = self.INITIAL_RECONNECT_DELAY

        while self._is_running:
            try:
                self._connect()
                reconnect_delay = self.INITIAL_RECONNECT_DELAY  # Reset on success
                self._reconnect_count = 0

            except WebSocketError as e:
                if not self._is_running:
                    break  # Intentional shutdown

                self._reconnect_count += 1
                if self._reconnect_count > self.MAX_RECONNECT_ATTEMPTS:
                    logger.error(
                        f"WebSocket max reconnect attempts ({self.MAX_RECONNECT_ATTEMPTS}) "
                        f"exhausted — giving up"
                    )
                    self._audit.log(
                        AuditEventType.ERROR,
                        {
                            "component": "websocket",
                            "error": str(e),
                            "reconnect_attempts": self._reconnect_count,
                        },
                    )
                    break

                logger.warning(
                    f"WebSocket disconnected — reconnecting in {reconnect_delay:.0f}s "
                    f"(attempt {self._reconnect_count}/{self.MAX_RECONNECT_ATTEMPTS})"
                )
                time.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, self.MAX_RECONNECT_DELAY)

            except SessionExpiredError:
                logger.warning("WebSocket session expired — waiting for re-auth")
                time.sleep(5.0)  # Let the session manager handle re-auth

            except Exception as e:
                logger.error(f"Unexpected WebSocket error: {e}")
                if self._is_running:
                    time.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 2, self.MAX_RECONNECT_DELAY)

    def _connect(self) -> None:
        """
        Establish WebSocket connection and start streaming.

        Raises:
            WebSocketError: If connection fails.
            SessionExpiredError: If auth tokens are invalid.
        """
        try:
            from SmartApi.smartWebSocketV2 import SmartWebSocketV2

            api_key = self._session._vault.get_credential("ANGEL_API_KEY")
            client_id = self._session.client_id
            feed_token = self._session.feed_token
            jwt_token = self._session.jwt_token

            if not feed_token or not jwt_token:
                raise SessionExpiredError(
                    "Missing feed/JWT token — session may have expired",
                    details={"has_feed_token": bool(feed_token), "has_jwt": bool(jwt_token)},
                )

            # Create WebSocket instance
            self._ws = SmartWebSocketV2(
                auth_token=jwt_token,
                api_key=api_key,
                client_code=client_id,
                feed_token=feed_token,
            )

            # Register callbacks
            self._ws.on_open = self._on_open
            self._ws.on_data = self._on_data
            self._ws.on_error = self._on_error
            self._ws.on_close = self._on_close

            self._ws.connect()

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

    # ── WebSocket Callbacks ──────────────────────────────

    def _on_open(self, ws: Any) -> None:
        """Called when WebSocket connection is established."""
        self._is_connected = True
        self._connected_at = datetime.now(IST)

        logger.info("WebSocket connected")

        # Subscribe to any pending tokens
        with self._sub_lock:
            if self._pending_subscribes:
                self._do_subscribe(list(self._pending_subscribes))
                self._pending_subscribes.clear()

            # Re-subscribe to previously active tokens (on reconnect)
            if self._subscribed_tokens:
                self._do_subscribe(list(self._subscribed_tokens))

    def _on_data(self, ws: Any, message: Any) -> None:
        """Called when tick data is received."""
        try:
            tick = self._parse_tick(message)
            if tick is None:
                return

            self._ticks_received += 1
            self._last_tick_at = datetime.now(IST)

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

    def _on_error(self, ws: Any, error: Any) -> None:
        """Called on WebSocket error."""
        error_msg = str(error) if error else "Unknown error"
        logger.error(f"WebSocket error: {error_msg}")
        self._is_connected = False

    def _on_close(self, ws: Any, close_status_code: Any, close_msg: Any) -> None:
        """Called when WebSocket connection is closed."""
        self._is_connected = False
        logger.info(f"WebSocket closed | code={close_status_code} | msg={close_msg}")

    # ── Internal Operations ──────────────────────────────

    def _do_subscribe(self, tokens: list[str]) -> None:
        """Send subscribe request to WebSocket."""
        if not tokens or not self._ws:
            return

        try:
            # SmartAPI subscription format
            token_list = [
                {
                    "exchangeType": 1,  # NSE
                    "tokens": tokens,
                }
            ]
            self._ws.subscribe(
                correlation_id="trend_investorr",
                mode=self._feed_mode.value,
                token_list=token_list,
            )
            self._subscribed_tokens.update(tokens)
            logger.debug(f"Subscribed to {len(tokens)} tokens via WebSocket")

        except Exception as e:
            logger.error(f"WebSocket subscribe failed: {e}")

    def _do_unsubscribe(self, tokens: list[str]) -> None:
        """Send unsubscribe request to WebSocket."""
        if not tokens or not self._ws:
            return

        try:
            token_list = [
                {
                    "exchangeType": 1,
                    "tokens": tokens,
                }
            ]
            self._ws.unsubscribe(
                correlation_id="trend_investorr",
                mode=self._feed_mode.value,
                token_list=token_list,
            )
            self._subscribed_tokens -= set(tokens)
            logger.debug(f"Unsubscribed from {len(tokens)} tokens")

        except Exception as e:
            logger.error(f"WebSocket unsubscribe failed: {e}")

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
    def subscribed_count(self) -> int:
        return len(self._subscribed_tokens)

    @property
    def buffer_size(self) -> int:
        return len(self._tick_buffer)

    @property
    def stats(self) -> dict[str, Any]:
        """Connection statistics for monitoring."""
        return {
            "is_connected": self._is_connected,
            "is_running": self._is_running,
            "subscribed_tokens": len(self._subscribed_tokens),
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
            f"tokens={len(self._subscribed_tokens)}, "
            f"ticks={self._ticks_received})"
        )
