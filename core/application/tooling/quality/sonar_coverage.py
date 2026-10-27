"""Normalize Cobertura paths for the Sonar scanner container."""

from __future__ import annotations

import argparse
import os
import xml.etree.ElementTree as ET
from pathlib import Path

from defusedxml.ElementTree import parse as parse_xml

APP_CONTAINER_ROOT = "/app"


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _canonical_source_root(source: str, repo_root: Path) -> Path | None:
    source_path = Path(source)
    if source == APP_CONTAINER_ROOT:
        return repo_root
    app_prefix = f"{APP_CONTAINER_ROOT}/"
    if source.startswith(app_prefix):
        return repo_root / source.removeprefix(app_prefix)

    resolved_source = source_path.resolve()
    if _is_within(resolved_source, repo_root):
        return resolved_source

    if source_path.name == "core" and source_path.parent.name.startswith("flow-steward-core-only."):
        return repo_root / "core"
    return None


def _repository_path(
    filename: str,
    *,
    repo_root: Path,
    source_roots: list[tuple[Path, Path]],
) -> Path | None:
    filename_path = Path(filename)
    candidates: list[Path] = []

    if filename_path.is_absolute():
        if filename == APP_CONTAINER_ROOT:
            candidates.append(repo_root)
        else:
            app_prefix = f"{APP_CONTAINER_ROOT}/"
            if filename.startswith(app_prefix):
                candidates.append(repo_root / filename.removeprefix(app_prefix))
        if _is_within(filename_path.resolve(), repo_root):
            candidates.append(filename_path)
        for original_root, canonical_root in source_roots:
            try:
                relative = filename_path.relative_to(original_root)
            except ValueError:
                continue
            candidates.append(canonical_root / relative)
    else:
        candidates.append(repo_root / filename_path)
        candidates.extend(canonical_root / filename_path for _, canonical_root in source_roots)

    for candidate in candidates:
        resolved_candidate = candidate.resolve()
        if _is_within(resolved_candidate, repo_root) and resolved_candidate.is_file():
            return resolved_candidate.relative_to(repo_root)
    return None


def normalize_coverage_report(
    source_report: Path,
    target_report: Path,
    repo_root: Path,
) -> int:
    """Rewrite coverage paths to the repository mounted at ``/usr/src``.

    Tier 3 tests execute in a temporary ``flow-steward-core-only.*`` checkout.
    Coverage.py therefore records that checkout's ``core`` directory as the
    source and emits filenames relative to it. Sonar runs later against the
    canonical checkout, so every retained class is rewritten to a path relative
    to the repository root and the sole Cobertura source becomes ``/usr/src``.
    """

    repo_root = repo_root.resolve()
    tree = parse_xml(source_report)
    coverage = tree.getroot()

    sources = coverage.find("sources")
    if sources is None:
        sources = ET.SubElement(coverage, "sources")

    source_roots: list[tuple[Path, Path]] = []
    for source_node in sources.findall("source"):
        source = (source_node.text or "").strip()
        canonical_root = _canonical_source_root(source, repo_root) if source else None
        if canonical_root is not None:
            source_roots.append((Path(source), canonical_root.resolve()))

    for source_node in sources.findall("source"):
        sources.remove(source_node)
    ET.SubElement(sources, "source").text = "/usr/src"

    removed_classes = 0
    for classes in coverage.findall(".//classes"):
        for class_node in classes.findall("class"):
            filename = str(class_node.get("filename") or "").strip()
            if not filename:
                continue
            repository_path = _repository_path(
                filename,
                repo_root=repo_root,
                source_roots=source_roots,
            )
            if repository_path is None:
                classes.remove(class_node)
                removed_classes += 1
                continue
            class_node.set("filename", repository_path.as_posix())

    target_report.parent.mkdir(parents=True, exist_ok=True)
    tree.write(target_report, encoding="utf-8", xml_declaration=True)
    return removed_classes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_report", type=Path)
    parser.add_argument("target_report", type=Path)
    parser.add_argument("repo_root", type=Path)
    args = parser.parse_args()

    removed_classes = normalize_coverage_report(
        args.source_report,
        args.target_report,
        args.repo_root,
    )
    print(
        "[lint] Sonar Python coverage: wrote "
        f"{os.path.relpath(args.target_report, args.repo_root)}."
    )
    if removed_classes:
        print(
            f"[lint] Sonar Python coverage: removed {removed_classes} non-repository class entries."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
