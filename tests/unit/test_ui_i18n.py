from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import core.application.settings.ui_i18n as ui_i18n_module
from core.application.settings.ui_i18n import (
    DEFAULT_UI_LOCALE,
    UI_LOCALE_COOKIE,
    build_client_i18n_payload,
    normalize_ui_locale,
    reset_ui_i18n_cache,
    resolve_ui_locale,
    supported_ui_locales,
    translate_ui_text,
    ui_messages_for_locale,
)


def _request(*, query=None, cookies=None, headers=None, auth_user=None):
    return SimpleNamespace(
        query_params=query or {},
        cookies=cookies or {},
        headers=headers or {},
        state=SimpleNamespace(auth_user=auth_user),
    )


@pytest.fixture(autouse=True)
def _clear_i18n_caches() -> None:
    reset_ui_i18n_cache()
    yield
    reset_ui_i18n_cache()


def test_normalize_ui_locale_accepts_regional_tags() -> None:
    assert normalize_ui_locale("lt-LT") == "lt"
    assert normalize_ui_locale("en_US") == "en"
    assert normalize_ui_locale("fr-FR") == ""


def test_resolve_ui_locale_prefers_query_param_over_cookie_and_headers() -> None:
    request = _request(
        query={"lang": "lt-LT"},
        cookies={UI_LOCALE_COOKIE: "en"},
        headers={"accept-language": "en-US,en;q=0.9"},
    )

    assert resolve_ui_locale(request) == "lt"


def test_resolve_ui_locale_uses_request_state_user_when_available() -> None:
    request = _request(auth_user={"locale": "lt"})

    assert resolve_ui_locale(request) == "lt"


def test_resolve_ui_locale_prefers_user_profile_over_cookie() -> None:
    request = _request(cookies={UI_LOCALE_COOKIE: "lt"}, auth_user={"locale": "en"})

    assert resolve_ui_locale(request) == "en"


@pytest.mark.parametrize(
    "preference_key", ["locale", "language", "preferred_locale", "preferred_language"]
)
@pytest.mark.parametrize("locale", ["ru", "ru-RU", "RU_ru"])
def test_resolve_ui_locale_rejects_unsupported_russian_profile_preferences(
    preference_key: str,
    locale: str,
) -> None:
    request = _request(auth_user={preference_key: locale})

    assert resolve_ui_locale(request) == "en"


@pytest.mark.parametrize("supported,expected", [("en-US", "en"), ("lt-LT", "lt")])
def test_resolve_ui_locale_skips_unsupported_profile_for_next_supported_preference(
    supported: str, expected: str
) -> None:
    request = _request(auth_user={"locale": "ru", "language": supported})

    assert resolve_ui_locale(request) == expected


@pytest.mark.parametrize("supported,expected", [("en", "en"), ("lt", "lt")])
def test_resolve_ui_locale_skips_unsupported_profile_for_supported_cookie(
    supported: str, expected: str
) -> None:
    request = _request(cookies={UI_LOCALE_COOKIE: supported})

    assert resolve_ui_locale(request, auth_user={"locale": "ru-RU"}) == expected


def test_resolve_ui_locale_skips_unsupported_cookie_for_lithuanian_header() -> None:
    request = _request(
        cookies={UI_LOCALE_COOKIE: "ru"},
        headers={"accept-language": "lt-LT,en;q=0.8"},
    )

    assert resolve_ui_locale(request) == "lt"


@pytest.mark.parametrize("locale", ["ru", "ru-RU", "RU_ru"])
@pytest.mark.parametrize("header", ["", "en-US,en;q=0.9"])
def test_resolve_ui_locale_rejects_unsupported_russian_cookie(
    locale: str,
    header: str,
) -> None:
    request = _request(
        cookies={UI_LOCALE_COOKIE: locale},
        headers={"accept-language": header},
    )

    assert resolve_ui_locale(request) == "en"


def test_resolve_ui_locale_falls_back_to_accept_language() -> None:
    request = _request(headers={"accept-language": "fr-FR,lt;q=0.9,en;q=0.8"})

    assert resolve_ui_locale(request) == "lt"


def test_resolve_ui_locale_prefers_user_profile_over_accept_language() -> None:
    request = _request(
        headers={"accept-language": "lt-LT,lt;q=0.9,en;q=0.8"},
        auth_user={"locale": "en"},
    )

    assert resolve_ui_locale(request) == "en"


def test_resolve_ui_locale_ignores_retired_cookie_name() -> None:
    retired_cookie_name = "retiredcodename-locale"
    request = _request(cookies={retired_cookie_name: "lt-LT"})

    assert resolve_ui_locale(request) == "en"


def test_translate_ui_text_formats_values_for_selected_locale() -> None:
    assert (
        translate_ui_text(
            "shell.account.current",
            locale="lt",
            params={"account": "ACME"},
        )
        == "Dabartinė paskyra: ACME"
    )


def test_translate_ui_text_uses_default_string_for_unknown_keys() -> None:
    assert (
        translate_ui_text(
            "missing.key",
            locale="lt",
            default="Hello, {name}",
            params={"name": "Ada"},
        )
        == "Hello, Ada"
    )


def test_ui_messages_for_locale_loads_from_catalog_files() -> None:
    messages = ui_messages_for_locale("lt")
    assert messages["auth.login.heading"] == "Prisijungti"
    assert messages["jobs.refresh"] == "Atnaujinti"


@pytest.mark.parametrize(
    ("locale", "expected"),
    [
        (
            "en",
            "transform input.source 'state.inputs.owner_payload' is not produced by prior workflow inputs or earlier steps",
        ),
        (
            "lt",
            "Transformavimo įvesties šaltinis 'state.inputs.owner_payload' nėra sukurtas ankstesnių darbo eigos įvesčių ar žingsnių.",
        ),
    ],
)
def test_transform_source_diagnostic_translation_preserves_owner_source(locale, expected):
    assert (
        translate_ui_text(
            "react.workflows.transform_input_source_unavailable",
            locale=locale,
            params={"source": "state.inputs.owner_payload"},
        )
        == expected
    )


def test_translate_ui_text_falls_back_to_default_locale_when_selected_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_catalog(locale: str) -> dict[str, str]:
        if locale == DEFAULT_UI_LOCALE:
            return {
                "demo.key": "Default value",
                "demo.format": "Default {value}",
            }
        if locale == "lt":
            return {}
        return {}

    monkeypatch.setattr(ui_i18n_module, "_catalog_messages", _fake_catalog)

    assert translate_ui_text("demo.key", locale="lt") == "Default value"
    assert translate_ui_text("demo.format", locale="lt", params={"value": 7}) == "Default 7"


def test_supported_ui_locales_discovered_from_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    locales_root = tmp_path / "locales"

    def _write_locale(
        code: str,
        label: str,
        native_label: str,
        *,
        compiled: bool = True,
    ) -> None:
        locale_dir = locales_root / code
        (locale_dir / "LC_MESSAGES").mkdir(parents=True, exist_ok=True)
        (locale_dir / "LC_MESSAGES" / "ui.po").write_text(
            'msgid ""\nmsgstr ""\n',
            encoding="utf-8",
        )
        if compiled:
            (locale_dir / "LC_MESSAGES" / "ui.mo").write_bytes(b"\x00")
        (locale_dir / "meta.json").write_text(
            (
                "{\n"
                f'  "code": "{code}",\n'
                f'  "label": "{label}",\n'
                f'  "native_label": "{native_label}"\n'
                "}\n"
            ),
            encoding="utf-8",
        )

    _write_locale("en", "English", "English")
    _write_locale("de", "German", "Deutsch")
    _write_locale("fr", "French", "Français", compiled=False)

    monkeypatch.setattr(ui_i18n_module, "LOCALES_ROOT", locales_root)
    reset_ui_i18n_cache()

    locales = supported_ui_locales()
    assert locales[0]["code"] == "en"
    assert any(item["code"] == "de" and item["native_label"] == "Deutsch" for item in locales)
    assert not any(item["code"] == "fr" for item in locales)


def test_translate_ui_text_formats_new_page_level_strings() -> None:
    assert translate_ui_text("auth.login.heading", locale="lt") == "Prisijungti"
    assert (
        translate_ui_text(
            "projects.filter.all",
            locale="lt",
            params={"count": 3},
        )
        == "Visi (3)"
    )
    assert (
        translate_ui_text(
            "monitoring.banner.ready_note",
            locale="en",
            params={"time": "10:15"},
        )
        == "All core readiness checks passed at 10:15."
    )
    assert translate_ui_text("account.tokens.revoke", locale="lt") == "Atšaukti"
    assert (
        translate_ui_text(
            "processes.bulk.confirm_cancel",
            locale="en",
            params={"count": 2},
        )
        == "Cancel 2 selected jobs?"
    )
    assert translate_ui_text("events.dispatches", locale="lt") == "Siuntimai"
    assert (
        translate_ui_text(
            "jobs.confirm.restart_job",
            locale="lt",
            params={"job_id": 17},
        )
        == "Paleisti užduotį Nr. 17 iš naujo?"
    )
    assert (
        translate_ui_text(
            "project_edit.badge.linked_workflows",
            locale="en",
            params={"count": 4},
        )
        == "4 linked workflows"
    )
    assert (
        translate_ui_text(
            "project_edit.diff.changed_more",
            locale="lt",
            params={"count": 3},
        )
        == "Dar + 3 laukų pakeitimų. Visus skirtumus rasite istorijoje."
    )
    assert translate_ui_text("project_mode.workspace", locale="lt") == "Darbo sritis"
    assert translate_ui_text("job_security.title", locale="lt") == "Užduoties saugumo problemos"
    assert translate_ui_text("events.page_title", locale="en") == "Event Inspector"
    assert translate_ui_text("react.projects.history", locale="lt") == "Istorija"
    assert (
        translate_ui_text(
            "settings.notifications.routes_count",
            locale="en",
            params={"count": 5},
        )
        == "5 routes"
    )
    assert (
        translate_ui_text(
            "settings.watchdog.recover_confirm",
            locale="lt",
            params={"action": "Pakartoti", "job_id": 21},
        )
        == "Pakartoti užduotį Nr. 21?"
    )
    assert (
        translate_ui_text("settings.model_providers.fields.base_url", locale="lt") == "Bazinis URL"
    )
    assert (
        translate_ui_text("settings.access.service_accounts.create_account", locale="lt")
        == "Sukurti paskyrą"
    )
    assert (
        translate_ui_text(
            "settings.access.keys.confirm.revoke",
            locale="en",
            params={"token_id": 8},
        )
        == "Revoke token #8?"
    )
    assert (
        translate_ui_text(
            "settings.access.picker.selected_count",
            locale="en",
            params={"count": 2},
        )
        == "2 selected"
    )
    assert (
        translate_ui_text(
            "settings.mcp.summary",
            locale="lt",
            params={"url": "https://mcp.example", "auth": "Bearer raktas"},
        )
        == "Serveris: https://mcp.example · Autentifikavimas: Bearer raktas"
    )
    assert (
        translate_ui_text("settings.extensions.installed.title", locale="lt") == "Įdiegti plėtiniai"
    )
    assert (
        translate_ui_text(
            "settings.extensions.marketplace.results",
            locale="en",
            params={"count": 4},
        )
        == "results: 4"
    )
    assert (
        translate_ui_text("settings.extensions.status.billing_blocked", locale="lt")
        == "Atsiskaitymas užblokuotas"
    )
    assert (
        translate_ui_text("settings.extensions.delete.confirm", locale="en")
        == "Uninstall this extension? This cannot be undone."
    )
    assert (
        translate_ui_text("account_settings.updated", locale="lt")
        == "Paskyros pavadinimas atnaujintas."
    )
    assert translate_ui_text("settings.retention.saved", locale="en") == "Retention settings saved."
    assert (
        translate_ui_text("security_overview.mode_note.monitor", locale="lt")
        == "Aktyvus režimas: stebėjimas (neblokuoja)"
    )
    assert (
        translate_ui_text(
            "trash.summary.filtered",
            locale="en",
            params={"visible": 3, "total": 8},
        )
        == "Showing 3 of 8 deleted items"
    )
    assert (
        translate_ui_text("extension_page.validation.clean", locale="lt")
        == "Sukompiliuotas manifestas praėjo kompiliavimo ir tikrinimo patikras."
    )
    assert translate_ui_text("dashboard.command_center.title", locale="lt") == "Valdymo centras"
    assert (
        translate_ui_text(
            "dashboard.empty.pill.awaiting_approval",
            locale="en",
            params={"count": 3},
        )
        == "3 awaiting approval"
    )
    assert (
        translate_ui_text(
            "dashboard.classic.error.updated_at",
            locale="lt",
            params={"time": "10:15"},
        )
        == "Atnaujinta 10:15"
    )
    assert (
        translate_ui_text(
            "dashboard.classic.recent.job_fallback",
            locale="en",
            params={"job_id": 12},
        )
        == "Job #12"
    )
    assert translate_ui_text("dashboard.time.just_now", locale="lt") == "ką tik"
    assert (
        translate_ui_text("react.workflows.inputs_for_scheduled_runs", locale="en")
        == "Inputs for scheduled runs"
    )
    assert (
        translate_ui_text("react.workflows.inputs_for_scheduled_runs", locale="lt")
        == "Įvestys suplanuotiems vykdymams"
    )
    assert (
        translate_ui_text(
            "project_detail.scheduled.flash.run_now_enqueued",
            locale="lt",
            params={"scheduled_job_id": 3, "job_id": 77},
        )
        == "Suplanuota užduotis Nr. 3 įtraukta į eilę kaip užduotis Nr. 77."
    )


def test_build_client_i18n_payload_returns_current_locale_and_messages() -> None:
    payload = build_client_i18n_payload(
        _request(cookies={UI_LOCALE_COOKIE: "lt"}),
    )

    assert payload["locale"] == "lt"
    assert payload["messages"]["shell.nav.dashboard"] == "Valdymo skydelis"
    assert payload["messages"]["shell.navigation.close"] == "Uždaryti navigaciją"
    assert payload["messages"]["auth.login.heading"] == "Prisijungti"
    assert payload["messages"]["projects.page_title"] == "Projektai"
    assert payload["messages"]["monitoring.page_title"] == "Stebėsena"
    assert payload["messages"]["dashboard.command_center.title"] == "Valdymo centras"
    assert any(entry["code"] == "lt" for entry in payload["supported_locales"])


def test_for_each_concurrency_has_workflow_owned_translations() -> None:
    assert translate_ui_text("react.workflows.for_each_concurrency", locale="en") == "Concurrency"
    assert translate_ui_text("react.workflows.for_each_concurrency", locale="lt") == "Lygiagretumas"
