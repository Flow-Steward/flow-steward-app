"""Installs whose files are gone, and folders that are not extensions.

Seen in the demo: rows named after folders ("acme_file_sync") with
"—" author and "Uncategorized", whose folders held only a ``.fs-python``
dependency cache; their Uninstall asked for a plan under the folder name and
got 404. The install records (``acme.file-sync``) were disabled with
``extension_invalid_or_missing``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.application.extensions.install import ExtensionInstallRecord
from core.application.extensions.query_list import (
    FILES_MISSING_REASON,
    ExtensionQueryList,
    extension_files_missing,
)
from core.application.extensions.query_presentation import ExtensionPresentationQuery


@pytest.fixture
def extensions_root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "extensions"
    root.mkdir()
    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
        lambda: str(root),
    )
    return root


def _record(state: str = "disabled") -> ExtensionInstallRecord:
    return ExtensionInstallRecord(
        extension_id="acme.file-sync",
        version="1.0.0",
        account_id="acc-1",
        install_id="pi_ft",
        state=state,
    )


def test_a_folder_holding_only_a_dependency_cache_is_not_an_extension(
    extensions_root: Path,
) -> None:
    from core.infrastructure.extension_runtime.extension_runtime import scan_extensions

    (extensions_root / "acme_file_sync" / ".fs-python" / "3.12" / "lib").mkdir(
        parents=True
    )
    (
        extensions_root / "acme_file_sync" / ".fs-python" / "3.12" / "lib" / "x.py"
    ).write_text("", encoding="utf-8")

    assert scan_extensions(str(extensions_root)) == []


def test_a_disabled_install_without_its_folder_says_files_are_missing(
    extensions_root: Path,
) -> None:
    (extensions_root / "acme_file_sync" / ".fs-python").mkdir(parents=True)

    assert extension_files_missing(_record()) is True
    # An active install is never reported missing, and a folder with its
    # manifest is not missing.
    assert extension_files_missing(_record("active")) is False
    (extensions_root / "acme_file_sync" / "extension.yaml").write_text(
        "extension_id: acme.file-sync\n", encoding="utf-8"
    )
    assert extension_files_missing(_record()) is False


def test_the_installed_row_names_the_install_and_says_what_to_do(extensions_root: Path) -> None:
    summary = ExtensionQueryList(
        get_logger=lambda: None,
        get_install_store=lambda: None,
        get_runtime_store=lambda: None,
    ).summary_from_install_record(
        record=_record(),
        viewer_account_id="acc-1",
        release=None,
        entitlement=None,
        release_artifacts=[],
        connected_count=0,
    )

    assert summary.extension_id == "acme.file-sync"
    assert summary.version == "1.0.0"
    assert summary.files_missing is True
    assert summary.broken is True
    assert summary.broken_reason == FILES_MISSING_REASON == "Files missing. Uninstall to clean up."


def test_a_scanned_folder_without_a_manifest_is_not_listed() -> None:
    records = [
        {
            "extension_key": "acme_file_sync",
            "status": "invalid",
            "compile_errors": [{"code": "missing_manifest", "message": "No manifest file found"}],
        },
        {"extension_key": "acme.valid", "status": "valid", "manifest_path": "/x/extension.yaml"},
    ]
    presentation = ExtensionPresentationQuery(list_runtime_records=lambda **_k: records)

    summaries = presentation.runtime_summaries(scope=None)

    assert [row.extension_id for row in summaries] == ["acme.valid"]
