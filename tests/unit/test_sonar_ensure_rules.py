from __future__ import annotations

from typing import Any

import pytest

from core.application.tooling.quality import sonar_ensure_rules


class FakeSonarClient:
    def __init__(self) -> None:
        self.profiles: dict[str, list[dict[str, Any]]] = {}
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.activate_error: sonar_ensure_rules.SonarApiError | None = None

    def get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        assert path == "/api/qualityprofiles/search"
        return {"profiles": self.profiles.get(str(params["language"]), [])}

    def post(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        self.posts.append((path, dict(params)))
        if path == "/api/qualityprofiles/copy":
            source = self._find_profile(str(params["fromKey"]))
            language = str(source["language"])
            profile = {
                "key": f"flow-steward-{language}",
                "name": str(params["toName"]),
                "language": language,
                "isDefault": False,
            }
            self.profiles.setdefault(language, []).append(profile)
            return {"profile": profile}
        if path == "/api/qualityprofiles/set_default":
            language = str(params["language"])
            profile_name = str(params["qualityProfile"])
            for profile in self.profiles.get(language, []):
                profile["isDefault"] = profile["name"] == profile_name
            return {}
        if path == "/api/qualityprofiles/activate_rule":
            if self.activate_error is not None:
                raise self.activate_error
            return {}
        raise AssertionError(f"Unexpected POST path: {path}")

    def _find_profile(self, key: str) -> dict[str, Any]:
        for profiles in self.profiles.values():
            for profile in profiles:
                if profile["key"] == key:
                    return profile
        raise AssertionError(f"Profile not found: {key}")


def test_parse_rule_list_trims_empty_entries() -> None:
    assert sonar_ensure_rules.parse_rule_list("python:S104, ,typescript:S104") == [
        "python:S104",
        "typescript:S104",
    ]


def test_ensure_rules_copies_builtin_default_profile_and_activates_rule() -> None:
    client = FakeSonarClient()
    client.profiles["py"] = [
        {
            "key": "sonar-way-py",
            "name": "Sonar way",
            "language": "py",
            "isDefault": True,
            "isBuiltIn": True,
        }
    ]

    results = sonar_ensure_rules.ensure_rules(client, ["python:S104"])

    assert results == [
        sonar_ensure_rules.EnsureResult(
            language="py",
            profile_name="Flow Steward Python",
            profile_key="flow-steward-py",
            rule="python:S104",
            profile_action="copied:Sonar way",
            rule_action="activated",
        )
    ]
    assert (
        "/api/qualityprofiles/copy",
        {"fromKey": "sonar-way-py", "toName": "Flow Steward Python"},
    ) in client.posts
    assert (
        "/api/qualityprofiles/set_default",
        {"language": "py", "qualityProfile": "Flow Steward Python"},
    ) in client.posts
    assert (
        "/api/qualityprofiles/activate_rule",
        {"key": "flow-steward-py", "rule": "python:S104"},
    ) in client.posts


def test_ensure_rules_reuses_managed_profile_and_treats_already_active_as_ok() -> None:
    client = FakeSonarClient()
    client.profiles["ts"] = [
        {
            "key": "flow-steward-ts",
            "name": "Flow Steward TypeScript",
            "language": "ts",
            "isDefault": True,
        }
    ]
    client.activate_error = sonar_ensure_rules.SonarApiError(
        400,
        "The rule is already activated on this quality profile.",
    )

    results = sonar_ensure_rules.ensure_rules(client, ["typescript:S104"])

    assert results[0].profile_action == "reused-default"
    assert results[0].rule_action == "already-active"


def test_ensure_rules_rejects_unsupported_rule_repository() -> None:
    _raises_input_120_1 = FakeSonarClient()
    with pytest.raises(SystemExit, match="Unsupported rule repository"):
        sonar_ensure_rules.ensure_rules(_raises_input_120_1, ["java:S104"])
