"""
Trend-Investorr: TOTP Manager

Handles Time-based One-Time Password generation for Angel One SmartAPI
authentication. SmartAPI requires TOTP as part of the 2FA login flow.

Usage:
    totp_mgr = TOTPManager(secret="BASE32_SECRET_FROM_ANGEL_ONE")
    current_otp = totp_mgr.generate()  # e.g., "482901"
"""

from __future__ import annotations

from loguru import logger
import pyotp

from core.exceptions import TOTPGenerationError


class TOTPManager:
    """
    TOTP generator for SmartAPI 2FA authentication.

    Angel One requires a TOTP token during every login/session creation.
    The TOTP secret is provided when you enable 2FA in the Angel One app.
    """

    def __init__(self, secret: str):
        """
        Initialize with the TOTP secret key.

        Args:
            secret: Base32-encoded TOTP secret from Angel One.
        """
        if not secret:
            raise TOTPGenerationError("TOTP secret is empty — cannot generate tokens")

        self._secret = secret
        try:
            self._totp = pyotp.TOTP(secret)
        except Exception as e:
            raise TOTPGenerationError(
                f"Invalid TOTP secret format: {e}",
                details={"error": str(e)},
            )

    def generate(self) -> str:
        """
        Generate the current TOTP token.

        Returns:
            6-digit TOTP string (e.g., "482901")

        Raises:
            TOTPGenerationError: If token generation fails.
        """
        try:
            token = self._totp.now()
            logger.debug("TOTP token generated successfully")
            return token
        except Exception as e:
            raise TOTPGenerationError(
                f"Failed to generate TOTP: {e}",
                details={"error": str(e)},
            )

    def verify(self, token: str, valid_window: int = 1) -> bool:
        """
        Verify a TOTP token against the current time.

        Args:
            token: The 6-digit token to verify.
            valid_window: Number of 30-second windows to check
                          before and after current time (handles clock drift).
                          Default 1 = ±30 seconds tolerance.

        Returns:
            True if the token is valid.
        """
        try:
            return self._totp.verify(token, valid_window=valid_window)
        except Exception:
            return False

    @property
    def interval(self) -> int:
        """TOTP refresh interval in seconds (always 30 for Angel One)."""
        return self._totp.interval

    def time_remaining(self) -> int:
        """Seconds remaining before the current TOTP token expires."""
        import time
        return self._totp.interval - (int(time.time()) % self._totp.interval)
