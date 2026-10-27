"""Infrastructure ports used by worker-side provider event ingress."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class WorkerEventIngressPorts:
    dispatch_project_notification_event: Callable[..., Any]
    fetch_provider_attachments: Callable[..., Any]
    enqueue_task: Callable[..., Any]


__all__ = ["WorkerEventIngressPorts"]
