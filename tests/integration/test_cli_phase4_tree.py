"""Frozen Phase 4 lifecycle command inventory."""

from __future__ import annotations

from core.application.management.command_specs.registry import PUBLIC_COMMAND_SPECS
from core.entrypoints.cli.platform.renderers import RENDERERS
from core.entrypoints.cli.platform.service_registry import (
    CHOICE_PROVIDERS,
    PLAN_BUILDERS,
    SERVICE_FACTORIES,
)

PHASE4_LEAVES: dict[str, tuple[str, ...]] = {
    "stack": ("status", "start", "stop", "restart", "rebuild", "logs"),
    "environment": ("show", "switch"),
    "release": ("info", "check", "upgrade"),
    "backup": ("create", "list", "verify"),
    "config": ("show", "validate", "set", "apply"),
}


def test_phase4_public_lifecycle_inventory_is_exact() -> None:
    """Only the approved lifecycle leaves may be added to the public CLI."""
    actual: dict[str, tuple[str, ...]] = {}
    for domain in PHASE4_LEAVES:
        actual[domain] = tuple(
            spec.path[1]
            for spec in PUBLIC_COMMAND_SPECS
            if len(spec.path) == 2 and spec.path[0] == domain
        )

    assert actual == PHASE4_LEAVES


def test_phase4_leaves_have_no_placeholder_or_unregistered_references() -> None:
    specs = tuple(spec for spec in PUBLIC_COMMAND_SPECS if spec.path[0] in PHASE4_LEAVES)

    assert len(specs) == sum(len(leaves) for leaves in PHASE4_LEAVES.values())
    assert all(spec.service_ref in SERVICE_FACTORIES for spec in specs)
    assert all(spec.result_renderer_ref in RENDERERS for spec in specs)
    assert all(
        field.choice_provider is None or field.choice_provider in CHOICE_PROVIDERS
        for spec in specs
        for field in spec.fields
    )
    assert all(
        spec.plan_builder_ref is None or spec.plan_builder_ref in PLAN_BUILDERS for spec in specs
    )
    assert all("placeholder" not in spec.summary.casefold() for spec in specs)
    assert all("not implemented" not in spec.description.casefold() for spec in specs)
