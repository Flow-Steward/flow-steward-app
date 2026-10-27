import json
from pathlib import Path

import pytest
from babel.messages.catalog import Catalog
from babel.messages.mofile import read_mo, write_mo

from core.application.tooling.i18n import ui_i18n_catalog as catalog


def test_multiline_catalog_text_is_read_without_truncation(tmp_path: Path):
    source = tmp_path / "ui.po"
    source.write_text(
        'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n\n'
        'msgid "example.help"\nmsgstr ""\n"First line. "\n"Second {name}."\n',
        encoding="utf-8",
    )

    assert catalog._read_po(source) == {"example.help": "First line. Second {name}."}


def test_catalog_validation_reports_missing_react_and_application_keys(
    tmp_path: Path, monkeypatch, capsys
):
    locales = tmp_path / "locales"
    default = locales / "en" / "LC_MESSAGES" / "ui.po"
    default.parent.mkdir(parents=True)
    default.write_text('msgid "known.title"\nmsgstr "Known"\n', encoding="utf-8")
    frontend = tmp_path / "assets" / "frontend" / "src"
    frontend.mkdir(parents=True)
    (frontend / "view.tsx").write_text(
        'function View() { const { t } = useI18n(); return <p>{t("missing.react", "Title")}</p>; }',
        encoding="utf-8",
    )
    application = tmp_path / "core" / "application"
    application.mkdir(parents=True)
    (application / "view.py").write_text(
        'translate_ui_text("missing.application")', encoding="utf-8"
    )
    monkeypatch.setattr(catalog, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(catalog, "LOCALES_DIR", locales)

    assert catalog.validate_keys() == 1
    output = capsys.readouterr().out
    assert "missing.react" in output
    assert "missing.application" in output


def _write_compiled_catalog(path: Path, messages: dict[str, str]) -> None:
    compiled = Catalog(locale=path.parent.parent.name)
    for key, translation in messages.items():
        compiled.add(key, translation)
    with path.open("wb") as stream:
        write_mo(stream, compiled)


def _used_key_catalogs(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    locales = tmp_path / "locales"
    for code, text in (("en", "Used title"), ("lt", "Naudojamas pavadinimas")):
        directory = locales / code
        (directory / "LC_MESSAGES").mkdir(parents=True)
        (directory / "meta.json").write_text(json.dumps({"code": code}), encoding="utf-8")
        (directory / "LC_MESSAGES" / "ui.po").write_text(
            f'msgid "used.title"\nmsgstr {json.dumps(text)}\nmsgid "unused.title"\nmsgstr ""\n',
            encoding="utf-8",
        )
        _write_compiled_catalog(directory / "LC_MESSAGES" / "ui.mo", {"used.title": text})
    pot = locales / "ui.pot"
    pot.write_text('msgid "used.title"\nmsgstr ""\n', encoding="utf-8")
    frontend = tmp_path / "assets" / "frontend" / "src"
    frontend.mkdir(parents=True)
    (frontend / "view.tsx").write_text('t("used.title", "Used title")', encoding="utf-8")
    monkeypatch.setattr(catalog, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(catalog, "LOCALES_DIR", locales)
    monkeypatch.setattr(catalog, "POT_PATH", pot)
    return locales, pot


@pytest.mark.parametrize(
    "relative_path,translation",
    (
        ("lt/LC_MESSAGES/ui.po", None),
        ("en/LC_MESSAGES/ui.po", None),
        ("ui.pot", None),
        ("en/LC_MESSAGES/ui.po", ""),
        ("lt/LC_MESSAGES/ui.po", ""),
        ("lt/LC_MESSAGES/ui.po", "   "),
    ),
)
def test_used_keys_must_exist_in_pot_and_have_text_in_each_declared_locale(
    tmp_path: Path, monkeypatch, capsys, relative_path: str, translation: str | None
):
    locales, _pot = _used_key_catalogs(tmp_path, monkeypatch)
    damaged = locales / relative_path
    text = 'msgid "unused.title"\nmsgstr "Unused"\n'
    if translation is not None:
        text += f'msgid "used.title"\nmsgstr {json.dumps(translation)}\n'
    damaged.write_text(text, encoding="utf-8")
    before = {path: path.read_bytes() for path in locales.rglob("*") if path.is_file()}

    assert catalog.validate_keys() == 1

    output = capsys.readouterr()
    assert "used.title" in output.out + output.err
    assert relative_path in output.out + output.err
    assert {path: path.read_bytes() for path in before} == before


def test_valid_used_keys_allow_empty_pot_translations_and_unused_locale_entries(
    tmp_path: Path, monkeypatch
):
    locales, _pot = _used_key_catalogs(tmp_path, monkeypatch)
    before = {path: path.read_bytes() for path in locales.rglob("*") if path.is_file()}

    assert catalog.validate_keys() == 0
    assert {path: path.read_bytes() for path in before} == before


def test_a_shipped_runtime_locale_without_metadata_still_requires_used_keys(
    tmp_path: Path, monkeypatch, capsys
):
    locales, _pot = _used_key_catalogs(tmp_path, monkeypatch)
    (locales / "lt" / "meta.json").unlink()
    (locales / "lt" / "LC_MESSAGES" / "ui.po").unlink()

    assert catalog.validate_keys() == 1
    assert "lt/LC_MESSAGES/ui.po" in capsys.readouterr().out


@pytest.mark.parametrize("locale", ("en", "lt"))
@pytest.mark.parametrize("damage", ("missing_file", "missing_key", "stale_text", "invalid_file"))
def test_used_keys_require_current_compiled_translations(
    tmp_path: Path, monkeypatch, capsys, locale: str, damage: str
):
    locales, _pot = _used_key_catalogs(tmp_path, monkeypatch)
    compiled = locales / locale / "LC_MESSAGES" / "ui.mo"
    if damage == "missing_file":
        compiled.unlink()
    elif damage == "missing_key":
        _write_compiled_catalog(compiled, {"unused.title": "Unused"})
    elif damage == "stale_text":
        _write_compiled_catalog(compiled, {"used.title": "Outdated translation"})
    else:
        compiled.write_bytes(b"not a compiled gettext catalog")
    before = {path: path.read_bytes() for path in locales.rglob("*") if path.is_file()}

    assert catalog.validate_keys() == 1

    output = capsys.readouterr()
    assert f"{locale}/LC_MESSAGES/ui.mo" in output.out + output.err
    if damage in {"missing_key", "stale_text"}:
        assert "used.title" in output.out + output.err
    assert {path: path.read_bytes() for path in locales.rglob("*") if path.is_file()} == before


def test_compiled_translation_equal_to_key_requires_an_actual_catalog_entry(
    tmp_path: Path, monkeypatch, capsys
):
    locales, _pot = _used_key_catalogs(tmp_path, monkeypatch)
    source = locales / "en" / "LC_MESSAGES" / "ui.po"
    source.write_text('msgid "used.title"\nmsgstr "used.title"\n', encoding="utf-8")
    compiled = source.with_suffix(".mo")
    _write_compiled_catalog(compiled, {"used.title": "used.title"})

    assert catalog.validate_keys() == 0

    _write_compiled_catalog(compiled, {"unused.title": "Unused"})
    assert catalog.validate_keys() == 1
    output = capsys.readouterr()
    assert "used.title" in output.out + output.err
    assert "en/LC_MESSAGES/ui.mo" in output.out + output.err


@pytest.mark.parametrize(
    "field", ["message_key", "label_key", "description_key", "title_key", "suggested_action_key"]
)
def test_declared_metadata_keys_are_validated_in_source_and_runtime_catalogs(
    tmp_path: Path,
    monkeypatch,
    capsys,
    field: str,
) -> None:
    _used_key_catalogs(tmp_path, monkeypatch)
    application = tmp_path / "core/application"
    application.mkdir(parents=True)
    (application / "projection.py").write_text(
        f'projection = {{"{field}": "missing.backend_metadata"}}\n', encoding="utf-8"
    )
    frontend = tmp_path / "assets/frontend/src"
    (frontend / "projection.ts").write_text(
        f'const projection = {{ {field}: "missing.frontend_metadata" }};\n', encoding="utf-8"
    )
    assert catalog._discover_template_and_python_keys() == {
        "used.title",
        "missing.backend_metadata",
        "missing.frontend_metadata",
    }
    assert catalog.validate_keys() == 1
    output = capsys.readouterr().out
    assert "missing.backend_metadata" in output
    assert "missing.frontend_metadata" in output
    for path in ("en/LC_MESSAGES/ui.po", "lt/LC_MESSAGES/ui.po", "ui.pot"):
        assert path in output


def test_metadata_discovery_rejects_dynamic_prefixes_and_ordinary_dotted_strings(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _used_key_catalogs(tmp_path, monkeypatch)
    application = tmp_path / "core/application"
    application.mkdir(parents=True)
    (application / "projection.py").write_text(
        'prefix = {"message_key": "react.dynamic."}\n'
        'owner = {"label": "react.owner_authored"}\n'
        'opaque = ("react.ordinary_tuple", "Owner value")\n'
        'module = "core.application.example"\n',
        encoding="utf-8",
    )
    assert catalog._discover_template_and_python_keys() == {"used.title"}


def test_shipped_dynamic_error_guidance_and_outcome_action_are_discovered() -> None:
    keys = catalog._discover_template_and_python_keys()
    assert {
        "react.extensions.secret_value_required",
        "react.extensions.secret_storage_unavailable",
        "react.extensions.secret_storage_failed",
        "react.extensions.connection_storage_unavailable",
        "react.extensions.project_required_for_action",
        "react.extensions.authorization_required_for_action",
        "react.extensions.invalid_connection_configuration",
        "react.workflows.diagnostic_action_outcome_edge",
    } <= keys


def test_shipped_catalogs_carry_no_phase_gate_labels() -> None:
    # Workflows have no phases; the gate-mode labels for them are referenced nowhere.
    retired = {
        "react.workflows.phase_gate_auto_approve",
        "react.workflows.phase_gate_human_approval",
    }
    sources = [catalog.LOCALES_DIR / f"{catalog.DOMAIN}.pot"]
    sources += sorted(catalog.LOCALES_DIR.glob(f"*/LC_MESSAGES/{catalog.DOMAIN}.po"))
    for path in sources:
        assert not retired & catalog._read_po(path).keys(), path
    for path in sorted(catalog.LOCALES_DIR.glob(f"*/LC_MESSAGES/{catalog.DOMAIN}.mo")):
        with path.open("rb") as stream:
            compiled = read_mo(stream)
        assert not [key for key in retired if compiled.get(key) is not None], path
