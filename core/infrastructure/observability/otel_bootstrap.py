"""Import-time application of the OpenTelemetry environment policy.

Entrypoints import this module first (with ``# isort: split``) so that the
policy runs before any library reads ``OTEL_*`` at import. It is idempotent;
``init_telemetry`` applies it again for processes started another way.
"""

from __future__ import annotations

from core.infrastructure.observability.otel_environment import apply_environment_policy

apply_environment_policy()
