# ruff: noqa: F403
"""Canonical public module for extension runtime helpers."""

from __future__ import annotations

from .extension_action_manifest import (
    _normalize_query_or_action_items as _normalize_query_or_action_items,
)
from .extension_action_manifest import (
    _resolve_action_manifest as _resolve_action_manifest,
)
from .extension_action_manifest import (
    _resolve_query_manifest as _resolve_query_manifest,
)
from .extension_browser_picker_contract import (
    _safe_browser_bootstrap_action_ids as _safe_browser_bootstrap_action_ids,
)
from .extension_browser_picker_contract import (
    _validate_external_resource_picker as _validate_external_resource_picker,
)
from .extension_runtime_common import *  # NOSONAR
from .extension_runtime_common import __all__ as _common_exports
from .extension_runtime_compile import *  # NOSONAR
from .extension_runtime_compile import __all__ as _compile_exports
from .extension_runtime_entrypoints import *  # NOSONAR
from .extension_runtime_entrypoints import __all__ as _entrypoint_exports
from .extension_runtime_feature_validation import *  # NOSONAR
from .extension_runtime_feature_validation import __all__ as _feature_validation_exports
from .extension_runtime_invocation import *  # NOSONAR
from .extension_runtime_invocation import __all__ as _invocation_exports
from .extension_runtime_manifest import *  # NOSONAR
from .extension_runtime_manifest import __all__ as _manifest_exports
from .extension_runtime_platform import *  # NOSONAR
from .extension_runtime_platform import __all__ as _platform_exports
from .extension_runtime_ui_manifest import *  # NOSONAR
from .extension_runtime_ui_manifest import __all__ as _ui_manifest_exports
from .extension_runtime_validation import *  # NOSONAR
from .extension_runtime_validation import __all__ as _validation_exports

__all__ = list(
    dict.fromkeys(
        [
            *_common_exports,
            *_validation_exports,
            *_manifest_exports,
            *_ui_manifest_exports,
            "_normalize_query_or_action_items",
            "_resolve_query_manifest",
            "_resolve_action_manifest",
            "_safe_browser_bootstrap_action_ids",
            "_validate_external_resource_picker",
            *_feature_validation_exports,
            *_compile_exports,
            *_invocation_exports,
            *_platform_exports,
            *_entrypoint_exports,
        ]
    )
)
