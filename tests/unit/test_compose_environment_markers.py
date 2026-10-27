"""AC17-R129 follow-up: an app process that cannot inherit its environment must name it.

AC17-R129 made silence mean production: an image started with none of
FS_STACK_ENVIRONMENT, ENVIRONMENT or APP_ENV is read as a deployment, which is the
safe reading for a shipped image. It rested on "every Compose file sets one of the
markers", which nothing checked, and it was not true of the OAuth acceptance lane.
That lane's app died at import with "SECURITY_PII_SALT must be set ... in production",
and the Tier 3 check that exercises it failed before its browser runner started.

The markers mostly do not live in Compose at all -- they arrive from an env file, or
from the repository .env the app loads itself. So the rule is about the one mechanism
that removes every source at once, not about a list of services: an environment
declared with `!override` drops the base file's, and a service that also has no
env_file and turns repository dotenv loading off has nowhere left to learn it from.
Such a service has to say what it is.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_DIR = ROOT / "infra" / "docker"
MARKERS = ("FS_STACK_ENVIRONMENT", "ENVIRONMENT", "APP_ENV")


class _Overridden(dict):
    """An environment mapping that was declared with `!override`."""


def _loader() -> type[yaml.SafeLoader]:
    class Loader(yaml.SafeLoader):
        pass

    def overridden(loader: yaml.SafeLoader, node: yaml.Node) -> object:
        if isinstance(node, yaml.MappingNode):
            return _Overridden(loader.construct_mapping(node, deep=True))
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node, deep=True)
        return loader.construct_scalar(node)

    def reset(loader: yaml.SafeLoader, node: yaml.Node) -> object:
        if isinstance(node, yaml.MappingNode):
            return loader.construct_mapping(node, deep=True)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node, deep=True)
        return loader.construct_scalar(node)

    Loader.add_constructor("!override", overridden)
    Loader.add_constructor("!reset", reset)
    return Loader


def _services(path: Path) -> dict:
    document = yaml.load(path.read_text(encoding="utf-8"), Loader=_loader())  # noqa: S506
    return dict((document or {}).get("services") or {})


def _isolated_app_services(services: dict) -> dict[str, dict]:
    """Services that replaced their environment and have no other source for it."""

    isolated = {}
    for name, body in services.items():
        environment = (body or {}).get("environment")
        if not isinstance(environment, _Overridden):
            continue
        if (body or {}).get("env_file"):
            continue
        # Only our own app reads this switch; a database or broker container that
        # overrides its environment is not asked what deployment it is.
        if str(environment.get("FS_LOAD_REPO_DOTENV", "")).strip() != "0":
            continue
        isolated[name] = environment
    return isolated


def _compose_files() -> list[Path]:
    return sorted(COMPOSE_DIR.glob("docker-compose*.yaml"))


def test_every_isolated_app_process_names_its_environment() -> None:
    unmarked = sorted(
        f"{path.name}:{name}"
        for path in _compose_files()
        for name, environment in _isolated_app_services(_services(path)).items()
        if not any(str(environment.get(marker) or "").strip() for marker in MARKERS)
    )

    assert unmarked == [], (
        "these app services override their environment, have no env_file and do not "
        "load the repository .env, so nothing tells them what deployment they are and "
        f"they will be read as production: {unmarked}"
    )


def test_the_rule_reaches_the_lane_that_broke() -> None:
    """Without this the rule above passes on a tree where it finds nothing to check."""

    isolated = _isolated_app_services(_services(COMPOSE_DIR / "docker-compose.oauth-browser.yaml"))

    assert "fs-app" in isolated, "the OAuth lane's app is out of scope; this guard is vacuous"
    # The database container overrides its environment too, and must not be swept in.
    assert "pg-fs" not in isolated


@pytest.mark.parametrize(
    ("body", "expected_in_scope"),
    [
        ({"environment": _Overridden({"FS_LOAD_REPO_DOTENV": "0"})}, True),
        ({"environment": _Overridden({"FS_LOAD_REPO_DOTENV": "0"}), "env_file": ".env"}, False),
        ({"environment": _Overridden({"FS_LOAD_REPO_DOTENV": "1"})}, False),
        ({"environment": {"FS_LOAD_REPO_DOTENV": "0"}}, False),
        ({"environment": _Overridden({"POSTGRES_DB": "x"})}, False),
    ],
)
def test_scope_is_exactly_the_process_with_no_source_left(
    body: dict, expected_in_scope: bool
) -> None:
    """Each way of still having a source keeps a service out; only the last one left is in."""

    assert ("svc" in _isolated_app_services({"svc": body})) is expected_in_scope


def test_the_development_quickstart_names_its_environment() -> None:
    """The other place silence reached, and one the rule above cannot see.

    `.env.example` opens with "cp .env.example .env" and the README calls it "only
    what is needed for first boot". Its services have no `!override` and load the
    repository .env, so they are out of scope above by construction: they simply
    trust the file to carry a marker. It carried none, so a clone started from it was
    read as production and its app refused to import for want of a salt. The
    installer writes the marker itself; the quickstart has to carry it.
    """

    values = {}
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            name, _, value = stripped.partition("=")
            values[name.strip()] = value.strip()
    declared = {marker: values[marker] for marker in MARKERS if values.get(marker)}

    assert declared, ".env.example names no environment, so its first boot is read as production"
    assert all(value.lower() not in {"production", "prod"} for value in declared.values())
