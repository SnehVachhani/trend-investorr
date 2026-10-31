"""
Trend-Investorr: Immutable Audit Trail Logger

Append-only, tamper-evident audit log. Each entry contains a SHA-256 hash
of the previous entry, creating a chain that detects any modifications.

Used for:
- Every trade/order placed
- Authentication events
- Config changes
- Circuit breaker state changes
- System start/shutdown
"""

from __future__ import annotations

import hashlib
import json
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from core.constants import AuditEventType, IST


class AuditEntry:
    """A single immutable audit log entry."""

    __slots__ = (
        "timestamp", "event_type", "details", "ip_address",
        "checksum", "prev_checksum",
    )

    def __init__(
        self,
        event_type: AuditEventType,
        details: dict[str, Any],
        prev_checksum: str = "",
        ip_address: str = "",
    ):
        self.timestamp = datetime.now(IST).isoformat()
        self.event_type = event_type.value
        self.details = details
        self.ip_address = ip_address or self._get_local_ip()
        self.prev_checksum = prev_checksum

        # Chain hash: SHA-256 of (prev_checksum + timestamp + event_type + details)
        hash_input = f"{prev_checksum}{self.timestamp}{self.event_type}{json.dumps(details, sort_keys=True)}"
        self.checksum = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "details": self.details,
            "ip_address": self.ip_address,
            "checksum": self.checksum,
            "prev_checksum": self.prev_checksum,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)

    @staticmethod
    def _get_local_ip() -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"


class AuditTrail:
    """
    Append-only audit trail with chain hashing.

    Writes to both:
    1. A local JSONL file (one JSON entry per line)
    2. The database (via a callback, injected later to avoid circular deps)

    The chain hash ensures any tampering with historical entries is detectable
    by verifying the hash chain from the first entry forward.
    """

    def __init__(self, log_dir: Path, db_writer: Any = None):
        """
        Args:
            log_dir: Directory where audit log files are written.
            db_writer: Optional callable(entry_dict) to persist to database.
                       Injected after database module is initialized.
        """
        self._log_dir = log_dir
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            logger.error(
                f"Permission denied creating audit log directory: {log_dir}. "
                "Audit logs will only write to database (if available)."
            )
        except OSError as e:
            logger.error(f"Failed to create audit log directory: {e}")
        self._db_writer = db_writer
        self._last_checksum = self._load_last_checksum()

    def _get_log_file(self) -> Path:
        """Get today's audit log file path."""
        today = datetime.now(IST).strftime("%Y-%m-%d")
        return self._log_dir / f"audit_{today}.jsonl"

    def _load_last_checksum(self) -> str:
        """Load the last checksum from the most recent audit file."""
        log_file = self._get_log_file()
        if not log_file.exists():
            return ""

        try:
            lines = log_file.read_text(encoding="utf-8").strip().split("\n")
            if lines and lines[-1]:
                last_entry = json.loads(lines[-1])
                return last_entry.get("checksum", "")
        except (json.JSONDecodeError, IndexError, OSError):
            logger.warning("Could not load last audit checksum — starting fresh chain")

        return ""

    def log(self, event_type: AuditEventType, details: dict[str, Any] | None = None) -> AuditEntry:
        """
        Log an audit event.

        Args:
            event_type: The type of event (from AuditEventType enum).
            details: Additional context for the event.

        Returns:
            The created AuditEntry with its chain checksum.
        """
        details = details or {}

        # Redact sensitive fields
        safe_details = self._redact_sensitive(details)

        entry = AuditEntry(
            event_type=event_type,
            details=safe_details,
            prev_checksum=self._last_checksum,
        )

        # Write to file
        try:
            log_file = self._get_log_file()
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(entry.to_json() + "\n")
        except OSError as e:
            logger.error(f"Failed to write audit log to file: {e}")

        # Write to database (if writer injected)
        if self._db_writer:
            try:
                self._db_writer(entry.to_dict())
            except Exception as e:
                logger.error(f"Failed to write audit log to DB: {e}")

        # Update chain
        self._last_checksum = entry.checksum

        # Also log to application logger for observability
        logger.info(f"AUDIT | {entry.event_type} | {json.dumps(safe_details, default=str)}")

        return entry

    def set_db_writer(self, writer: Any) -> None:
        """Inject database writer after DB initialization."""
        self._db_writer = writer
        logger.debug("Audit trail DB writer injected")

    def verify_chain(self, log_file: Path | None = None) -> tuple[bool, int, str]:
        """
        Verify the integrity of the audit chain in a log file.

        Returns:
            (is_valid, entries_checked, error_message)
        """
        log_file = log_file or self._get_log_file()
        if not log_file.exists():
            return True, 0, ""

        try:
            lines = log_file.read_text(encoding="utf-8").strip().split("\n")
            entries = [json.loads(line) for line in lines if line.strip()]
        except (json.JSONDecodeError, OSError) as e:
            return False, 0, f"Failed to read audit file: {e}"

        if not entries:
            return True, 0, ""

        prev_checksum = ""
        for i, entry in enumerate(entries):
            try:
                # Recompute the expected checksum
                hash_input = (
                    f"{prev_checksum}{entry['timestamp']}{entry['event_type']}"
                    f"{json.dumps(entry['details'], sort_keys=True)}"
                )
                expected = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()

                if entry["checksum"] != expected:
                    return False, i, f"Chain broken at entry {i}: expected {expected[:16]}..., got {entry['checksum'][:16]}..."

                if entry["prev_checksum"] != prev_checksum:
                    return False, i, f"prev_checksum mismatch at entry {i}"

                prev_checksum = entry["checksum"]
            except KeyError as e:
                return False, i, f"Malformed audit entry at index {i}: missing field {e}"

        return True, len(entries), ""

    @staticmethod
    def _redact_sensitive(details: dict[str, Any]) -> dict[str, Any]:
        """Redact any sensitive values from audit details."""
        sensitive_keys = {
            "password", "api_key", "totp_secret", "jwt_token",
            "refresh_token", "feed_token", "secret", "pin",
        }
        redacted = {}
        for key, value in details.items():
            lower_key = key.lower()
            if any(s in lower_key for s in sensitive_keys):
                redacted[key] = "***REDACTED***"
            else:
                redacted[key] = value
        return redacted
