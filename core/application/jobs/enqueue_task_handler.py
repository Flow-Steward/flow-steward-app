"""The job-creating ``enqueue_task`` every process hands to the services that start runs.

Kept apart from ``management_runtime``: the worker needs only this handler, and importing
it from there pulled the whole management stack (portability, authoring, the template
catalog) into the worker image.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from functools import lru_cache
from typing import Any

from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
from core.application.extensions.artifact_storage import create_storage_adapter
from core.application.jobs.task_enqueue_runtime import build_enqueue_task_handler
from core.application.management.snapshot_serializers import build_project_config_snapshot
from core.application.workflows.domain_event_runtime import project_account_id
from core.infrastructure.config import runtime_config as core_config
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.runtime.artifact_storage_factory import build_artifact_storage

_LOGGER = logging.getLogger("platform.manage.runtime")
_SENSITIVE_KEY_RE = re.compile(
    r"(api[_-]?key|token|secret|password|passphrase|private[_-]?key|dsn)",
    re.IGNORECASE,
)


@lru_cache(maxsize=1)
def job_invocation_artifact_plane() -> ArtifactDatasetPlane:
    return build_artifact_storage(
        environ=dict(_runtime_env),
        create_storage_adapter=create_storage_adapter,
        artifact_plane_factory=ArtifactDatasetPlane,
    )


def project_config_snapshot(project_config: dict | None) -> dict[str, Any]:
    return build_project_config_snapshot(project_config, secret_key_re=_SENSITIVE_KEY_RE)


def publish_task(**kwargs) -> None:
    from core.infrastructure.runtime.worker.queue_publisher import enqueue_task

    enqueue_task(**kwargs)


def create_enqueue_task_handler(
    *, get_logger: Callable[[], Any], log_prefix: str = "platform.worker.enqueue"
) -> Callable[..., Any]:
    """``enqueue_task(task_id=..., test_mode=...)`` that creates the job row, then publishes it.

    The worker hands the event service this handler, not the raw queue publisher, so an
    event that starts a workflow (a provider trigger, a reaction) creates the job row
    first and publishes it second, the same as the API process does.
    """
    return build_enqueue_task_handler(
        get_config=lambda: core_config.CONFIG,
        create_logger_fn=lambda _dsn: get_logger(),
        enqueue_task_fn=lambda **kwargs: publish_task(**kwargs),
        build_project_config_snapshot=project_config_snapshot,
        project_account_id_fn=lambda bl, project_id: project_account_id(bl, project_id),
        logger=_LOGGER,
        log_prefix=log_prefix,
        artifact_plane=job_invocation_artifact_plane,
    )


__all__ = [
    "create_enqueue_task_handler",
    "job_invocation_artifact_plane",
    "project_config_snapshot",
    "publish_task",
]
