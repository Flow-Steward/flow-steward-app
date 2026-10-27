from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from defusedxml.common import EntitiesForbidden

from core.application.tooling.quality.sonar_coverage import main, normalize_coverage_report


def _write_report(path: Path, *, source: str, filenames: list[str]) -> None:
    coverage = ET.Element("coverage")
    sources = ET.SubElement(coverage, "sources")
    ET.SubElement(sources, "source").text = source
    packages = ET.SubElement(coverage, "packages")
    package = ET.SubElement(packages, "package", name="application")
    classes = ET.SubElement(package, "classes")
    for filename in filenames:
        ET.SubElement(classes, "class", name=Path(filename).name, filename=filename)
    ET.ElementTree(coverage).write(path, encoding="utf-8", xml_declaration=True)


def test_core_only_coverage_is_mapped_back_to_the_repository(tmp_path: Path) -> None:
    repo_root = tmp_path / "repository"
    source_file = repo_root / "core/application/example.py"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("VALUE = 1\n", encoding="utf-8")

    report = tmp_path / "coverage.xml"
    normalized = tmp_path / "coverage.sonar.xml"
    _write_report(
        report,
        source="/tmp/flow-steward-core-only.A1b2C3/core",
        filenames=["application/example.py", "application/missing.py"],
    )

    removed = normalize_coverage_report(report, normalized, repo_root)

    root = ET.parse(normalized).getroot()
    assert [node.text for node in root.findall("./sources/source")] == ["/usr/src"]
    assert [node.get("filename") for node in root.findall(".//class")] == [
        "core/application/example.py"
    ]
    assert removed == 1


def test_container_coverage_keeps_repository_relative_paths(tmp_path: Path) -> None:
    repo_root = tmp_path / "repository"
    source_file = repo_root / "core/example.py"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("VALUE = 1\n", encoding="utf-8")

    report = tmp_path / "coverage.xml"
    normalized = tmp_path / "coverage.sonar.xml"
    _write_report(report, source="/app", filenames=["core/example.py"])

    removed = normalize_coverage_report(report, normalized, repo_root)

    root = ET.parse(normalized).getroot()
    assert [node.text for node in root.findall("./sources/source")] == ["/usr/src"]
    assert [node.get("filename") for node in root.findall(".//class")] == ["core/example.py"]
    assert removed == 0


def test_coverage_entities_are_rejected(tmp_path: Path) -> None:
    report = tmp_path / "coverage.xml"
    report.write_text(
        """<?xml version="1.0"?>
<!DOCTYPE coverage [<!ENTITY external SYSTEM "file:///etc/passwd">]>
<coverage><sources><source>&external;</source></sources></coverage>
""",
        encoding="utf-8",
    )

    with pytest.raises(EntitiesForbidden):
        normalize_coverage_report(report, tmp_path / "normalized.xml", tmp_path)


def test_app_subdirectory_source_is_mapped_to_repository(tmp_path: Path) -> None:
    repo_root = tmp_path / "repository"
    source_file = repo_root / "core/example.py"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("VALUE = 1\n", encoding="utf-8")

    report = tmp_path / "coverage.xml"
    normalized = tmp_path / "coverage.sonar.xml"
    _write_report(report, source="/app/core", filenames=["example.py"])

    assert normalize_coverage_report(report, normalized, repo_root) == 0
    root = ET.parse(normalized).getroot()
    assert [node.get("filename") for node in root.findall(".//class")] == ["core/example.py"]


def test_absolute_coverage_paths_are_mapped_to_repository(tmp_path: Path) -> None:
    repo_root = tmp_path / "repository"
    local_file = repo_root / "core/local.py"
    container_file = repo_root / "core/container.py"
    local_file.parent.mkdir(parents=True)
    local_file.write_text("LOCAL = 1\n", encoding="utf-8")
    container_file.write_text("CONTAINER = 1\n", encoding="utf-8")

    report = tmp_path / "coverage.xml"
    normalized = tmp_path / "coverage.sonar.xml"
    _write_report(
        report,
        source=str(repo_root / "core"),
        filenames=[str(local_file), "/app/core/container.py", "/app"],
    )

    assert normalize_coverage_report(report, normalized, repo_root) == 1
    root = ET.parse(normalized).getroot()
    assert [node.get("filename") for node in root.findall(".//class")] == [
        "core/local.py",
        "core/container.py",
    ]


def test_missing_sources_element_is_created(tmp_path: Path) -> None:
    repo_root = tmp_path / "repository"
    source_file = repo_root / "core/example.py"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("VALUE = 1\n", encoding="utf-8")

    report = tmp_path / "coverage.xml"
    normalized = tmp_path / "coverage.sonar.xml"
    coverage = ET.Element("coverage")
    packages = ET.SubElement(coverage, "packages")
    package = ET.SubElement(packages, "package", name="application")
    classes = ET.SubElement(package, "classes")
    ET.SubElement(classes, "class", name="example.py", filename="core/example.py")
    ET.ElementTree(coverage).write(report, encoding="utf-8", xml_declaration=True)

    assert normalize_coverage_report(report, normalized, repo_root) == 0
    root = ET.parse(normalized).getroot()
    assert [node.text for node in root.findall("./sources/source")] == ["/usr/src"]


def test_cli_reports_removed_non_repository_classes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root = tmp_path / "repository"
    repo_root.mkdir()
    report = tmp_path / "coverage.xml"
    normalized = tmp_path / "coverage.sonar.xml"
    _write_report(report, source="/outside/repository", filenames=["missing.py"])
    monkeypatch.setattr(
        "sys.argv",
        ["sonar_coverage", str(report), str(normalized), str(repo_root)],
    )

    assert main() == 0
    assert normalized.is_file()
    assert capsys.readouterr().out.splitlines() == [
        "[lint] Sonar Python coverage: wrote ../coverage.sonar.xml.",
        "[lint] Sonar Python coverage: removed 1 non-repository class entries.",
    ]
