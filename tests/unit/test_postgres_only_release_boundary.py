from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

ACTIVE_TREES = (
    ROOT / "core",
    ROOT / "extensions",
    ROOT / "infra" / "docker",
    ROOT / "scripts" / "release",
    ROOT / "assets" / "frontend" / "src",
    ROOT / "locales",
    ROOT / "tests",
)
ACTIVE_FILES = (
    ROOT / ".env.example",
    ROOT / ".env.example.memory",
    ROOT / ".env.example.ops",
    ROOT / ".env.example.prod",
    ROOT / "README.md",
    ROOT / "docs" / "GUIDE.md",
    ROOT / "requirements-runtime.txt",
    ROOT / "requirements-runtime.lock",
    ROOT / "requirements-cli.lock",
)
TEXT_SUFFIXES = {
    ".css",
    ".html",
    ".js",
    ".json",
    ".md",
    ".po",
    ".pot",
    ".py",
    ".sh",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}
SCAN_EXCLUSIONS = {
    Path("tests/unit/test_postgres_only_release_boundary.py"),
    Path("tests/unit/test_pre_010_cleanup_guardrails.py"),
}
INTENTIONAL_RUNTIME_REQUIREMENTS = {
    "openpyxl",
    "polars",
}


def _active_release_files() -> list[Path]:
    paths = list(ACTIVE_FILES)
    for tree in ACTIVE_TREES:
        paths.extend(
            path
            for path in tree.rglob("*")
            if path.is_file()
            and path.suffix in TEXT_SUFFIXES
            and path.relative_to(ROOT) not in SCAN_EXCLUSIONS
        )
    return sorted(set(paths))


def test_active_first_release_has_no_neo4j_surface() -> None:
    matches: list[str] = []
    for path in _active_release_files():
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(),
            start=1,
        ):
            if "neo4j" in line.lower():
                matches.append(f"{path.relative_to(ROOT)}:{line_number}: {line.strip()}")

    assert matches == [], "Neo4j remains in active first-release surfaces:\n" + "\n".join(matches)


def test_runtime_keeps_intentional_data_dependencies_without_leaking_them_to_cli() -> None:
    inputs = {
        path.name: path.read_text(encoding="utf-8")
        for path in (
            ROOT / "requirements-runtime.txt",
            ROOT / "requirements-runtime.lock",
            ROOT / "requirements-cli.lock",
        )
    }
    combined = "\n".join(inputs.values()).lower()

    assert "neo4j==" not in combined
    assert "pandas==" not in combined
    for requirement in sorted(INTENTIONAL_RUNTIME_REQUIREMENTS):
        assert f"{requirement}==" in inputs["requirements-runtime.lock"].lower()
        assert f"{requirement}==" not in inputs["requirements-cli.lock"].lower()
