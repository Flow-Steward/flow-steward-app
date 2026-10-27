"""Tests for auth/secrets.py — AES-256-GCM encrypt/decrypt."""

import os

import pytest
from cryptography.exceptions import InvalidTag

from core.application.auth.secrets import decrypt_secret, encrypt_secret, mask_secret


class TestEncryptDecrypt:
    def test_round_trip(self, monkeypatch):
        monkeypatch.setenv("ENCRYPTION_KEY", "unit-test-encryption-key")
        plaintext = "sk-abc123secretkey"
        ct = encrypt_secret(plaintext)
        assert decrypt_secret(ct) == plaintext

    def test_round_trip_explicit_key(self):
        key = os.urandom(32)
        plaintext = "my-api-key"
        ct = encrypt_secret(plaintext, master_key=key)
        assert decrypt_secret(ct, master_key=key) == plaintext

    def test_different_nonces(self, monkeypatch):
        monkeypatch.setenv("ENCRYPTION_KEY", "unit-test-encryption-key")
        plaintext = "same-input"
        ct1 = encrypt_secret(plaintext)
        ct2 = encrypt_secret(plaintext)
        assert ct1 != ct2  # random nonce each time

    def test_wrong_key_fails(self):
        ct = encrypt_secret("secret", master_key=os.urandom(32))
        _raises_input_33_1 = os.urandom(32)
        with pytest.raises(InvalidTag):
            decrypt_secret(ct, master_key=_raises_input_33_1)

    def test_empty_string(self, monkeypatch):
        monkeypatch.setenv("ENCRYPTION_KEY", "unit-test-encryption-key")
        ct = encrypt_secret("")
        assert decrypt_secret(ct) == ""

    def test_secret_key_fallback_is_rejected(self, monkeypatch):
        monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
        monkeypatch.delenv("FS_SECRET_KEY", raising=False)
        monkeypatch.setenv("SECRET_KEY", "fallback-secret")

        with pytest.raises(RuntimeError, match="ENCRYPTION_KEY"):
            encrypt_secret("stored-password")

    def test_unicode(self, monkeypatch):
        monkeypatch.setenv("ENCRYPTION_KEY", "unit-test-encryption-key")
        plaintext = "ключ-доступа-🔑"
        ct = encrypt_secret(plaintext)
        assert decrypt_secret(ct) == plaintext


class TestMaskSecret:
    def test_default_visible(self):
        # "sk-1234567890" = 13 chars, visible=4 → 9 stars + "7890"
        assert mask_secret("sk-1234567890") == "*********7890"

    def test_short_value(self):
        assert mask_secret("abc", visible=4) == "***"

    def test_exact_visible(self):
        # len == visible → all masked per implementation
        assert mask_secret("abcd", visible=4) == "****"

    def test_custom_visible(self):
        # 13 chars, visible=6 → 7 stars + "567890"
        assert mask_secret("sk-1234567890", visible=6) == "*******567890"
