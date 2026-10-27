from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_every_explicit_sonar_source_path_exists() -> None:
    for relative_path in (
        "infra/tooling/sonar-project.py.properties",
        "infra/tooling/sonar-project.full.properties",
    ):
        properties = (ROOT / relative_path).read_text(encoding="utf-8")
        source_line = next(
            line for line in properties.splitlines() if line.startswith("sonar.sources=")
        )
        for source_path in source_line.partition("=")[2].split(","):
            assert (ROOT / source_path).exists(), f"{relative_path}: {source_path} does not exist"


def test_frontend_pages_are_included_in_every_frontend_sonar_profile() -> None:
    for relative_path in (
        "infra/tooling/sonar-project.properties",
        "infra/tooling/sonar-project.js.properties",
        "infra/tooling/sonar-project.full.properties",
    ):
        properties = (ROOT / relative_path).read_text(encoding="utf-8")
        exclusion_lines = [
            line for line in properties.splitlines() if line.startswith("sonar.exclusions=")
        ]
        assert exclusion_lines, relative_path
        assert all("assets/frontend/src/pages/**" not in line for line in exclusion_lines), (
            relative_path
        )
