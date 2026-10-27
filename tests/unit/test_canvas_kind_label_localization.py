from __future__ import annotations

import json

import pytest

from core.application.settings.ui_i18n import reset_ui_i18n_cache, translate_ui_text

CASES = json.loads(
    r"""[{"kind":"agent","key":"react.workflows.agent","en":"Agent","lt":"Agentas"},{"kind":"connector","key":"react.workflows.tool_action","en":"Tool Action","lt":"Įrankio veiksmas"},{"kind":"invoke_workflow","key":"react.workflows.invoke_workflow","en":"Invoke workflow","lt":"Iškviesti darbo eigą"},{"kind":"if","key":"react.workflows.if","en":"if","lt":"jei"},{"kind":"switch","key":"react.workflows.switch","en":"switch","lt":"pasirinkimas"},{"kind":"stop_success","key":"react.workflows.finish_successfully","en":"Finish successfully","lt":"Užbaigti sėkmingai"},{"kind":"stop_fail","key":"react.workflows.finish_with_failure","en":"Finish with failure","lt":"Užbaigti nesėkmingai"},{"kind":"set_variable","key":"react.workflows.set_variable","en":"Set variable","lt":"Nustatyti kintamąjį"},{"kind":"for_each","key":"react.workflows.for_each_loop","en":"For each loop","lt":"Ciklas kiekvienam elementui"},{"kind":"batch","key":"react.workflows.batch","en":"batch","lt":"paketas"},{"kind":"http_request","key":"react.workflows.http_request","en":"HTTP request","lt":"HTTP užklausa"},{"kind":"dataset_query","key":"react.workflows.dataset_query","en":"Dataset query","lt":"Duomenų rinkinio užklausa"},{"kind":"transform","key":"react.workflows.transform","en":"transform","lt":"transformavimas"},{"kind":"tabular_feed","key":"react.workflows.tabular_feed","en":"Tabular feed","lt":"Lentelinis srautas"},{"kind":"file_from_base64","key":"react.workflows.save_file_from_base64","en":"Save file from base64","lt":"Išsaugoti failą iš base64"},{"kind":"template","key":"react.workflows.template","en":"template","lt":"šablonas"}]"""
)


@pytest.mark.parametrize("locale", ["en", "lt"])
@pytest.mark.parametrize("case", CASES, ids=lambda row: row["kind"])
def test_canvas_kind_label_in_actual_compiled_catalog(locale, case):
    reset_ui_i18n_cache()
    assert translate_ui_text(case["key"], locale=locale) == case[locale]


@pytest.mark.parametrize("locale", ["en", "lt"])
@pytest.mark.parametrize(
    ("key", "en", "lt"),
    [
        ("react.workflows.inline_branch_badge", "branch", "šaka"),
        ("react.workflows.inline_terminal_badge", "terminal", "galutinis"),
    ],
)
def test_inline_semantic_badge_in_actual_compiled_catalog(locale, key, en, lt):
    reset_ui_i18n_cache()
    assert translate_ui_text(key, locale=locale) == (lt if locale == "lt" else en)
