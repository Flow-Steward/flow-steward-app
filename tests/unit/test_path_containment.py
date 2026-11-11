from __future__ import annotations

from pathlib import Path

import pytest

from core.infrastructure.extension_runtime.boundary import validate_extension_runtime_command
from core.infrastructure.extension_runtime.extension_runtime_manifest import _resolve_ref_path
from core.infrastructure.extension_runtime.package_identity import managed_extension_dir
from core.infrastructure.security.path_containment import PathEscapeError, contained_path


def test_contained_path_returns_the_real_path_inside_the_root(tmp_path: Path) -> None:
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("ok", encoding="utf-8")

    assert contained_path(tmp_path, "assets/app.js") == (tmp_path / "assets/app.js").resolve()


@pytest.mark.parametrize(
    "part",
    ["../outside.txt", "assets/../../outside.txt", "/etc/passwd", ".", "", "assets/.."],
)
def test_contained_path_refuses_what_leaves_or_is_the_root(tmp_path: Path, part: str) -> None:
    (tmp_path / "assets").mkdir()

    with pytest.raises(PathEscapeError):
        contained_path(tmp_path, part)


def test_contained_path_refuses_a_link_out_of_the_root(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    (root / "link.txt").symlink_to(tmp_path / "secret.txt")

    with pytest.raises(PathEscapeError):
        contained_path(root, "link.txt")
    # Without following links the link itself is inside, and it is returned as is.
    assert contained_path(root, "link.txt", follow_symlinks=False) == root / "link.txt"


def test_contained_path_does_not_accept_a_sibling_sharing_the_root_prefix(tmp_path: Path) -> None:
    (tmp_path / "root").mkdir()
    (tmp_path / "root-sibling").mkdir()

    with pytest.raises(PathEscapeError):
        contained_path(tmp_path / "root", "../root-sibling/file")


def test_managed_extension_dir_stays_directly_under_the_extensions_root(tmp_path: Path) -> None:
    assert managed_extension_dir(tmp_path, "acme.crm-sync") == tmp_path / "acme_crm_sync"
    for hostile in ("../etc", "acme/../../etc", "/etc/passwd", "acme..crm", ""):
        with pytest.raises(ValueError):
            managed_extension_dir(tmp_path, hostile)


def test_manifest_ref_cannot_leave_the_bundle(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    manifest = bundle / "extension.yaml"
    manifest.write_text("{}", encoding="utf-8")
    (tmp_path / "outside.yaml").write_text("{}", encoding="utf-8")
    (bundle / "inside.yaml").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="escapes extension bundle root"):
        _resolve_ref_path(bundle_root=bundle, parent_path=manifest, ref_text="../outside.yaml")
    assert (
        _resolve_ref_path(bundle_root=bundle, parent_path=manifest, ref_text="inside.yaml")
        == (bundle / "inside.yaml").resolve()
    )


def test_runtime_command_script_linked_out_of_the_bundle_is_refused(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (tmp_path / "evil.py").write_text("print('x')\n", encoding="utf-8")
    (bundle / "main.py").symlink_to(tmp_path / "evil.py")

    issues = validate_extension_runtime_command(
        bundle_root=bundle, command=["python", "main.py"], field_path="entrypoint.command"
    )

    assert [issue["code"] for issue in issues] == ["extension_command_path_escape"]
