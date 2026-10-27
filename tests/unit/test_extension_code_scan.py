"""The static look at an extension's own code before it is installed.

Not the security boundary (Python reaches anything indirectly, and dependencies
legitimately use the network, files and processes); it refuses what an
extension's own code has no business doing in plain sight, and lists what an
administrator should look at.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.infrastructure.extension_runtime.extension_code_scan import scan_extension_code


def _bundle(tmp_path: Path, files: dict[str, str]) -> Path:
    for relative, source in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    ("source", "mentions"),
    [
        ("exec(payload)\n", "exec()"),
        ("eval(expression)\n", "eval()"),
        ("code = compile(text, 'x', 'exec')\n", "compile()"),
        ("module = __import__(name)\n", "__import__()"),
        ("import importlib\nmodule = importlib.import_module(name)\n", "importlib.import_module()"),
        ("import ctypes\n", "import ctypes"),
        ("from cffi import FFI\n", "import cffi"),
        ("import os\nos.system('id')\n", "os.system()"),
        ("import os as o\no.popen('id')\n", "os.popen()"),
        ("from os import system\nsystem('id')\n", "os.system()"),
        ("import os\nos.execv('/bin/sh', ['sh'])\n", "os.execv()"),
        ("import os\nos.spawnl(0, '/bin/sh', 'sh')\n", "os.spawnl()"),
        ("import os\nos.fork()\n", "os.fork()"),
        ("import subprocess\nsubprocess.run('id', shell=True)\n", "subprocess.run(shell=...)"),
        ("import subprocess\nsubprocess.Popen(cmd, shell=flag)\n", "subprocess.Popen(shell=...)"),
        ("import asyncio\nasyncio.create_subprocess_shell('id')\n", "create_subprocess_shell()"),
        ("import pickle\npickle.loads(data)\n", "pickle.loads()"),
        ("import marshal\nmarshal.loads(data)\n", "marshal.loads()"),
    ],
)
def test_code_with_no_business_in_an_extension_is_refused(
    tmp_path: Path, source: str, mentions: str
) -> None:
    errors, _warnings = scan_extension_code(_bundle(tmp_path, {"main.py": source}))

    assert [error["code"] for error in errors] == ["extension_code_forbidden"]
    assert mentions in errors[0]["message"]
    assert errors[0]["file_path"] == "main.py"
    assert errors[0]["message"].startswith("main.py:")


@pytest.mark.parametrize(
    ("source", "mentions"),
    [
        ("import socket\n", "import socket"),
        ("import subprocess\nsubprocess.run(['git', 'status'])\n", "subprocess.run()"),
        ("import subprocess\nsubprocess.check_output(['ls'], shell=False)\n", "check_output()"),
        ("data = open('/etc/passwd').read()\n", "open('/etc/passwd')"),
        ("import os\nos.chmod(path, 0o777)\n", "os.chmod()"),
        ("import os\nos.setuid(0)\n", "os.setuid()"),
    ],
)
def test_code_worth_a_look_is_listed_for_review(tmp_path: Path, source: str, mentions: str) -> None:
    errors, warnings = scan_extension_code(_bundle(tmp_path, {"main.py": source}))

    assert errors == []
    assert [warning["code"] for warning in warnings] == ["extension_code_review"]
    assert mentions in warnings[0]["message"]


def test_ordinary_extension_code_passes(tmp_path: Path) -> None:
    source = """
import json
import re
import importlib
from re import compile
from urllib import request

PATTERN = compile(r"\\d+")
helpers = importlib.import_module("helpers")


def handle(payload: dict) -> dict:
    with open("config.json", encoding="utf-8") as handle:
        settings = json.load(handle)
    return {"ok": True, "count": len(PATTERN.findall(json.dumps(payload))), **settings}
"""
    errors, warnings = scan_extension_code(_bundle(tmp_path, {"main.py": source}))

    assert errors == []
    assert warnings == []


def test_tests_caches_and_environments_are_not_scanned(tmp_path: Path) -> None:
    bundle = _bundle(
        tmp_path,
        {
            "main.py": "print('ok')\n",
            "tests/test_main.py": "import os\nos.system('pytest')\n",
            "test_helpers.py": "exec('x = 1')\n",
            "conftest.py": "eval('1')\n",
            ".venv/lib/site.py": "import ctypes\n",
            "__pycache__/x.py": "import ctypes\n",
            "dev-wheels/tool.py": "import ctypes\n",
        },
    )

    assert scan_extension_code(bundle) == ([], [])


def test_nested_package_code_is_scanned(tmp_path: Path) -> None:
    errors, _warnings = scan_extension_code(
        _bundle(tmp_path, {"vendor/helper/run.py": "import os\nos.system('id')\n"})
    )

    assert errors[0]["file_path"] == "vendor/helper/run.py"


def test_code_that_does_not_parse_is_refused(tmp_path: Path) -> None:
    errors, _warnings = scan_extension_code(_bundle(tmp_path, {"main.py": "def broken(:\n"}))

    assert [error["code"] for error in errors] == ["extension_code_unparseable"]


def test_a_missing_bundle_has_nothing_to_report(tmp_path: Path) -> None:
    assert scan_extension_code(tmp_path / "missing") == ([], [])


def test_inline_python_from_the_manifest_is_reviewed(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, {"main.py": "print('ok')\n"})

    errors, _warnings = scan_extension_code(
        bundle, [("entrypoint", ["python3", "-c", "import os; os.system('id')"])]
    )

    assert [error["code"] for error in errors] == ["extension_code_forbidden"]
    assert errors[0]["message"].startswith("<entrypoint -c>:1: os.system()")


def test_a_file_the_manifest_runs_is_reviewed_even_under_tests(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, {"tests/run_me.py": "exec('x = 1')\n"})

    errors, _warnings = scan_extension_code(bundle, [("health", ["python3", "tests/run_me.py"])])

    assert [error["file_path"] for error in errors] == ["tests/run_me.py"]


def test_one_file_larger_than_the_budget_is_refused_before_parsing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.infrastructure.extension_runtime import extension_code_scan

    monkeypatch.setattr(extension_code_scan, "MAX_SCANNED_BYTES", 1024)
    parsed: list[str] = []
    monkeypatch.setattr(
        extension_code_scan, "_scan_source", lambda _s, label, *_a: parsed.append(label)
    )
    bundle = _bundle(tmp_path, {"main.py": "x = 1\n" + "#" * 2000 + "\n"})

    errors, _warnings = scan_extension_code(bundle)

    assert [error["code"] for error in errors] == ["extension_code_too_large"]
    assert parsed == []


def test_the_last_file_overflowing_the_budget_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.infrastructure.extension_runtime import extension_code_scan

    monkeypatch.setattr(extension_code_scan, "MAX_SCANNED_BYTES", 1000)
    bundle = _bundle(tmp_path, {"a.py": "#" * 600 + "\n", "b.py": "#" * 600 + "\n"})

    errors, _warnings = scan_extension_code(bundle)

    assert [error["code"] for error in errors] == ["extension_code_too_large"]
    assert errors[0]["file_path"] == "b.py"


def test_code_exactly_at_the_budget_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from core.infrastructure.extension_runtime import extension_code_scan

    monkeypatch.setattr(extension_code_scan, "MAX_SCANNED_BYTES", 1000)
    bundle = _bundle(tmp_path, {"a.py": "#" * 499 + "\n", "b.py": "#" * 499 + "\n"})

    assert scan_extension_code(bundle) == ([], [])


def test_too_many_files_are_refused_without_listing_them_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.infrastructure.extension_runtime import extension_code_scan

    monkeypatch.setattr(extension_code_scan, "MAX_SCANNED_FILES", 3)
    bundle = _bundle(tmp_path, {f"m{index}.py": "x = 1\n" for index in range(5)})

    errors, _warnings = scan_extension_code(bundle)

    assert [error["code"] for error in errors] == ["extension_code_too_large"]
