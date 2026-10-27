"""Dynamic error-guidance and canvas-action keys must reach real locale catalogs."""

from __future__ import annotations

import pytest

from core.application.settings.ui_i18n import reset_ui_i18n_cache, translate_ui_text

CASES = [
    (
        "react.extensions.secret_value_required",
        "Enter a secret value before saving.",
        "Prieš išsaugodami įveskite slaptą reikšmę.",
    ),
    (
        "react.extensions.secret_storage_unavailable",
        "Secret storage is unavailable. Retry, then contact an administrator if the problem continues.",
        "Slaptų duomenų saugykla nepasiekiama. Bandykite dar kartą, o jei problema kartojasi, kreipkitės į administratorių.",
    ),
    (
        "react.extensions.secret_storage_failed",
        "The secret was not saved. Retry, then contact an administrator if the problem continues.",
        "Slapta reikšmė neišsaugota. Bandykite dar kartą, o jei problema kartojasi, kreipkitės į administratorių.",
    ),
    (
        "react.extensions.connection_storage_unavailable",
        "The connection could not be saved. Retry, then contact an administrator if the problem continues.",
        "Ryšio nepavyko išsaugoti. Bandykite dar kartą, o jei problema kartojasi, kreipkitės į administratorių.",
    ),
    (
        "react.extensions.project_required_for_action",
        "Select a project before running this action.",
        "Prieš vykdydami šį veiksmą pasirinkite projektą.",
    ),
    (
        "react.extensions.authorization_required_for_action",
        "Authorize this connection, then retry the action.",
        "Autorizuokite šį ryšį ir pakartokite veiksmą.",
    ),
    (
        "react.extensions.invalid_connection_configuration",
        "Review the connection configuration and correct the highlighted values.",
        "Peržiūrėkite ryšio konfigūraciją ir pataisykite pažymėtas reikšmes.",
    ),
    (
        "react.workflows.diagnostic_action_outcome_edge",
        "Connect every outcome of the affected step to its next step.",
        "Sujunkite kiekvieną paveikto žingsnio baigtį su tolesniu žingsniu.",
    ),
]


@pytest.mark.parametrize("key, english, lithuanian", CASES)
@pytest.mark.parametrize("locale", ["en", "lt"])
def test_dynamic_guidance_key_is_translated(
    key: str, english: str, lithuanian: str, locale: str
) -> None:
    reset_ui_i18n_cache()
    assert translate_ui_text(key, locale=locale) == (english if locale == "en" else lithuanian)
