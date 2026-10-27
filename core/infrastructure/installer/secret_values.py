"""Installer secret-value classification owned by infrastructure adapters."""

from __future__ import annotations

import re

_PLACEHOLDER_EXACT_VALUES = frozenset(
    {
        "change_me",
        "change_me_32_plus_characters",
        "change_me_bootstrap_token",
        "change_me_metrics_token",
        "change_me_strong_password",
        "changeme",
        "default_pii_salt_change_me",
        "local_dev_secret_not_for_production",
        "replace_me",
        "replace_with_secure_token",
        "your_password",
        "your_secret",
    }
)


def is_missing_or_placeholder_secret(value: object) -> bool:
    """Return true for empty and documented example-only secret values."""
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    return not normalized or normalized in _PLACEHOLDER_EXACT_VALUES
