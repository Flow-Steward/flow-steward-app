"""Canonical strong-confirmation token rendering for reviewed CLI plans."""

from __future__ import annotations

from collections.abc import Mapping


def confirmation_token(policy: str | None, values: Mapping[str, object]) -> str:
    raw = str(policy or "")
    if not raw.startswith("match:"):
        return ""
    keys = tuple(key.strip() for key in raw.removeprefix("match:").split(",") if key.strip())
    rendered = tuple(str(values.get(key) or "").strip() for key in keys)
    if not rendered or any(not value for value in rendered):
        return ""
    return rendered[0] if len(rendered) == 1 else "|".join(rendered)


__all__ = ["confirmation_token"]
