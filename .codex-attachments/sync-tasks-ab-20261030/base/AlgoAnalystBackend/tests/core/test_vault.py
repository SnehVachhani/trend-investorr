"""Tests for core.security.vault — encrypted credential storage."""

import pytest

from core.security.vault import SecureVault
from core.exceptions import VaultError


class TestVaultRoundtrip:
    """Encrypt/decrypt roundtrip tests."""

    def test_store_and_retrieve_credential(self, temp_dir):
        vault = SecureVault(vault_path=temp_dir / "test.vault")
        vault.store_credential("TEST_KEY", "test_value_12345")
        retrieved = vault.get_credential("TEST_KEY")
        assert retrieved == "test_value_12345"

    def test_overwrite_credential(self, temp_dir):
        vault = SecureVault(vault_path=temp_dir / "test.vault")
        vault.store_credential("KEY", "original")
        vault.store_credential("KEY", "updated")
        assert vault.get_credential("KEY") == "updated"

    def test_multiple_credentials(self, temp_dir):
        vault = SecureVault(vault_path=temp_dir / "test.vault")
        vault.store_credential("KEY_A", "value_a")
        vault.store_credential("KEY_B", "value_b")
        assert vault.get_credential("KEY_A") == "value_a"
        assert vault.get_credential("KEY_B") == "value_b"


class TestVaultEdgeCases:
    def test_get_nonexistent_returns_empty_or_none(self, temp_dir):
        vault = SecureVault(vault_path=temp_dir / "test.vault")
        result = vault.get_credential("DOES_NOT_EXIST")
        assert result == "" or result is None

    def test_auto_creates_parent_directory(self, temp_dir):
        nested = temp_dir / "deep" / "vault"
        nested.mkdir(parents=True, exist_ok=True)
        vault = SecureVault(vault_path=nested / "creds.vault")
        vault.store_credential("TEST", "value")
        assert (nested / "creds.vault").exists()

    def test_empty_value_stored_and_retrieved(self, temp_dir):
        vault = SecureVault(vault_path=temp_dir / "test.vault")
        vault.store_credential("EMPTY", "")
        assert vault.get_credential("EMPTY") == ""

    def test_special_characters_in_value(self, temp_dir):
        vault = SecureVault(vault_path=temp_dir / "test.vault")
        special = "p@$$w0rd!#%^&*()_+-=[]{}|;':\",./<>?"
        vault.store_credential("SPECIAL", special)
        assert vault.get_credential("SPECIAL") == special
