from __future__ import annotations


def test_required_dependency_marker_blocks_while_extension_is_missing() -> None:
    from core.application.extensions.projection_dependency_availability import (
        resolve_projection_dependency_availability,
    )

    availability = resolve_projection_dependency_availability(
        {
            "dependencies": {"extensions": [{"extension_id": "acme.provider", "required": True}]},
            "extension_dependency_availability": {
                "missing_extensions": ["acme.provider"],
                "new_runs_blocked": True,
            },
        },
        active_extension_ids=set(),
    )

    assert availability["availability_status"] == "unavailable"
    assert availability["missing_extensions"] == ["acme.provider"]
    assert availability["new_runs_blocked"] is True


def test_required_dependency_marker_recovers_after_extension_reinstall() -> None:
    from core.application.extensions.projection_dependency_availability import (
        resolve_projection_dependency_availability,
    )

    availability = resolve_projection_dependency_availability(
        {
            "dependencies": {"extensions": [{"extension_id": "acme.provider", "required": True}]},
            "extension_dependency_availability": {
                "availability_status": "unavailable",
                "missing_extensions": ["acme.provider"],
                "new_runs_blocked": True,
            },
        },
        active_extension_ids={"acme.provider"},
    )

    assert availability == {
        "availability_status": "available",
        "availability_reason_code": "none",
        "availability_reason_text": "",
        "missing_extensions": [],
        "new_runs_blocked": False,
        "visible_in_catalog": True,
    }


def test_optional_dependency_does_not_block_projection() -> None:
    from core.application.extensions.projection_dependency_availability import (
        resolve_projection_dependency_availability,
    )

    availability = resolve_projection_dependency_availability(
        {"dependencies": {"extensions": [{"extension_id": "acme.optional", "required": False}]}},
        active_extension_ids=set(),
    )

    assert availability["availability_status"] == "available"
    assert availability["new_runs_blocked"] is False


def test_required_dependency_needs_an_active_version_in_its_declared_range() -> None:
    from core.application.extensions.projection_dependency_availability import (
        resolve_projection_dependency_availability,
    )

    projection = {
        "dependencies": {
            "extensions": [
                {
                    "extension_id": "acme.provider",
                    "required": True,
                    "min_version": "2.0.0",
                    "max_version_exclusive": "3.0.0",
                }
            ]
        }
    }

    unavailable = resolve_projection_dependency_availability(
        projection,
        active_extension_ids={"acme.provider"},
        active_extension_versions={"acme.provider": {"1.9.0"}},
    )
    available = resolve_projection_dependency_availability(
        projection,
        active_extension_ids={"acme.provider"},
        active_extension_versions={"acme.provider": {"1.9.0", "2.1.0"}},
    )

    assert unavailable["availability_status"] == "unavailable"
    assert unavailable["missing_extensions"] == ["acme.provider"]
    assert available["availability_status"] == "available"


_SHOWCASE_PROJECTION = {
    "dependencies": {
        "extensions": [
            {
                "extension_id": "acme.data-connector-postgres",
                "required": True,
                "reason": "This workflow executes the projected dataset through MCP Toolbox.",
            }
        ]
    }
}


def test_an_unavailable_result_carries_the_structured_reason_next_to_the_sentence() -> None:
    """A client words the reason itself: id, name and state, not only a sentence."""
    from core.application.extensions.projection_dependency_availability import (
        missing_extension_details,
        resolve_projection_dependency_availability,
    )

    def describe(missing_ids, required_dependencies):
        return missing_extension_details(
            missing_ids,
            required_dependencies,
            installs={},
            catalog_names={"acme.data-connector-postgres": "PostgreSQL (Google MCP Toolbox)"},
        )

    availability = resolve_projection_dependency_availability(
        _SHOWCASE_PROJECTION, active_extension_ids=set(), describe_missing=describe
    )

    assert availability["availability_reason_text"].startswith("Needs the extension ")
    assert availability["missing_extension_details"] == [
        {
            "extension_id": "acme.data-connector-postgres",
            "display_name": "PostgreSQL (Google MCP Toolbox)",
            "state": "not_installed",
            "min_version": "",
            "max_version_exclusive": "",
        }
    ]


def test_the_state_tells_installed_and_off_apart_from_never_installed() -> None:
    from core.application.extensions.projection_dependency_availability import (
        missing_extension_details,
    )

    dependencies = [
        {"extension_id": "acme.off"},
        {"extension_id": "acme.absent"},
        {"extension_id": "acme.old", "min_version": "2.0.0"},
    ]
    details = missing_extension_details(
        ["acme.absent", "acme.off", "acme.old"],
        dependencies,
        installs={
            "acme.off": {"active": False, "display_name": "Acme Off"},
            "acme.old": {"active": True, "display_name": "Acme Old"},
        },
    )

    assert [(item["extension_id"], item["state"], item["display_name"]) for item in details] == [
        ("acme.absent", "not_installed", "Absent"),
        ("acme.off", "installed_disabled", "Acme Off"),
        ("acme.old", "installed_incompatible", "Acme Old"),
    ]
    assert details[2]["min_version"] == "2.0.0"


def test_unreadable_installs_say_unknown_rather_than_not_installed() -> None:
    from core.application.extensions.projection_dependency_availability import (
        resolve_projection_dependency_availability,
    )

    availability = resolve_projection_dependency_availability(
        _SHOWCASE_PROJECTION, active_extension_ids=set()
    )

    [detail] = availability["missing_extension_details"]
    assert detail["state"] == "unknown"
    assert detail["display_name"] == "Data Connector Postgres"


def test_a_failing_describer_still_leaves_a_readable_reason() -> None:
    from core.application.extensions.projection_dependency_availability import (
        resolve_projection_dependency_availability,
    )

    def describe(_missing_ids, _required_dependencies):
        raise RuntimeError("install store down")

    availability = resolve_projection_dependency_availability(
        _SHOWCASE_PROJECTION, active_extension_ids=set(), describe_missing=describe
    )

    assert availability["missing_extension_details"][0]["state"] == "unknown"


def test_the_installed_name_comes_from_the_release_and_inactive_installs_count() -> None:
    from types import SimpleNamespace
    from unittest.mock import patch

    from core.application.extensions.projection_dependency_availability import (
        extension_installs_for_account,
    )

    calls = []

    class _InstallStore:
        def list_for_account(self, account_id, *, include_global, include_inactive_owned):
            calls.append((account_id, include_global, include_inactive_owned))
            return [
                SimpleNamespace(extension_id="acme.off", state="disabled", release_id="rel-off"),
                SimpleNamespace(extension_id="acme.other", state="active", release_id="rel-x"),
            ]

    runtime_store = SimpleNamespace(
        get_release=lambda release_id: SimpleNamespace(
            descriptor={"identity": {"display_name": f"Name of {release_id}"}}
        )
    )
    with (
        patch(
            "core.application.extensions.service_support.get_default_extension_install_store",
            return_value=_InstallStore(),
        ),
        patch(
            "core.application.extensions.service_support.get_default_extension_runtime_store",
            return_value=runtime_store,
        ),
    ):
        installs = extension_installs_for_account(
            object(), account_id="acc-1", extension_ids=["acme.off", "acme.absent"]
        )

    assert calls == [("acc-1", True, True)]
    assert installs == {"acme.off": {"active": False, "display_name": "Name of rel-off"}}


def test_the_catalog_name_is_read_from_the_cached_index_without_fetching(monkeypatch) -> None:
    from core.application.extensions import extension_catalog

    item = type("Item", (), {"extension_id": "acme.pg", "name": "Acme PostgreSQL"})()
    index = type("Index", (), {"items": (item,)})()
    monkeypatch.setattr(
        extension_catalog, "extension_catalog_url", lambda: "https://cat/index.json"
    )
    monkeypatch.setattr(
        extension_catalog._INDEX,
        "current",
        lambda url: index if url == "https://cat/index.json" else None,
    )
    monkeypatch.setattr(
        extension_catalog._INDEX,
        "load",
        lambda _url: (_ for _ in ()).throw(AssertionError("must not fetch")),
    )

    assert extension_catalog.cached_extension_catalog_names() == {"acme.pg": "Acme PostgreSQL"}

    monkeypatch.setattr(extension_catalog, "extension_catalog_url", lambda: "")
    assert extension_catalog.cached_extension_catalog_names() == {}
