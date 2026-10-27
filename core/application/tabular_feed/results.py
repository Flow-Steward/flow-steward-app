"""Result models for first-party tabular feed execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TabularFeedExecutionResult:
    ok: bool
    result: dict[str, Any] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)
    commands: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    reload: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_envelope(cls, envelope: dict[str, Any]) -> TabularFeedExecutionResult:
        return cls(
            ok=bool(envelope.get("ok")),
            result=dict(envelope.get("result") or {}),
            errors=list(envelope.get("errors") or []),
            commands=list(envelope.get("commands") or []),
            events=list(envelope.get("events") or []),
            reload=list(envelope.get("reload") or []),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "result": self.result,
            "errors": self.errors,
            "commands": self.commands,
            "events": self.events,
            "reload": self.reload,
        }
