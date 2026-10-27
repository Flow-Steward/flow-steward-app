"""Provider inventory must tolerate a connection with no provider row.

Remediation epic 2026-07-29, WS-09 — `AUDIT-060` (P0).

`GET /api/projects/{project_id}/providers` built each entry from
`to_json_obj(bl.get_provider(extension_id))`. That is `None` for an
extension-backed project connection, which has no provider record of its own.
One access was guarded (`if provider_row else True`); the five after it assumed a
mapping, so a single such connection raised `AttributeError` and the endpoint
returned 500.

The blast radius was the whole notification-route UI: the editor sources its
provider list from this endpoint, so *Provider* contained only "Select provider"
and *Create route* could only answer "Provider is required". That blocked email
and Mailpit notification configuration, and persisted even after system SMTP was
configured and verified as delivering.

The route handler is a closure registered inside
`register_project_integration_routes`, so driving it end to end would mean
standing up the whole router. Instead both versions of the per-connection block
are reproduced here — the pre-fix one copied verbatim from `83b5cb6`, the audit's
own baseline — so the test proves the defect as well as the fix.
"""

from __future__ import annotations

from pathlib import Path

import pytest

SOURCE = Path("core/entrypoints/api/project_integrations.py")


def to_json_obj(value):
    """Stand-in with the behaviour that matters here: None passes through."""
    return value


def pre_fix_block(bl, extension_id: str, *, enabled_only: bool) -> dict:
    """The block exactly as `83b5cb6` shipped it. Kept verbatim."""
    provider_row = (
        to_json_obj(bl.get_provider(extension_id))
        if extension_id and hasattr(bl, "get_provider")
        else {}
    )
    extension_row = (
        to_json_obj(bl.get_extension(extension_id))
        if extension_id and hasattr(bl, "get_extension")
        else {}
    )
    provider_enabled = bool(provider_row.get("enabled", True)) if provider_row else True
    if enabled_only and not provider_enabled:
        return {}
    provider_type_key = str(provider_row.get("provider_type_key") or "").strip()
    if not provider_type_key:
        provider_type_key = str(
            extension_row.get("kind") or extension_row.get("provider_type_key") or "task_provider"
        ).strip()
    return {"provider_key": extension_id, "provider_type_key": provider_type_key}


def fixed_block(bl, extension_id: str, *, enabled_only: bool) -> dict:
    """The block as it ships now: both rows normalized before any access."""
    provider_row = (
        to_json_obj(bl.get_provider(extension_id))
        if extension_id and hasattr(bl, "get_provider")
        else {}
    ) or {}
    extension_row = (
        to_json_obj(bl.get_extension(extension_id))
        if extension_id and hasattr(bl, "get_extension")
        else {}
    ) or {}
    provider_enabled = bool(provider_row.get("enabled", True)) if provider_row else True
    if enabled_only and not provider_enabled:
        return {}
    provider_type_key = str(provider_row.get("provider_type_key") or "").strip()
    if not provider_type_key:
        provider_type_key = str(
            extension_row.get("kind") or extension_row.get("provider_type_key") or "task_provider"
        ).strip()
    return {"provider_key": extension_id, "provider_type_key": provider_type_key}


class FakeLogger:
    """A project whose extension connection has no provider record."""

    def __init__(self, *, provider=None, extension=None):
        self._provider = provider
        self._extension = extension

    def get_provider(self, _key):
        return self._provider

    def get_extension(self, _key):
        return self._extension


class TestThePreFixBlockStillFails:
    """Proves the defect, so the fix below means something."""

    def test_a_missing_provider_row_raises_attribute_error(self):
        bl = FakeLogger(provider=None, extension={"kind": "notifier"})

        with pytest.raises(AttributeError):
            pre_fix_block(bl, "acme_showcase", enabled_only=True)

    def test_a_missing_extension_row_also_raises(self):
        bl = FakeLogger(provider={"provider_type_key": ""}, extension=None)

        with pytest.raises(AttributeError):
            pre_fix_block(bl, "acme_showcase", enabled_only=True)

    def test_it_worked_whenever_a_provider_row_happened_to_exist(self):
        """Why this went unnoticed for so long: the common case is fine."""
        bl = FakeLogger(provider={"provider_type_key": "email"}, extension={})

        assert pre_fix_block(bl, "smtp", enabled_only=True)["provider_type_key"] == "email"


class TestTheFixedBlockToleratesMissingRows:
    def test_a_missing_provider_row_is_handled(self):
        bl = FakeLogger(provider=None, extension={"kind": "notifier"})

        entry = fixed_block(bl, "acme_showcase", enabled_only=True)

        assert entry["provider_key"] == "acme_showcase"
        assert entry["provider_type_key"] == "notifier"

    def test_a_missing_extension_row_is_handled(self):
        bl = FakeLogger(provider={"provider_type_key": ""}, extension=None)

        assert fixed_block(bl, "x", enabled_only=True)["provider_type_key"] == "task_provider"

    def test_both_rows_missing_still_yields_a_stable_provider_type(self):
        bl = FakeLogger(provider=None, extension=None)

        assert fixed_block(bl, "x", enabled_only=True)["provider_type_key"] == "task_provider"

    def test_an_existing_provider_row_behaves_as_before(self):
        """The fix must not change the path that already worked."""
        bl = FakeLogger(provider={"provider_type_key": "email"}, extension={})

        assert fixed_block(bl, "smtp", enabled_only=True)["provider_type_key"] == "email"

    def test_a_disabled_provider_is_still_filtered_when_enabled_only(self):
        bl = FakeLogger(provider={"enabled": False}, extension={})

        assert fixed_block(bl, "smtp", enabled_only=True) == {}

    def test_a_disabled_provider_is_kept_when_not_filtering(self):
        bl = FakeLogger(provider={"enabled": False, "provider_type_key": "email"}, extension={})

        assert fixed_block(bl, "smtp", enabled_only=False)["provider_type_key"] == "email"


class TestShippedSourceMatchesTheFixedBlock:
    """Guard against the normalization being removed again."""

    def test_both_rows_are_normalized_before_any_access(self):
        source = SOURCE.read_text(encoding="utf-8")
        start = source.index("provider_row = (")
        block = source[start : source.index("@router.post", start)]

        assert block.index("provider_row = (") < block.index("provider_row.get(")
        assert block.index("extension_row = (") < block.index("extension_row.get(")
        assert block.count(") or {}") >= 2, "a nullable row is dereferenced unnormalized"
