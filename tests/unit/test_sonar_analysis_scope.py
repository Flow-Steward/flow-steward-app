from __future__ import annotations

import os.path
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_properties(relative_path: str) -> dict[str, str]:
    properties: dict[str, str] = {}
    for raw_line in (ROOT / relative_path).read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        properties[key.strip()] = value.strip()
    return properties


def _csv_values(properties: dict[str, str], key: str) -> set[str]:
    return {item.strip() for item in properties.get(key, "").split(",") if item.strip()}


PROFILE_GLOB = "infra/tooling/sonar-project*.properties"
# Sonar resolves a scope as: everything under the base keys, narrowed by the inclusion
# filter when one is set, minus the exclusions. Treating the keys as independent lists
# gets the answer wrong in both directions -- `sonar.inclusions=**/*.js` looks like it
# reaches the whole repository until you remember it only filters within the sources.
SCOPES = (
    ("sonar.sources", "sonar.inclusions", "sonar.exclusions"),
    ("sonar.tests", "sonar.test.inclusions", "sonar.test.exclusions"),
)


def _ant_to_regex(pattern: str) -> str:
    """Translate an Ant-style Sonar path pattern, where ** crosses separators and * does not."""
    out: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            out.append("(?:[^/]+/)*")
            index += 3
        elif pattern.startswith("**", index):
            out.append(".*")
            index += 2
        elif pattern[index] == "*":
            out.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(pattern[index]))
            index += 1
    return "".join(out)


def _selects(value: str, repository_path: str) -> bool:
    """True when a single Sonar path value covers this repository path.

    Paths are normalised before they are compared. A literal prefix test answers the
    wrong question for a root written the long way round: `core/..` is the repository
    root, and so is a symlink pointing at it, but neither is a prefix of
    `extensions/...`. Collapsing them first puts both back in the case below.
    """
    raw = os.path.normpath(value.strip()) if value.strip() else "."
    if any(character in raw for character in "*?"):
        return re.fullmatch(_ant_to_regex(raw), repository_path) is not None
    resolved = (ROOT / raw).resolve()
    try:
        relative = resolved.relative_to(ROOT.resolve())
    except ValueError:
        # the value escapes the repository, so it selects nothing inside it
        return False
    base = "" if str(relative) == "." else str(relative)
    if not base:
        # a scope that was never narrowed takes the whole repository with it
        return True
    return repository_path == base or repository_path.startswith(f"{base}/")


def _profiles() -> tuple[str, ...]:
    """Every Sonar profile on disk, because a named list silently stops covering new ones."""
    paths = tuple(sorted(str(path.relative_to(ROOT)) for path in ROOT.glob(PROFILE_GLOB)))
    # a mistyped glob would otherwise make this whole test vacuous
    assert paths, f"no Sonar profiles matched {PROFILE_GLOB}; this test would be vacuous"
    return paths


def _extension_files() -> tuple[str, ...]:
    """Paths an installed extension puts under extensions/, as a checkout would hold them.

    Core ships no extension, so the tree is empty in the repository; the scopes are
    checked against the shapes an install writes there instead.
    """
    return (
        "extensions/acme_crm/extension.yaml",
        "extensions/acme_crm/main.py",
        "extensions/acme_crm/runtime/client.py",
        "extensions/acme_crm/tests/test_main.py",
        "extensions/acme_crm/ui/actions/actions.yaml",
        "extensions/acme_crm/wheels/acme-1.0.0-py3-none-any.whl",
    )


def _reaches(properties: dict[str, str], path: str) -> bool:
    for base_key, inclusion_key, exclusion_key in SCOPES:
        bases = _csv_values(properties, base_key)
        if not any(_selects(base, path) for base in bases):
            continue
        inclusions = _csv_values(properties, inclusion_key)
        if inclusions and not any(_selects(rule, path) for rule in inclusions):
            continue
        if any(_selects(rule, path) for rule in _csv_values(properties, exclusion_key)):
            continue
        return True
    return False


def test_no_sonar_profile_reaches_extension_code() -> None:
    """Extensions left the core scan; they have their own check and their own repository.

    This replaces two tests that pinned the previous arrangement, where extensions were
    analysed and only their tests and fixtures were held out of production sources by
    exclusion. Keeping them would have guarded a scope that no longer exists -- and both
    passed with `sonar.sources=core,extensions`, so neither watched the regression they
    appeared to be there for.

    What gets asserted is reachability, not wording. Searching scope values for the word
    "extensions" answers a different question, and errs in the dangerous direction: it
    misses `sonar.sources=.`, the likelier future regression, while failing on a legal
    core path, since core/application/extensions/ is thirty-odd real files. So every
    profile's scope is resolved the way Sonar resolves it -- bases, narrowed by
    inclusions, minus exclusions -- against the paths an install writes under extensions/.

    Profiles come from a glob rather than a list, so the set under test cannot drift
    away from the set on disk the next time somebody adds one.
    """
    extension_files = _extension_files()
    reaching = {}
    for profile in _profiles():
        properties = _load_properties(profile)
        hits = [path for path in extension_files if _reaches(properties, path)]
        if hits:
            reaching[profile] = hits[:3]
    assert reaching == {}, f"these Sonar profiles still analyse extension code: {reaching}"
