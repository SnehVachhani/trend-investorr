"""
Trend-Investorr: Secure Credential Vault

Fernet symmetric encryption for storing API credentials at rest.
Credentials are encrypted on first run and never stored as plaintext again.

Architecture:
    .env (plaintext, first run only)
        ↓ encrypt_and_store()
    .vault (Fernet-encrypted JSON file)
        ↓ get_credential()
    In-memory use → never logged, never serialized
"""

from __future__ import annotations

import hashlib
import json
import platform
import uuid
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from loguru import logger

from core.exceptions import VaultError


class SecureVault:
    """
    Encrypted credential storage using Fernet symmetric encryption.

    The master key is derived from either:
    1. An explicit VAULT_MASTER_KEY env var (recommended for production/cloud)
    2. Machine-specific entropy (hostname + MAC address) for local development

    Credentials are stored as an encrypted JSON blob in a .vault file.
    """

    def __init__(self, vault_path: Path, master_key: str = ""):
        self._vault_path = vault_path
        self._fernet = self._create_fernet(master_key)
        self._cache: dict[str, str] = {}  # In-memory decrypted cache
        self._loaded = False

    def _create_fernet(self, master_key: str) -> Fernet:
        """Create Fernet cipher from master key or machine entropy."""
        try:
            if master_key:
                # Use provided master key — hash it to get exactly 32 bytes
                key_bytes = hashlib.sha256(master_key.encode()).digest()
            else:
                # Derive from machine-specific entropy (dev mode)
                machine_id = f"{platform.node()}-{uuid.getnode()}"
                key_bytes = hashlib.sha256(machine_id.encode()).digest()
                logger.warning(
                    "Vault using machine-derived key (dev mode). "
                    "Set VAULT_MASTER_KEY in .env for production."
                )

            # Fernet requires URL-safe base64-encoded 32-byte key
            import base64
            fernet_key = base64.urlsafe_b64encode(key_bytes)
            return Fernet(fernet_key)
        except Exception as e:
            raise VaultError(
                f"Failed to initialize encryption engine: {e}",
                details={"error": str(e)},
            )

    def _load(self) -> None:
        """Load and decrypt vault file into memory cache."""
        if self._loaded:
            return

        if not self._vault_path.exists():
            self._cache = {}
            self._loaded = True
            return

        try:
            encrypted_data = self._vault_path.read_bytes()
            decrypted_json = self._fernet.decrypt(encrypted_data)
            self._cache = json.loads(decrypted_json.decode("utf-8"))
            self._loaded = True
            logger.info(f"Vault loaded: {len(self._cache)} credentials decrypted")
        except InvalidToken:
            raise VaultError(
                "Failed to decrypt vault — master key may have changed. "
                "Delete .vault file and re-run setup if you changed machines."
            )
        except json.JSONDecodeError:
            raise VaultError("Vault file is corrupted — invalid JSON after decryption.")

    def _save(self) -> None:
        """Encrypt and save the in-memory cache to disk.

        Raises:
            VaultError: If the file cannot be written (permissions, disk full, etc.).
        """
        try:
            json_bytes = json.dumps(self._cache, indent=2).encode("utf-8")
            encrypted_data = self._fernet.encrypt(json_bytes)
            self._vault_path.write_bytes(encrypted_data)
            logger.debug(f"Vault saved: {len(self._cache)} credentials encrypted")
        except PermissionError:
            raise VaultError(
                f"Permission denied writing vault file: {self._vault_path}. "
                "Check file/directory permissions."
            )
        except OSError as e:
            raise VaultError(
                f"Failed to write vault file: {e}",
                details={"path": str(self._vault_path), "error": str(e)},
            )

    def get_credential(self, key: str) -> str | None:
        """
        Get a decrypted credential by key.

        Returns None if the key doesn't exist in the vault.
        """
        self._load()
        return self._cache.get(key)

    def store_credential(self, key: str, value: str) -> None:
        """
        Encrypt and store a credential.

        Overwrites if key already exists.
        """
        self._load()
        self._cache[key] = value
        self._save()
        logger.info(f"Credential stored: '{key}' (value redacted)")

    def store_many(self, credentials: dict[str, str]) -> None:
        """Store multiple credentials at once (single write)."""
        self._load()
        self._cache.update(credentials)
        self._save()
        logger.info(f"Stored {len(credentials)} credentials: {list(credentials.keys())}")

    def has_credential(self, key: str) -> bool:
        """Check if a credential exists without returning its value."""
        self._load()
        return key in self._cache

    def remove_credential(self, key: str) -> bool:
        """Remove a credential. Returns True if it existed."""
        self._load()
        if key in self._cache:
            del self._cache[key]
            self._save()
            logger.info(f"Credential removed: '{key}'")
            return True
        return False

    def list_keys(self) -> list[str]:
        """List all credential keys (values are never exposed)."""
        self._load()
        return list(self._cache.keys())

    @property
    def is_initialized(self) -> bool:
        """Check if the vault file exists and has credentials."""
        self._load()
        return len(self._cache) > 0

    def setup_from_env(self, settings: Any) -> bool:
        """
        First-run setup: read credentials from Settings (loaded from .env),
        encrypt them into the vault, and return True if successful.

        After this, the plaintext .env values are no longer needed
        (though they remain for re-initialization if vault is deleted).
        """
        credentials_to_store = {}

        if settings.ANGEL_API_KEY:
            credentials_to_store["ANGEL_API_KEY"] = settings.ANGEL_API_KEY
        if settings.ANGEL_CLIENT_ID:
            credentials_to_store["ANGEL_CLIENT_ID"] = settings.ANGEL_CLIENT_ID
        if settings.ANGEL_PASSWORD:
            credentials_to_store["ANGEL_PASSWORD"] = settings.ANGEL_PASSWORD
        if settings.ANGEL_TOTP_SECRET:
            credentials_to_store["ANGEL_TOTP_SECRET"] = settings.ANGEL_TOTP_SECRET
        if settings.GEMINI_API_KEY:
            credentials_to_store["GEMINI_API_KEY"] = settings.GEMINI_API_KEY
        if getattr(settings, "UPSTOX_ANALYTICS_TOKEN", ""):
            credentials_to_store["UPSTOX_ANALYTICS_TOKEN"] = settings.UPSTOX_ANALYTICS_TOKEN

        if not credentials_to_store:
            logger.warning("No credentials found in .env — vault will be empty")
            return False

        self.store_many(credentials_to_store)
        logger.info(
            f"Vault initialized with {len(credentials_to_store)} credentials. "
            "You can now remove plaintext values from .env (optional)."
        )
        return True
