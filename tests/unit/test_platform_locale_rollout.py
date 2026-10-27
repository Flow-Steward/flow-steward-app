"""Public localization behavior for the English/Lithuanian platform rollout."""

import re
from types import SimpleNamespace

import pytest

from core.application.settings import ui_i18n


@pytest.fixture(autouse=True)
def clear_locale_caches():
    ui_i18n.reset_ui_i18n_cache()
    yield
    ui_i18n.reset_ui_i18n_cache()


def request(*, profile=None, cookie="", query=None, header=""):
    return SimpleNamespace(
        state=SimpleNamespace(auth_user=profile),
        cookies={ui_i18n.UI_LOCALE_COOKIE: cookie},
        query_params=query or {},
        headers={"accept-language": header},
    )


def test_session_offers_only_english_and_lithuanian_with_lithuanian_messages():
    payload = ui_i18n.build_client_i18n_payload(request(profile={"locale": "lt-LT"}))

    assert [entry["code"] for entry in payload["supported_locales"]] == ["en", "lt"]
    assert payload["locale"] == "lt"
    assert payload["fallback_locale"] == "en"
    assert payload["messages"]["auth.login.heading"] == "Prisijungti"
    assert payload["messages"]["shell.nav.dashboard"] == "Valdymo skydelis"


def test_unsupported_preference_falls_back_without_rewriting_user():
    profile = {"locale": "ru-RU", "display_name": "Ąžuolas"}
    payload = ui_i18n.build_client_i18n_payload(request(profile=profile))

    assert payload["locale"] == "en"
    assert profile == {"locale": "ru-RU", "display_name": "Ąžuolas"}
    assert ui_i18n.resolve_ui_locale(request(cookie="ru")) == "en"


def test_russian_browser_language_is_not_mistaken_for_lithuanian_preference():
    assert ui_i18n.normalize_ui_locale("ru") == ""
    assert ui_i18n.resolve_ui_locale(request(header="ru-RU,en;q=0.8")) == "en"
    assert ui_i18n.resolve_ui_locale(request(header="lt-LT,en;q=0.8")) == "lt"
    assert (
        ui_i18n.resolve_ui_locale(request(profile={"locale": "ru"}, query={"lang": "en"})) == "en"
    )


def test_lithuanian_compiled_catalog_covers_english_without_losing_placeholders():
    english = ui_i18n._catalog_messages("en")
    lithuanian = ui_i18n._catalog_messages("lt")

    assert english
    missing = sorted(key for key in english if not lithuanian.get(key))
    assert not missing, f"Missing Lithuanian translations: {missing}"
    for key, source in english.items():
        source_fields = set(re.findall(r"(?<!{){([A-Za-z_]\w*)}(?!})", source))
        translated_fields = set(re.findall(r"(?<!{){([A-Za-z_]\w*)}(?!})", lithuanian[key]))
        assert translated_fields == source_fields, f"Changed placeholders: {key}"


def test_lithuanian_formatting_keeps_operator_values_unchanged():
    assert (
        ui_i18n.translate_ui_text(
            "shell.account.current", locale="lt", params={"account": "ACME Ąžuolas"}
        )
        == "Dabartinė paskyra: ACME Ąžuolas"
    )


@pytest.mark.parametrize(
    ("key", "english", "lithuanian"),
    [
        (
            "react.workflows.transform_summary",
            "state.owner_input -> state.owner_result (2 fields)",
            "state.owner_input -> state.owner_result (laukų skaičius: 2)",
        ),
        (
            "react.workflows.transform_summary_default_source",
            "source -> state.owner_result (2 fields)",
            "šaltinis -> state.owner_result (laukų skaičius: 2)",
        ),
        (
            "react.workflows.transform_summary_default_target",
            "state.owner_input -> result (2 fields)",
            "state.owner_input -> rezultatas (laukų skaičius: 2)",
        ),
        (
            "react.workflows.transform_summary_defaults",
            "source -> result (2 fields)",
            "šaltinis -> rezultatas (laukų skaičius: 2)",
        ),
    ],
)
@pytest.mark.parametrize("locale", ["en", "lt"])
def test_compiled_transform_summaries_keep_authored_paths_and_exact_count(
    key, english, lithuanian, locale
):
    params = {
        "source": "state.owner_input",
        "target": "state.owner_result",
        "field_count": 2,
    }

    assert key in ui_i18n._catalog_messages(locale)
    assert ui_i18n.translate_ui_text(key, locale=locale, params=params) == (
        lithuanian if locale == "lt" else english
    )


@pytest.mark.parametrize(
    ("key", "params", "english", "lithuanian"),
    [
        (
            "react.workflows.if_default_summary",
            {},
            "conditional branch",
            "sąlyginis šakojimas",
        ),
        (
            "react.workflows.switch_selector_summary",
            {"selector": "state.owner_choice", "case_count": 2},
            "state.owner_choice (2 cases)",
            "state.owner_choice (atvejų skaičius: 2)",
        ),
        (
            "react.workflows.switch_cases_summary",
            {"case_count": 0},
            "0 switch cases",
            "Pasirinkimo atvejų skaičius: 0",
        ),
    ],
)
@pytest.mark.parametrize("locale", ["en", "lt"])
def test_compiled_branch_summaries_keep_authored_selector_and_exact_count(
    key, params, english, lithuanian, locale
):
    assert key in ui_i18n._catalog_messages(locale)
    assert ui_i18n.translate_ui_text(key, locale=locale, params=params) == (
        lithuanian if locale == "lt" else english
    )


@pytest.mark.parametrize(
    ("key", "params", "english", "lithuanian"),
    [
        (
            "react.workflows.agent_summary",
            {"label": "owner-agent"},
            "agent: owner-agent",
            "agentas: owner-agent",
        ),
        ("react.workflows.agent_default_summary", {}, "agent step", "agento žingsnis"),
        (
            "react.workflows.invoke_workflow_summary",
            {"workflow_id": "owner-child"},
            "invoke owner-child",
            "Vykdyti owner-child",
        ),
        (
            "react.workflows.invoke_workflow_default_summary",
            {"workflow_id": "owner-parent"},
            "invoke from owner-parent",
            "Vykdyti iš owner-parent",
        ),
        (
            "react.workflows.batch_summary",
            {"source": "state.owner_items", "batch_size": 3},
            "state.owner_items in batches of 3",
            "state.owner_items paketais po 3",
        ),
        (
            "react.workflows.batch_default_summary",
            {"batch_size": 1},
            "items in batches of 1",
            "Elementai paketais po 1",
        ),
    ],
)
@pytest.mark.parametrize("locale", ["en", "lt"])
def test_compiled_agent_invoke_batch_summaries_preserve_owner_values(
    key, params, english, lithuanian, locale
):
    assert key in ui_i18n._catalog_messages(locale)
    assert ui_i18n.translate_ui_text(key, locale=locale, params=params) == (
        lithuanian if locale == "lt" else english
    )
