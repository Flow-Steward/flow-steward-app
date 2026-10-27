#!/usr/bin/env python3
"""Manage UI gettext catalogs for key-based translations."""

from __future__ import annotations

import argparse
import ast
import json
import re
import shutil
import struct
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from babel.messages.mofile import read_mo

MSGSTR_VALUE = 'msgstr ""'


REPO_ROOT = Path(__file__).resolve().parents[4]
LOCALES_DIR = REPO_ROOT / "locales"
DOMAIN = "ui"
DEFAULT_LOCALE = "en"
POT_PATH = LOCALES_DIR / f"{DOMAIN}.pot"


@dataclass(frozen=True)
class LocaleMeta:
    code: str
    label: str
    native_label: str


def _parse_source_constants(
    source_path: Path,
) -> tuple[dict[str, dict[str, str]], list[LocaleMeta]]:
    source = source_path.read_text(encoding="utf-8")
    module = ast.parse(source)

    translations: dict[str, dict[str, str]] | None = None
    locales: list[dict[str, str]] | None = None

    for node in module.body:
        target_name = None
        value_node: ast.AST | None = None

        if isinstance(node, ast.Assign) and node.targets and isinstance(node.targets[0], ast.Name):
            target_name = node.targets[0].id
            value_node = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target_name = node.target.id
            value_node = node.value

        if target_name == "_UI_TRANSLATIONS" and value_node is not None:
            parsed = ast.literal_eval(value_node)
            translations = {
                str(locale): {str(key): str(text) for key, text in dict(messages).items()}
                for locale, messages in dict(parsed).items()
            }
        elif target_name == "SUPPORTED_UI_LOCALES" and value_node is not None:
            parsed_locales = ast.literal_eval(value_node)
            locales = [
                {
                    "code": str(item.get("code") or "").strip(),
                    "label": str(item.get("label") or "").strip(),
                    "native_label": str(item.get("native_label") or "").strip(),
                }
                for item in parsed_locales
            ]

    if not translations:
        raise RuntimeError(f"Could not find _UI_TRANSLATIONS in {source_path}")
    if locales is None:
        locales = []

    metadata: list[LocaleMeta] = []
    for code in sorted(translations):
        locale_row = next((row for row in locales or [] if row.get("code") == code), None) or {}
        metadata.append(
            LocaleMeta(
                code=code,
                label=locale_row.get("label") or code,
                native_label=locale_row.get("native_label") or code,
            )
        )
    return translations, metadata


def _po_escape(text: str) -> str:
    return str(text or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _write_po(path: Path, locale: str, messages: dict[str, str]) -> None:
    lines: list[str] = [
        'msgid ""',
        MSGSTR_VALUE,
        f'"Project-Id-Version: {DOMAIN}\\n"',
        f'"Language: {locale}\\n"',
        '"MIME-Version: 1.0\\n"',
        '"Content-Type: text/plain; charset=UTF-8\\n"',
        '"Content-Transfer-Encoding: 8bit\\n"',
        '"Generated-By: core/application/tooling/ui_i18n_catalog.py\\n"',
        "",
    ]

    for key in sorted(messages):
        lines.append(f'msgid "{_po_escape(key)}"')
        lines.append(f'msgstr "{_po_escape(messages[key])}"')
        lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _write_pot(path: Path, keys: list[str]) -> None:
    lines: list[str] = [
        'msgid ""',
        MSGSTR_VALUE,
        f'"Project-Id-Version: {DOMAIN}\\n"',
        '"MIME-Version: 1.0\\n"',
        '"Content-Type: text/plain; charset=UTF-8\\n"',
        '"Content-Transfer-Encoding: 8bit\\n"',
        '"Generated-By: core/application/tooling/ui_i18n_catalog.py\\n"',
        "",
    ]
    for key in sorted(set(keys)):
        lines.append(f'msgid "{_po_escape(key)}"')
        lines.append(MSGSTR_VALUE)
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _read_po(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    entries: dict[str, str] = {}
    fields = {"msgid": "", "msgstr": ""}
    active_field = ""
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("msgid "):
            if fields["msgid"]:
                entries[fields["msgid"]] = fields["msgstr"]
            fields = {"msgid": ast.literal_eval(line[6:]), "msgstr": ""}
            active_field = "msgid"
        elif line.startswith("msgstr "):
            fields["msgstr"] = ast.literal_eval(line[7:])
            active_field = "msgstr"
        elif line.startswith('"') and active_field:
            fields[active_field] += ast.literal_eval(line)
        elif line.startswith(("msgid_plural ", "msgstr[")):
            # Platform catalogs use symbolic singular keys, not plural tuples.
            active_field = ""
    if fields["msgid"]:
        entries[fields["msgid"]] = fields["msgstr"]
    return entries


def _write_locale_meta(path: Path, meta: LocaleMeta) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "code": meta.code,
        "label": meta.label,
        "native_label": meta.native_label,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _run_pybabel(args: list[str]) -> int:
    pybabel_binary = shutil.which("pybabel")
    if pybabel_binary:
        process = subprocess.run([pybabel_binary, *args], check=False)
        return int(process.returncode)
    process = subprocess.run([sys.executable, "-m", "babel.messages.frontend", *args], check=False)
    if process.returncode != 0:
        print(
            "ERROR: pybabel is not installed in the current interpreter environment.",
            file=sys.stderr,
        )
    return int(process.returncode)


def _discover_template_and_python_keys() -> set[str]:
    key_capture = r"([a-z][a-z0-9_.-]*[a-z0-9])"
    patterns: tuple[tuple[Path, tuple[str, ...], str], ...] = (
        (REPO_ROOT / "core", ("*.py",), rf"\btranslate_ui_text\(\s*['\"]{key_capture}['\"]"),
        (
            REPO_ROOT / "assets" / "frontend" / "src",
            ("*.ts", "*.tsx"),
            rf"\bt\(\s*['\"]{key_capture}['\"]",
        ),
    )
    # UI projections carry literal translation keys without calling t() here.
    metadata_fields = "(?:message_key|label_key|description_key|title_key|suggested_action_key)"
    metadata_pattern = re.compile(
        rf"(?<![\w.])['\"]?{metadata_fields}['\"]?\s*[:=]\s*['\"]{key_capture}['\"]"
    )
    keys: set[str] = set()
    for root, globs, regex in patterns:
        files = [file_path for glob in globs for file_path in root.rglob(glob)]
        pattern = re.compile(regex)
        for file_path in files:
            if ".test." in file_path.name or ".test-" in file_path.name:
                continue
            content = file_path.read_text(encoding="utf-8")
            for match in (*pattern.findall(content), *metadata_pattern.findall(content)):
                if "." in match:
                    keys.add(match)
    return keys


def bootstrap_catalog(*, source: str) -> int:
    """Generate gettext catalogs from typed source path input."""
    source_path = Path(source).resolve()
    try:
        translations, metadata = _parse_source_constants(source_path)
    except RuntimeError as exc:
        default_po = LOCALES_DIR / DEFAULT_LOCALE / "LC_MESSAGES" / f"{DOMAIN}.po"
        if default_po.exists():
            print(
                "In-code registry was not found in the source file; "
                "catalogs already exist, so bootstrap is treated as complete."
            )
            return 0
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    keys = sorted(translations.get(DEFAULT_LOCALE, {}).keys())
    if not keys:
        print(
            f"ERROR: default locale '{DEFAULT_LOCALE}' has no messages in {source_path}",
            file=sys.stderr,
        )
        return 1

    for locale_meta in metadata:
        locale_messages = translations.get(locale_meta.code, {})
        po_path = LOCALES_DIR / locale_meta.code / "LC_MESSAGES" / f"{DOMAIN}.po"
        meta_path = LOCALES_DIR / locale_meta.code / "meta.json"
        _write_po(po_path, locale_meta.code, locale_messages)
        _write_locale_meta(meta_path, locale_meta)
        print(f"Wrote {po_path.relative_to(REPO_ROOT)} ({len(locale_messages)} messages)")
        print(f"Wrote {meta_path.relative_to(REPO_ROOT)}")

    _write_pot(POT_PATH, keys)
    print(f"Wrote {POT_PATH.relative_to(REPO_ROOT)} ({len(keys)} keys)")
    return 0


def rebuild_pot() -> int:
    """Rebuild the POT file from the default locale catalog."""
    default_po = LOCALES_DIR / DEFAULT_LOCALE / "LC_MESSAGES" / f"{DOMAIN}.po"
    messages = _read_po(default_po)
    if not messages:
        print(f"ERROR: default catalog missing or empty: {default_po}", file=sys.stderr)
        return 1
    _write_pot(POT_PATH, sorted(messages.keys()))
    print(f"Wrote {POT_PATH.relative_to(REPO_ROOT)} ({len(messages)} keys)")
    return 0


def init_locale(*, code: str, label: str = "", native_label: str = "") -> int:
    """Initialize one locale from the current POT file."""
    code = str(code or "").strip().lower()
    if not code:
        print("ERROR: locale code is required", file=sys.stderr)
        return 1
    if not POT_PATH.exists():
        print("POT file missing, rebuilding from default locale...")
        if rebuild_pot() != 0:
            return 1
    result = _run_pybabel(
        [
            "init",
            "-D",
            DOMAIN,
            "-d",
            str(LOCALES_DIR),
            "-i",
            str(POT_PATH),
            "-l",
            code,
        ]
    )
    if result != 0:
        return result
    meta = LocaleMeta(
        code=code,
        label=label or code,
        native_label=native_label or (label or code),
    )
    _write_locale_meta(LOCALES_DIR / code / "meta.json", meta)
    print(f"Initialized locale '{code}' and wrote metadata.")
    return 0


def update_catalogs() -> int:
    """Update locale catalogs from the current POT file."""
    if rebuild_pot() != 0:
        return 1
    return _run_pybabel(
        [
            "update",
            "-D",
            DOMAIN,
            "-d",
            str(LOCALES_DIR),
            "-i",
            str(POT_PATH),
            "--no-fuzzy-matching",
        ]
    )


def compile_catalogs(*, force: bool = False) -> int:
    """Compile locale catalogs to .mo files."""
    pybabel_args = ["compile", "-D", DOMAIN, "-d", str(LOCALES_DIR)]
    if force:
        pybabel_args.append("-f")
    return _run_pybabel(pybabel_args)


def _validate_compiled_catalog(
    po_path: Path, messages: dict[str, str], referenced_keys: set[str]
) -> bool:
    path = po_path.with_suffix(".mo")
    try:
        with path.open("rb") as stream:
            compiled = read_mo(stream)
    except (OSError, ValueError, struct.error) as exc:
        print(f"ERROR: compiled catalog unreadable: {path.relative_to(REPO_ROOT)}: {exc}")
        return False
    stale = sorted(
        key
        for key in referenced_keys & messages.keys()
        if (message := compiled.get(key)) is None or message.string != messages[key]
    )
    if stale:
        print(f"Missing or stale used UI i18n keys in {path.relative_to(REPO_ROOT)}:")
        print("\n".join(f"- {key}" for key in stale))
        return False
    return True


def validate_keys() -> int:
    """Validate used keys in source/runtime locale catalogs and the portable template."""
    # Metadata declares a source locale before compilation; .mo files identify
    # runtime locales even when their optional display metadata is absent.
    locale_codes = {DEFAULT_LOCALE}
    locale_codes.update(path.parent.name for path in LOCALES_DIR.glob("*/meta.json"))
    locale_codes.update(
        path.parent.parent.name for path in LOCALES_DIR.glob(f"*/LC_MESSAGES/{DOMAIN}.mo")
    )
    referenced_keys = _discover_template_and_python_keys()
    catalogs = [
        (LOCALES_DIR / code / "LC_MESSAGES" / f"{DOMAIN}.po", True) for code in sorted(locale_codes)
    ]
    catalogs.append((LOCALES_DIR / f"{DOMAIN}.pot", False))
    failed = False
    for path, require_translation in catalogs:
        messages = _read_po(path)
        missing = sorted(referenced_keys - messages.keys())
        empty = sorted(
            key
            for key in referenced_keys & messages.keys()
            if require_translation and not messages[key].strip()
        )
        if not path.is_file() or not messages:
            print(f"ERROR: catalog missing or empty: {path.relative_to(REPO_ROOT)}")
            failed = True
        if require_translation:
            failed = not _validate_compiled_catalog(path, messages, referenced_keys) or failed
        for label, keys in (("Missing", missing), ("Empty", empty)):
            if keys:
                print(f"{label} used UI i18n keys in {path.relative_to(REPO_ROOT)}:")
                print("\n".join(f"- {key}" for key in keys))
                failed = True
    if failed:
        return 1
    print(f"OK: validated {len(referenced_keys)} referenced keys in {len(catalogs)} catalogs")
    return 0


def cmd_bootstrap_catalog(args: argparse.Namespace) -> int:
    return bootstrap_catalog(source=str(args.source))


def cmd_rebuild_pot(_: argparse.Namespace) -> int:
    return rebuild_pot()


def cmd_init_locale(args: argparse.Namespace) -> int:
    return init_locale(
        code=str(args.code or ""),
        label=str(args.label or ""),
        native_label=str(args.native_label or ""),
    )


def cmd_update(_: argparse.Namespace) -> int:
    return update_catalogs()


def cmd_compile(args: argparse.Namespace) -> int:
    return compile_catalogs(force=bool(args.force))


def cmd_validate_keys(_: argparse.Namespace) -> int:
    return validate_keys()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage key-based UI gettext catalogs.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    migrate = subparsers.add_parser(
        "bootstrap-catalog",
        help=(
            "Generate locales/*.po + metadata from the in-code _UI_TRANSLATIONS registry in "
            "core/application/settings/ui_i18n.py"
        ),
    )
    migrate.add_argument(
        "--source",
        default=str(REPO_ROOT / "core" / "application" / "settings" / "ui_i18n.py"),
        help="Source ui_i18n.py path",
    )
    migrate.set_defaults(func=cmd_bootstrap_catalog)

    rebuild_pot = subparsers.add_parser(
        "rebuild-pot",
        help="Rebuild locales/ui.pot from default locale catalog keys",
    )
    rebuild_pot.set_defaults(func=cmd_rebuild_pot)

    init_locale = subparsers.add_parser(
        "init-locale",
        help="Initialize a new locale catalog from locales/ui.pot",
    )
    init_locale.add_argument("code", help="Locale code, e.g. de")
    init_locale.add_argument("--label", default="", help="Display label")
    init_locale.add_argument("--native-label", default="", help="Native display label")
    init_locale.set_defaults(func=cmd_init_locale)

    update = subparsers.add_parser(
        "update",
        help="Update locale catalogs from locales/ui.pot",
    )
    update.set_defaults(func=cmd_update)

    compile_cmd = subparsers.add_parser(
        "compile",
        help="Compile locale catalogs into .mo files via pybabel",
    )
    compile_cmd.add_argument(
        "-f", "--force", action="store_true", help="Force overwrite compiled catalogs"
    )
    compile_cmd.set_defaults(func=cmd_compile)

    validate = subparsers.add_parser(
        "validate-keys",
        help="Validate used i18n keys in supported locale catalogs and the POT template",
    )
    validate.set_defaults(func=cmd_validate_keys)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
