"""Shared terminal-only secret input for CLI service adapters."""

from __future__ import annotations

import getpass


def read_hidden_secret(prompt: str) -> bytes:
    """Read one hidden terminal value without adding it to a command model."""
    return getpass.getpass(prompt).encode()


__all__ = ["read_hidden_secret"]
