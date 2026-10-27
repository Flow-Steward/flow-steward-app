from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

try:
    import pika
    import pika.exceptions
except ImportError:  # pragma: no cover - exercised by local unit tests without RabbitMQ deps.

    class _MissingPikaConnectionError(Exception):
        pass

    class _MissingPikaURLParameters:
        def __init__(self, url: str):
            self.url = url
            self.heartbeat: int | None = None
            self.blocked_connection_timeout: int | None = None

    class _MissingPikaBasicProperties:
        def __init__(self, **kwargs: Any):
            self.kwargs = dict(kwargs)
            for key, value in kwargs.items():
                setattr(self, key, value)

    def _missing_blocking_connection(*_args: Any, **_kwargs: Any) -> Any:
        raise _MissingPikaConnectionError("RabbitMQ client dependency 'pika' is not installed")

    pika = ModuleType("pika")
    pika.URLParameters = _MissingPikaURLParameters  # type: ignore[attr-defined]
    pika.BasicProperties = _MissingPikaBasicProperties  # type: ignore[attr-defined]
    pika.BlockingConnection = _missing_blocking_connection  # type: ignore[attr-defined]
    pika_exceptions = ModuleType("pika.exceptions")
    pika_exceptions.AMQPConnectionError = _MissingPikaConnectionError  # type: ignore[attr-defined]
    pika.exceptions = pika_exceptions  # type: ignore[attr-defined]
    sys.modules.setdefault("pika", pika)
    sys.modules.setdefault("pika.exceptions", pika_exceptions)
