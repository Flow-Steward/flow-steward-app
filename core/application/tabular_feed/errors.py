"""Domain errors for first-party tabular feed orchestration."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TabularFeedError:
    code: str
    message: str
    path: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "path": self.path}


class TabularFeedExecutionError(RuntimeError):
    """Raised when core orchestration cannot safely invoke tabular processing."""

    def __init__(self, error: TabularFeedError) -> None:
        super().__init__(error.message)
        self.error = error
