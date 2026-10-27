from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.application.tooling.quality.check_registry import (
    CheckRegistry,
    load_check_registry,
)

ROOT = Path(__file__).resolve().parents[4]
REGISTRY_PATH = ROOT / "infra/tooling/quality/check-registry.json"
LINT_SCRIPT = ROOT / "core/entrypoints/cli/scripts/lint.sh"


def test_canonical_registry_assigns_each_check_to_one_tier() -> None:
    registry = load_check_registry(REGISTRY_PATH)

    assert len(registry.checks) == len({check.id for check in registry.checks})
    assert {check.owner_tier for check in registry.checks} == {1, 2, 3}
    registry.validate_contract()


def test_backend_test_command_carries_its_tier_three_owner_context() -> None:
    registry = load_check_registry(REGISTRY_PATH)
    backend = next(check for check in registry.checks if check.id == "backend-tests")

    assert backend.owner_tier == 3
    assert backend.command[-2:] == ("--quality-owner-tier", "3")


def test_targeted_tests_receive_only_an_explicit_browser_candidate() -> None:
    """A browser acceptance through targeted-tests sees the candidate the caller named.

    lint.sh forwards a declared runtime input only when it is set, so an unset
    APP_BASE_URL stays unset in the container and the acceptance fails its own
    prerequisite instead of reaching a default installation.
    """
    registry = load_check_registry(REGISTRY_PATH)
    targeted = next(check for check in registry.checks if check.id == "targeted-tests")

    assert targeted.runtime_inputs == ("APP_BASE_URL", "FS_SMOKE_CANDIDATE_COMMIT")
    assert '[[ -n "${runtime_input}" && -n "${!runtime_input:-}" ]] || continue' in (
        LINT_SCRIPT.read_text(encoding="utf-8")
    )


def test_runtime_heartbeat_acceptance_is_selected_only_by_tier_three():
    registry = load_check_registry(REGISTRY_PATH)
    assert "runtime-heartbeat-cross-profile" in registry.ids_for_tier(3)
    assert "runtime-heartbeat-cross-profile" not in registry.ids_for_tier(2)
    check = next(row for row in registry.checks if row.id == "runtime-heartbeat-cross-profile")
    assert check.command[-2:] == ("--checks", "runtime-heartbeat-cross-profile")


def test_frontend_targeted_tests_have_one_docker_selection_owner():
    registry = load_check_registry(REGISTRY_PATH)
    selected = next(check for check in registry.checks if check.id == "frontend-targeted-tests")
    assert selected.owner_tier == 1
    assert selected.selection_required is True
    assert selected.command == (
        "bash",
        "core/entrypoints/cli/scripts/lint.sh",
        "--checks",
        "frontend-targeted-tests",
    )
    script = LINT_SCRIPT.read_text()
    assert "frontend-targeted-tests)" in script
    body = script.split("run_check_frontend_targeted_tests() {", 1)[1].split("\n}\n", 1)[0]
    assert "--bail=1" in body
    assert "--coverage.enabled=false" in body
    assert "--no-install" in body
    assert "TEST_PATHS" in body


def test_extension_checks_are_selected_only_and_check_the_named_checkout():
    """An external extension checkout is an input no tier run has, so no tier runs it.

    Owned by Tier 1 (focused, on demand), never selected by `--fast` or `--full`, and
    dispatched by lint.sh, which streams the checkout into fs-audit rather than
    letting a core-only green gate stand in for extension evidence.
    """
    registry = load_check_registry(REGISTRY_PATH)
    script = LINT_SCRIPT.read_text()
    for check_id in ("extension-static", "extension-tests"):
        check = next(item for item in registry.checks if item.id == check_id)
        assert check.owner_tier == 1
        assert check.selection_required is True
        assert check.execution == "orchestrator"
        assert check.command == (
            "bash",
            "core/entrypoints/cli/scripts/lint.sh",
            "--checks",
            check_id,
        )
        assert check_id not in registry.ids_for_tier(2) + registry.ids_for_tier(3)
        assert f"{check_id})" in script
    tests_body = script.split("run_check_extension_tests() {", 1)[1].split("\n}\n", 1)[0]
    assert "FS_EXTENSION_TEST_RUNTIME=1" in tests_body
    assert "extension_gate tests" in tests_body
    assert "--test-path" in tests_body


def test_frontend_build_can_run_before_review_without_tier_three_tests():
    check = next(
        item for item in load_check_registry(REGISTRY_PATH).checks if item.id == "frontend-build"
    )
    assert check.owner_tier == 2
    assert check.command[-2:] == ("--checks", "frontend-build")
    body = LINT_SCRIPT.read_text().split("run_check_frontend_build() {", 1)[1].split("\n}\n", 1)[0]
    assert "npm run build" in body
    assert "npm run test" not in body


def test_secret_scan_fingerprint_covers_every_scanned_repository_area(tmp_path):
    import subprocess

    from core.application.tooling.quality.fingerprint import FingerprintContext, fingerprint_check

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for name in ("tests", "docs"):
        (tmp_path / name).mkdir()
    paths = [tmp_path / "tests/example.py", tmp_path / "docs/setup.md", tmp_path / ".env.example"]
    for path in paths:
        path.write_text("before")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    check = next(
        item for item in load_check_registry(REGISTRY_PATH).checks if item.id == "detect-secrets"
    )
    context = FingerprintContext(root=tmp_path, source="worktree")
    previous = fingerprint_check(check, context)
    for path in paths:
        path.write_text("after")
        current = fingerprint_check(check, context)
        assert current != previous, path
        previous = current


def test_dependency_audit_evidence_invalidates_when_lock_or_acceptance_changes(tmp_path):
    import subprocess

    from core.application.tooling.quality.fingerprint import FingerprintContext, fingerprint_check

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    paths = [
        tmp_path / "requirements-cli.lock",
        tmp_path / "infra/tooling/quality/pip-audit-risk-acceptance.md",
        tmp_path / "core/application/tooling/quality/pip_audit_acceptance.py",
    ]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("before")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    check = next(
        item for item in load_check_registry(REGISTRY_PATH).checks if item.id == "pip-audit"
    )
    context = FingerprintContext(root=tmp_path, source="worktree")
    previous = fingerprint_check(check, context)
    for path in paths:
        path.write_text("after")
        current = fingerprint_check(check, context)
        assert current != previous, path
        previous = current


def test_deterministic_formatting_checks_are_owned_by_tier_two() -> None:
    registry = load_check_registry(REGISTRY_PATH)
    formatting = next(check for check in registry.checks if check.id == "frontend-sql")

    assert formatting.owner_tier == 2
    assert formatting.trusted_ci_required is False


def test_tier_two_fails_fast_on_dependency_and_security_checks_first() -> None:
    registry = load_check_registry(REGISTRY_PATH)
    tier_two = tuple(check.id for check in registry.checks if check.owner_tier == 2)

    assert tier_two[:4] == (
        "pip-audit",
        "frontend-audit",
        "detect-secrets",
        "semgrep",
    )


def test_tier_three_keeps_only_long_suites_and_required_acceptance_checks() -> None:
    registry = load_check_registry(REGISTRY_PATH)
    tier_three_order = tuple(check.id for check in registry.checks if check.owner_tier == 3)

    assert set(tier_three_order) == {
        "frontend-tests",
        "backend-tests",
        "extension-oauth-browser",
        "clone-volume-disposable",
        "runtime-heartbeat-cross-profile",
    }
    oauth_index = tier_three_order.index("extension-oauth-browser")
    assert tier_three_order.index("frontend-tests") < oauth_index
    assert tier_three_order.index("backend-tests") < oauth_index


def test_candidate_runtime_checks_are_externally_owned() -> None:
    registry = load_check_registry(REGISTRY_PATH)
    external = {check.id for check in registry.checks if check.execution == "external"}

    assert external == {
        "extension-oauth-browser",
        "compact-p1-routing",
        "compact-p3-extension-invocation",
        "clone-volume-disposable",
    }


def test_clone_volume_disposable_is_selected_by_external_tier_three() -> None:
    registry = load_check_registry(REGISTRY_PATH)
    selected = {
        check.id
        for check in registry.checks
        if check.owner_tier == 3 and check.execution == "external"
    }

    assert "clone-volume-disposable" in selected


def test_duplicate_check_ids_are_rejected() -> None:
    raw = {
        "schema_version": 1,
        "checks": [
            {
                "id": "ruff",
                "owner_tier": 2,
                "command": ["bash", "lint.sh", "--checks", "ruff"],
                "inputs": ["core/**/*.py"],
            },
            {
                "id": "ruff",
                "owner_tier": 3,
                "command": ["bash", "lint.sh", "--checks", "ruff"],
                "inputs": ["core/**/*.py"],
            },
        ],
    }

    registry = CheckRegistry.model_validate(raw)
    with pytest.raises(ValueError, match="duplicate check ids: ruff"):
        registry.validate_contract()


def test_repeated_check_requires_a_reason() -> None:
    raw = {
        "schema_version": 1,
        "checks": [
            {
                "id": "candidate-smoke",
                "owner_tier": 3,
                "command": ["bash", "scripts/smoke/release-smoke.sh", "--profile", "release"],
                "inputs": ["scripts/smoke/**"],
                "repeat_count": 2,
            }
        ],
    }

    with pytest.raises(ValidationError, match="repeat_reason"):
        CheckRegistry.model_validate(raw)


def test_registry_json_is_stable_and_does_not_contain_secret_values() -> None:
    raw = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))

    serialized = json.dumps(raw, sort_keys=True).casefold()
    assert "password" not in serialized
    assert "bearer_token" not in serialized
    assert "secret_key" not in serialized


def test_no_check_escapes_the_container_gate() -> None:
    """AC17-R97: every tier must run the same way, and that way is the container.

    Tiers 2-3 invoke `lint.sh`, which starts a disposable fs-audit container and
    runs the check inside it, so the result does not depend on the host OS or its
    interpreter. Tier 1 invoked `python -m pytest` directly, and six checks in the
    other tiers invoked scripts directly. Two concrete failures came from that:

    * Tier 1 could not run on this machine at all -- the default `python3` is 3.14
      and requirements-runtime pins tree-sitter-languages, which caps at <3.12;
    * a host run of tests/unit/test_initial_extensions.py passed while the gate
      failed, because the gate substitutes the tree through run-core-only-tests.sh
      and a host run sees the whole repository.

    This asserts the invariant by execution shape, not by a phrase in a document:
    a check added tomorrow that shells out to a host interpreter fails here.
    """
    registry = load_check_registry(REGISTRY_PATH)
    escaping = []
    for check in registry.checks:
        # No exemption for `execution: external`. That field selects which run a
        # check belongs to -- verify.py filters on it -- not where the check
        # executes; `zap` is external and still goes through the gate. A check that
        # needs a host resource declares it in `runtime_inputs`, which lint.sh
        # forwards into the container, so needing a resource is a prerequisite
        # rather than a reason to escape.
        command = list(check.command)
        joined = " ".join(command)
        gate = "core/entrypoints/cli/scripts/lint.sh"
        # the exact path, so a differently named wrapper cannot satisfy this
        if command[:1] != ["bash"] or gate not in joined.split():
            escaping.append((check.id, joined))
    assert escaping == [], (
        "these checks run outside the container gate, so their result depends on the "
        f"host: {escaping}"
    )


def test_every_registry_check_names_a_check_the_gate_implements() -> None:
    """The other half of AC17-R97: going through lint.sh must actually mean something.

    Requiring the command to invoke lint.sh stops a check running on the host. It
    does not stop a check naming a gate check that does not exist -- that would
    fail only when someone ran it. This reads the dispatch table out of lint.sh and
    requires every `--checks <id>` in the registry to be a case the gate handles.

    Deliberately not a banned-substring rule: "the command must not say pytest" is
    defeated by any rename or wrapper, while "the id must exist in the dispatcher"
    is a fact about the two files agreeing.
    """
    source = LINT_SCRIPT.read_text(encoding="utf-8")
    # the definition, not a later call site: rsplit on the bare name lands after
    # the invocation and yields an empty dispatch table (caught by the guard below)
    dispatch = source.split("run_selected_checks() {", 1)[1]
    known: set[str] = set()
    for line in dispatch.splitlines():
        stripped = line.strip()
        if stripped.endswith(")") and not stripped.startswith(("#", "*", "esac")):
            label = stripped[:-1]
            if label and all(ch.isalnum() or ch in "|-_" for ch in label):
                known.update(part for part in label.split("|") if part)
    assert "ruff" in known, "the dispatch table was not parsed; the guard would pass vacuously"

    registry = load_check_registry(REGISTRY_PATH)
    unknown = []
    for check in registry.checks:
        command = list(check.command)
        if "--checks" not in command:
            continue
        requested = command[command.index("--checks") + 1]
        for name in requested.split(","):
            if name.strip() and name.strip() not in known:
                unknown.append((check.id, name.strip()))
    assert unknown == [], f"registry names checks the gate cannot dispatch: {unknown}"


def test_ui_i18n_has_one_tier_two_container_owner_and_real_checker_dispatch():
    registry = load_check_registry(REGISTRY_PATH)
    check = next(item for item in registry.checks if item.id == "ui-i18n")
    assert check.owner_tier == 2
    assert check.reuse_scope == "inputs"
    assert check.execution == "orchestrator"
    assert check.input_mode == "content"
    assert check.command == ("bash", "core/entrypoints/cli/scripts/lint.sh", "--checks", "ui-i18n")
    script = LINT_SCRIPT.read_text()
    assert "ui-i18n)" in script
    body = script.split("run_check_ui_i18n() {", 1)[1].split("\n}\n", 1)[0]
    assert '"${PYTHON_BIN}" -m core.application.tooling.i18n.ui_i18n_catalog validate-keys' in body
    assert "run_step" in body


def test_ui_i18n_fingerprint_changes_with_sources_locales_metadata_and_checker(tmp_path):
    import subprocess

    from core.application.tooling.quality.fingerprint import FingerprintContext, fingerprint_check

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    names = (
        "core/application/settings/view.py",
        "assets/frontend/src/view.ts",
        "assets/frontend/src/view.tsx",
        "locales/en/LC_MESSAGES/ui.po",
        "locales/lt/LC_MESSAGES/ui.po",
        "locales/lt/meta.json",
        "locales/lt/LC_MESSAGES/ui.mo",
        "locales/ui.pot",
        "core/application/tooling/i18n/ui_i18n_catalog.py",
    )
    paths = [tmp_path / name for name in names]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("before", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    check = next(item for item in load_check_registry(REGISTRY_PATH).checks if item.id == "ui-i18n")
    context = FingerprintContext(root=tmp_path, source="worktree")
    previous = fingerprint_check(check, context)
    for path in paths:
        path.write_text("after", encoding="utf-8")
        current = fingerprint_check(check, context)
        assert current != previous, path
        previous = current
