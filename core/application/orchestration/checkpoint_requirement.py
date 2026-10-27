"""A workflow run never executes without its LangGraph checkpoint.

Approval, Wait, durable child invocations, retries and every resume continue
from the checkpoint; a run that started without one would pause into nothing
or repeat effects after a restart. A missing or failing checkpointer is
therefore a typed refusal before the first step, never a silent fallback.
Only a deliberate compile-only build passes ``checkpointer=False``.
"""

from __future__ import annotations

CHECKPOINT_UNAVAILABLE = "checkpoint_unavailable"


class CheckpointUnavailableError(RuntimeError):
    """The run's checkpoint storage cannot be used; nothing was executed."""

    error_code = CHECKPOINT_UNAVAILABLE

    def __init__(self, reason: str) -> None:
        # The reason names what failed, never the connection string.
        super().__init__(f"Workflow checkpoint storage is unavailable: {reason}")
        self.reason = reason


__all__ = ["CHECKPOINT_UNAVAILABLE", "CheckpointUnavailableError"]
