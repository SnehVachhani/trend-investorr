"""
Trend-Investorr: SmartAPI Authentication & Session Manager

Handles the complete Angel One SmartAPI authentication lifecycle:
    1. Login with client_id + password + TOTP
    2. Receive JWT token + refresh token + feed token
    3. Auto-renew session before expiry
    4. Auto-relogin on session expiration (midnight daily logout)
    5. Graceful cleanup on shutdown

The session manager is the ONLY module that touches SmartAPI auth directly.
All other modules receive an authenticated SmartConnect instance.

Raises:
    AuthenticationError: On login failure (bad credentials, TOTP expired).
    SessionExpiredError: When session needs renewal and auto-relogin fails.
"""

from __future__ import annotations

import time as time_module
from datetime import datetime, time
from threading import Lock
from typing import Any

from loguru import logger

from core.constants import (
    AuditEventType,
    IST,
    SMARTAPI_SESSION_EXPIRY_HOUR,
)
from core.exceptions import (
    AuthenticationError,
    SessionExpiredError,
    RateLimitError,
    APIError,
)
from core.security.audit import AuditTrail
from core.security.totp_manager import TOTPManager
from core.security.vault import SecureVault


class SmartAPISession:
    """
    Manages a single Angel One SmartAPI session.

    Thread-safe: uses a lock for session state mutations.
    The SmartConnect SDK handles the underlying HTTP/WSS connections.
    """

    def __init__(
        self,
        vault: SecureVault,
        audit: AuditTrail,
    ):
        """
        Initialize the session manager.

        Args:
            vault: SecureVault for reading encrypted credentials.
            audit: AuditTrail for logging auth events.

        Raises:
            AuthenticationError: If required credentials are missing from vault.
        """
        self._vault = vault
        self._audit = audit
        self._lock = Lock()

        # Session state
        self._smart_connect: Any = None  # SmartConnect instance
        self._jwt_token: str = ""
        self._refresh_token: str = ""
        self._feed_token: str = ""
        self._is_authenticated: bool = False
        self._last_login_at: datetime | None = None
        self._login_count: int = 0

        # Load and validate credentials from vault
        self._api_key = self._vault.get_credential("ANGEL_API_KEY")
        self._client_id = self._vault.get_credential("ANGEL_CLIENT_ID")
        self._password = self._vault.get_credential("ANGEL_PASSWORD")
        self._totp_secret = self._vault.get_credential("ANGEL_TOTP_SECRET")

        self._validate_credentials()
        self._totp = TOTPManager(self._totp_secret)

    def _validate_credentials(self) -> None:
        """Verify all required credentials are present in the vault.

        Raises:
            AuthenticationError: If any required credential is missing.
        """
        missing = []
        if not self._api_key:
            missing.append("ANGEL_API_KEY")
        if not self._client_id:
            missing.append("ANGEL_CLIENT_ID")
        if not self._password:
            missing.append("ANGEL_PASSWORD")
        if not self._totp_secret:
            missing.append("ANGEL_TOTP_SECRET")

        if missing:
            raise AuthenticationError(
                f"Missing SmartAPI credentials in vault: {missing}. "
                "Run vault setup first or check your .env file.",
                details={"missing_keys": missing},
            )

    def login(self) -> bool:
        """
        Authenticate with Angel One SmartAPI.

        Generates a TOTP token, calls SmartConnect.generateSession(),
        and stores the JWT + feed tokens in memory.

        Returns:
            True if login succeeded.

        Raises:
            AuthenticationError: On login failure (bad creds, network, TOTP).
        """
        with self._lock:
            try:
                # Import here to avoid hard dependency during testing
                from SmartApi import SmartConnect

                # Create fresh SmartConnect instance
                self._smart_connect = SmartConnect(api_key=self._api_key)

                # Generate TOTP token
                totp_token = self._totp.generate()

                logger.info(
                    f"Logging in to SmartAPI | client_id={self._client_id} | "
                    f"TOTP expires in {self._totp.time_remaining()}s"
                )

                # Authenticate
                session_data = self._smart_connect.generateSession(
                    clientCode=self._client_id,
                    password=self._password,
                    totp=totp_token,
                )

                if not session_data or session_data.get("status") is False:
                    error_msg = session_data.get("message", "Unknown login error") if session_data else "No response"
                    self._audit.log(
                        AuditEventType.LOGIN_FAILURE,
                        {"client_id": self._client_id, "error": error_msg},
                    )
                    raise AuthenticationError(
                        f"SmartAPI login failed: {error_msg}",
                        details={"client_id": self._client_id, "response": str(session_data)},
                    )

                # Extract tokens
                data = session_data.get("data", {})
                self._jwt_token = data.get("jwtToken", "")
                self._refresh_token = data.get("refreshToken", "")
                self._feed_token = self._smart_connect.getfeedToken()

                if not self._jwt_token:
                    raise AuthenticationError(
                        "Login succeeded but no JWT token received",
                        details={"session_data_keys": list(data.keys())},
                    )

                self._is_authenticated = True
                self._last_login_at = datetime.now(IST)
                self._login_count += 1

                self._audit.log(
                    AuditEventType.LOGIN_SUCCESS,
                    {
                        "client_id": self._client_id,
                        "login_count": self._login_count,
                        "has_feed_token": bool(self._feed_token),
                    },
                )

                logger.info(
                    f"SmartAPI login successful | "
                    f"login_count={self._login_count} | "
                    f"feed_token={'✓' if self._feed_token else '✗'}"
                )
                return True

            except AuthenticationError:
                raise  # Re-raise our own exceptions

            except ImportError:
                raise AuthenticationError(
                    "SmartApi package not installed. Run: pip install smartapi-python",
                    details={"package": "smartapi-python"},
                )

            except Exception as e:
                error_msg = f"Unexpected error during SmartAPI login: {e}"
                self._audit.log(
                    AuditEventType.LOGIN_FAILURE,
                    {"client_id": self._client_id, "error": str(e), "type": type(e).__name__},
                )
                raise AuthenticationError(
                    error_msg,
                    details={"original_error": type(e).__name__, "message": str(e)},
                )

    def ensure_authenticated(self) -> None:
        """
        Ensure the session is active. Re-login if expired.

        SmartAPI sessions expire at midnight IST daily.
        This method checks and refreshes proactively.

        Raises:
            SessionExpiredError: If re-login also fails.
        """
        with self._lock:
            if not self._is_authenticated:
                pass  # Will login below
            elif self._is_session_expired():
                logger.warning("SmartAPI session expired — attempting re-login")
                self._is_authenticated = False
                self._audit.log(
                    AuditEventType.SESSION_EXPIRED,
                    {"client_id": self._client_id, "last_login": str(self._last_login_at)},
                )
            else:
                return  # Session is valid

        # Need to login (or re-login)
        try:
            self.login()
            if self._login_count > 1:
                self._audit.log(
                    AuditEventType.SESSION_RENEWED,
                    {"client_id": self._client_id, "login_count": self._login_count},
                )
        except AuthenticationError as e:
            raise SessionExpiredError(
                f"Session expired and re-login failed: {e}",
                details=e.details,
            )

    def _is_session_expired(self) -> bool:
        """Check if the current session has expired.

        SmartAPI sessions expire at midnight IST daily.
        We check slightly before (11:55 PM) to avoid edge cases.
        """
        if not self._last_login_at:
            return True

        now = datetime.now(IST)

        # Session expired if login was on a different date
        if self._last_login_at.date() != now.date():
            return True

        # Proactive expiry check: treat as expired after 11:55 PM
        if now.time() >= time(23, 55):
            return True

        return False

    def get_api(self) -> Any:
        """
        Get the authenticated SmartConnect instance.

        Ensures the session is valid before returning.

        Returns:
            Authenticated SmartConnect instance.

        Raises:
            SessionExpiredError: If session cannot be established.
        """
        self.ensure_authenticated()
        return self._smart_connect

    def logout(self) -> None:
        """Gracefully terminate the SmartAPI session."""
        with self._lock:
            if self._smart_connect and self._is_authenticated:
                try:
                    self._smart_connect.terminateSession(self._client_id)
                    logger.info("SmartAPI session terminated gracefully")
                except Exception as e:
                    logger.warning(f"Error during SmartAPI logout (non-critical): {e}")
                finally:
                    self._is_authenticated = False
                    self._jwt_token = ""
                    self._refresh_token = ""
                    self._feed_token = ""

    # ── Accessors (never expose raw credentials) ─────────

    @property
    def is_authenticated(self) -> bool:
        return self._is_authenticated

    @property
    def client_id(self) -> str:
        return self._client_id or ""

    @property
    def feed_token(self) -> str:
        """Feed token for WebSocket connections."""
        return self._feed_token

    @property
    def jwt_token(self) -> str:
        """JWT token for REST API calls (in-memory only)."""
        return self._jwt_token

    @property
    def last_login_at(self) -> datetime | None:
        return self._last_login_at

    @property
    def login_count(self) -> int:
        return self._login_count

    def __repr__(self) -> str:
        return (
            f"SmartAPISession(client_id='{self._client_id}', "
            f"authenticated={self._is_authenticated}, "
            f"logins={self._login_count})"
        )

    def __del__(self):
        """Cleanup on garbage collection."""
        try:
            self.logout()
        except Exception:
            pass
