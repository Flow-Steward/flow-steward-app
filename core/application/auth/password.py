"""Password hashing and token hashing utilities."""

from __future__ import annotations

import hashlib
import hmac
import secrets

from core.application.auth.config import AUTH_PASSWORD_ITERATIONS


def password_hash(password: str, *, salt_hex: str | None = None) -> str:
    password_text = str(password or "")
    if not password_text:
        raise ValueError("password is required")
    normalized_salt = str(salt_hex).strip().lower() if salt_hex else secrets.token_hex(16)
    salt_bytes = bytes.fromhex(normalized_salt)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password_text.encode("utf-8"),
        salt_bytes,
        AUTH_PASSWORD_ITERATIONS,
    ).hex()
    return f"pbkdf2_sha256${AUTH_PASSWORD_ITERATIONS}${normalized_salt}${digest}"


def verify_password(password: str, encoded_hash: str) -> bool:
    encoded = str(encoded_hash or "").strip()
    parts = encoded.split("$")
    if len(parts) != 4:
        return False
    algo, iterations_text, salt_hex, expected_hex = parts
    if algo != "pbkdf2_sha256":
        return False
    try:
        iterations = max(100_000, int(iterations_text))
    except (TypeError, ValueError):
        return False
    try:
        actual_hex = hashlib.pbkdf2_hmac(
            "sha256",
            str(password or "").encode("utf-8"),
            bytes.fromhex(salt_hex),
            iterations,
        ).hex()
    except Exception:
        return False
    return hmac.compare_digest(actual_hex, expected_hex)


def session_token_hash(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def password_reset_token_hash(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()
