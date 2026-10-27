from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from core.application.auth.config import AUTH_BOOTSTRAP_TOKEN_HEADER
from core.application.tooling.quality.check_registry import load_check_registry
from core.application.tooling.quality.fingerprint import FingerprintContext, fingerprint_check
from core.infrastructure.extension_runtime.extension_runtime import scan_extensions
from tests.fixtures import preprovision_extension_oauth_browser, seed_extension_oauth_browser

ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = Path(os.environ.get("PYTEST_SOURCE_REPO_ROOT", ROOT)).resolve()
SPEC = ROOT / "assets/frontend/tests/e2e/extension-oauth.spec.ts"
RUNNER = ROOT / "core/entrypoints/cli/scripts/run-extension-oauth-browser-acceptance.sh"
EXTERNAL_TIER_RUNNER = ROOT / "core/entrypoints/cli/scripts/run-external-tier-three.sh"
EXIT_CHECK = ROOT / "core/entrypoints/cli/scripts/check-oauth-browser-exit.sh"
OVERLAY = ROOT / "infra/docker/docker-compose.oauth-browser.yaml"
DOCKERFILE = ROOT / "infra/docker/Dockerfile"
MANUAL_OVERLAY = ROOT / "infra/docker/docker-compose.oauth-browser-manual.yaml"
MANUAL_RUNNER = ROOT / "core/entrypoints/cli/scripts/run-extension-oauth-browser-manual.sh"
REGISTRY_PATH = ROOT / "infra/tooling/quality/check-registry.json"


# Execute the real runner with a closed fake Docker: unknown commands fail rather
# than falling through to a daemon. State distinguishes pre-existing resources
# from images/containers created after the startup preflight.
_FAKE_OAUTH_DOCKER = r"""#!/usr/bin/env python3
import json, os, pathlib, sys
root = pathlib.Path(os.environ["OAUTH_FAKE_ROOT"])
args = sys.argv[1:]
with (root / "commands.jsonl").open("a") as stream:
    stream.write(json.dumps(args) + "\n")
state_path = root / "state.json"
state = json.loads(state_path.read_text())
def done(code=0, output=""):
    state_path.write_text(json.dumps(state))
    if output:
        print(output)
    raise SystemExit(code)
project = os.environ["OAUTH_EXPECTED_PROJECT"]
def inventory(kind, selector):
    if os.environ.get("OAUTH_INVENTORY_FAILURE") == kind:
        print("untrusted daemon diagnostic", file=sys.stderr)
        done(31)
    if selector in state[kind]:
        done(output="existing-object")
    done()
if args[:1] == ["ps"]:
    assert args[1:3] == ["-aq", "--filter"], args
    inventory("container", args[3].removeprefix("label=com.docker.compose.project="))
if args[:2] in (["network", "ls"], ["volume", "ls"]):
    assert args[2:4] == ["-q", "--filter"], args
    inventory(args[0], args[4].removeprefix("label=com.docker.compose.project="))
if args[:2] == ["image", "ls"]:
    assert args[2:4] == ["--quiet", "--filter"], args
    inventory("image", args[4].removeprefix("reference="))
if args[:2] == ["image", "inspect"]:
    done(0 if args[2] in state["image"] else 1)
if args[:2] == ["image", "rm"]:
    for image in args[2:]:
        if os.environ.get("OAUTH_IMAGE_RESIDUE") != "1" and image in state["image"]:
            state["image"].remove(image)
    done()
if args[:1] == ["build"]:
    state["image"].append(args[args.index("--tag") + 1])
    done()
if args[:1] == ["compose"]:
    assert args[1:3] == ["--project-name", project], args
    action = args[7]
    if action == "config":
        done(output=json.dumps({"services": {"oauth-browser": {}}}))
    if action == "down":
        for kind in ("container", "network", "volume"):
            if os.environ.get("OAUTH_RESOURCE_RESIDUE") != "1" and project in state[kind]:
                state[kind].remove(project)
        done()
    if action == "up":
        for kind in ("container", "network", "volume"):
            state[kind].append(project)
        done(int(os.environ.get("OAUTH_UP_STATUS", "0")))
    if action == "wait":
        done(int(os.environ.get("OAUTH_WAIT_STATUS", "0")), os.environ.get("OAUTH_WAIT_OUTPUT", "0"))
    if action in ("logs", "ps"):
        done()
print("Unexpected fake Docker command: " + repr(args), file=sys.stderr)
done(91)
"""


def _run_recorded_oauth_lane(
    tmp_path: Path,
    *,
    existing: str = "",
    inventory_failure: str = "",
    requested: str = "fs-oauth-generic-contract",
    overrides: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[list[str]], dict[str, list[str]]]:
    import shutil
    import sys

    repo = tmp_path / "repo"
    scripts = repo / "core/entrypoints/cli/scripts"
    scripts.mkdir(parents=True)
    for source in (RUNNER, EXIT_CHECK):
        shutil.copy2(source, scripts / source.name)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    # env python3 would enter the snapshot wrapper below, then exec this interpreter again.
    docker_source = _FAKE_OAUTH_DOCKER.replace("#!/usr/bin/env python3", f"#!{sys.executable}", 1)
    docker.write_text(docker_source, encoding="utf-8")
    docker.chmod(0o755)
    # Snapshot generation itself has separate contract tests; control its result
    # here while running the actual runner, Compose isolation check and exit parser.
    python = fake_bin / "python3"
    python.write_text(
        f"#!{sys.executable}\n"
        "import os,pathlib,sys\n"
        "if sys.argv[1:3] == ['-m', 'core.application.tooling.quality.repository_snapshot']:\n"
        " p=pathlib.Path(os.environ['OAUTH_FAKE_ROOT'])/'snapshot-count'\n"
        " count=int(p.read_text()) if p.exists() else 0\n"
        " p.write_text(str(count+1))\n"
        " print(('b' if count and os.environ.get('OAUTH_DIRTY_SOURCE') else 'a')*64)\n"
        "else:\n"
        f" os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    python.chmod(0o755)
    sleep = fake_bin / "sleep"
    sleep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    sleep.chmod(0o755)
    project = requested.removesuffix("-oauth-browser") + "-oauth-browser"
    state: dict[str, list[str]] = {kind: [] for kind in ("container", "network", "volume", "image")}
    if existing in ("container", "network", "volume"):
        state[existing].append(project)
    elif existing in ("app", "audit", "frontend"):
        state["image"].append(f"{project}-{existing}:{'a' * 16}")
    elif existing == "unrelated":
        for kind in ("container", "network", "volume"):
            state[kind].append(project + "-foreign")
        state["image"] = [f"{project}-app:{'b' * 16}", f"{project}-app-extra:{'a' * 16}"]
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FS_COMPOSE_PROJECT_NAME": requested,
        "OAUTH_FAKE_ROOT": str(tmp_path),
        "OAUTH_EXPECTED_PROJECT": project,
        "OAUTH_INVENTORY_FAILURE": inventory_failure,
        **(overrides or {}),
    }
    result = subprocess.run(
        ["bash", str(scripts / RUNNER.name)],
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    commands_path = tmp_path / "commands.jsonl"
    commands = [json.loads(line) for line in commands_path.read_text().splitlines()]
    return result, commands, json.loads((tmp_path / "state.json").read_text())


def _oauth_mutations(commands: list[list[str]]) -> list[list[str]]:
    return [
        args
        for args in commands
        if args[0] == "build"
        or args[:2] == ["image", "rm"]
        or (args[0] == "compose" and args[7] in ("down", "up"))
    ]


@pytest.mark.parametrize("existing", ("container", "network", "volume", "app", "audit", "frontend"))
def test_runner_refuses_preexisting_resources_before_any_mutation(
    tmp_path: Path, existing: str
) -> None:
    result, commands, state = _run_recorded_oauth_lane(tmp_path, existing=existing)

    assert _oauth_mutations(commands) == [], commands
    assert result.returncode != 0
    assert "pre-existing" in result.stderr
    assert "fs-oauth-generic-contract-oauth-browser" in result.stderr
    assert any(state.values())
    assert (tmp_path / "snapshot-count").read_text() == "1"  # No cleanup trap ran.


@pytest.mark.parametrize("kind", ("container", "network", "volume", "image"))
def test_runner_refuses_failed_startup_inventory(tmp_path: Path, kind: str) -> None:
    result, commands, _ = _run_recorded_oauth_lane(tmp_path, inventory_failure=kind)

    assert _oauth_mutations(commands) == [], commands
    assert result.returncode != 0
    assert "inventory" in result.stderr
    assert "untrusted daemon diagnostic" not in result.stderr
    assert (tmp_path / "snapshot-count").read_text() == "1"


@pytest.mark.parametrize(
    "requested", ("fs-oauth-generic-contract", "fs-oauth-generic-contract-oauth-browser")
)
def test_runner_probes_derived_namespace_and_exact_tags_then_cleans_own_lane(
    tmp_path: Path, requested: str
) -> None:
    result, commands, state = _run_recorded_oauth_lane(
        tmp_path, existing="unrelated", requested=requested
    )

    assert result.returncode == 0, result.stderr
    project = "fs-oauth-generic-contract-oauth-browser"
    first_mutation = commands.index(_oauth_mutations(commands)[0])
    assert commands[:first_mutation][:6] == [
        ["ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"],
        ["network", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"],
        ["volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"],
        *[
            ["image", "ls", "--quiet", "--filter", f"reference={project}-{role}:{'a' * 16}"]
            for role in ("app", "audit", "frontend")
        ],
    ]
    assert len([args for args in commands if args[0] == "build"]) == 3
    assert any(args[0] == "compose" and args[7:] == ["wait", "oauth-browser"] for args in commands)
    assert state == {
        "container": [project + "-foreign"],
        "network": [project + "-foreign"],
        "volume": [project + "-foreign"],
        "image": [f"{project}-app:{'b' * 16}", f"{project}-app-extra:{'a' * 16}"],
    }


@pytest.mark.parametrize(
    "overrides,diagnostic",
    (
        ({"OAUTH_WAIT_STATUS": "1"}, "OAuth browser service failed"),
        ({"OAUTH_WAIT_OUTPUT": "7"}, "OAuth browser service failed"),
        ({"OAUTH_WAIT_OUTPUT": "malformed"}, "OAuth browser service failed"),
        ({"OAUTH_DIRTY_SOURCE": "1"}, "changed the repository worktree"),
        ({"OAUTH_UP_STATUS": "1"}, "dependencies failed"),
        ({"OAUTH_RESOURCE_RESIDUE": "1"}, ""),
        ({"OAUTH_IMAGE_RESIDUE": "1"}, "Candidate image remains"),
    ),
)
def test_runner_preserves_failure_and_cleanup_contracts(
    tmp_path: Path, overrides: dict[str, str], diagnostic: str
) -> None:
    result, commands, state = _run_recorded_oauth_lane(tmp_path, overrides=overrides)

    assert result.returncode != 0
    assert diagnostic in result.stderr
    assert any(args[0] == "compose" and args[7] == "down" for args in commands)
    assert any(args[:2] == ["image", "rm"] for args in commands)
    if "OAUTH_RESOURCE_RESIDUE" not in overrides:
        assert state["container"] == state["network"] == state["volume"] == []
    if "OAUTH_IMAGE_RESIDUE" not in overrides:
        assert state["image"] == []


def test_consumer_seed_discovery_accepts_an_empty_core_extensions_root(tmp_path: Path) -> None:
    assert seed_extension_oauth_browser._consumer_extensions(tmp_path) == []


def test_consumer_seed_discovery_preserves_explicit_valid_consumers(tmp_path: Path) -> None:
    policy = tmp_path / "consumer" / "tests" / "oauth-browser-seed.json"
    policy.parent.mkdir(parents=True)
    policy.write_text('{"schema_version": 1, "extension_id": "acme.consumer"}', encoding="utf-8")

    assert seed_extension_oauth_browser._consumer_extensions(tmp_path) == [
        ("acme.consumer", tmp_path / "consumer")
    ]


@pytest.mark.parametrize(
    ("policy_text", "error_type"),
    [
        ("not-json", json.JSONDecodeError),
        ('{"schema_version": 2, "extension_id": "acme.consumer"}', RuntimeError),
        ('{"schema_version": 1, "extension_id": ""}', RuntimeError),
    ],
)
def test_consumer_seed_discovery_rejects_invalid_declarations(
    tmp_path: Path, policy_text: str, error_type: type[Exception]
) -> None:
    policy = tmp_path / "consumer" / "tests" / "oauth-browser-seed.json"
    policy.parent.mkdir(parents=True)
    policy.write_text(policy_text, encoding="utf-8")

    with pytest.raises(error_type):
        seed_extension_oauth_browser._consumer_extensions(tmp_path)


def test_primary_browser_spec_uses_real_product_apis_without_route_interception() -> None:
    source = SPEC.read_text(encoding="utf-8")

    assert "page.route(" not in source
    assert "route.fulfill(" not in source
    assert "/api/auth/login" in source
    assert "/authorization" in source
    assert ".authorization_url" in source
    assert "invalid_client@example.test" in source
    assert "invalid_grant@example.test" in source
    assert 'name: "Confirm configuration impact"' in source
    assert 'impactDialog.locator("li")' in source
    assert 'name: "Confirm and save"' in source
    validation_block = source.split('test("service rejects a browser-bypass mutation', 1)[1].split(
        "await login(page, viewer)", 1
    )[0]
    assert validation_block.count("status()).toBe(400)") == 3
    assert "status()).toBe(422)" not in validation_block
    assert validation_block.count('error: "invalid_credential_field"') == 3


def test_full_gate_and_registry_own_the_real_browser_lane() -> None:
    lint_source = (ROOT / "core/entrypoints/cli/scripts/lint.sh").read_text(encoding="utf-8")
    host_gate = lint_source.split(
        'if [[ "${INSIDE}" -eq 0 && "${_in_container}" -eq 0 ]]; then', 1
    )[1]
    registry = json.loads(
        (ROOT / "infra/tooling/quality/check-registry.json").read_text(encoding="utf-8")
    )
    checks = {row["id"]: row for row in registry["checks"]}

    assert "run-external-tier-three.sh" in host_gate
    assert '"${MODE}" == "full"' in host_gate
    assert checks["extension-oauth-browser"]["owner_tier"] == 3
    assert checks["extension-oauth-browser"]["execution"] == "external"
    # AC17-R97: the canonical command goes through the gate, like every other check.
    # `execution: external` above still holds and still means what it meant -- which
    # run this check belongs to, not where it executes. lint.sh dispatches
    # `extension-oauth-browser` to the same runner script, and forwards whatever the
    # check declares in runtime_inputs.
    assert checks["extension-oauth-browser"]["command"] == [
        "bash",
        "core/entrypoints/cli/scripts/lint.sh",
        "--checks",
        "extension-oauth-browser",
    ]
    assert RUNNER.is_file()


def test_external_tier_three_dispatch_uses_reproducible_audit_runtime_without_bashpid() -> None:
    env = {key: value for key, value in os.environ.items() if key != "BASHPID"}
    env["FS_EXTERNAL_TIER_GIT_COMMON_DIR"] = "/tmp/git-common"
    env["FS_TEST_AUDIT_IMAGE_ID"] = "sha256:" + "a" * 64
    result = subprocess.run(
        [
            "bash",
            "-c",
            """
unset BASHPID
[[ -z "${BASHPID+x}" ]]
source "$1" --dry-run \
  --evidence-mode off \
  --fingerprint-source worktree \
  --trust local
""",
            "external-tier-three-test",
            str(EXTERNAL_TIER_RUNNER),
        ],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "fs_compose run --no-deps --rm -T --user 0:0" in result.stdout
    assert "-e FS_TEST_AUDIT_IMAGE_ID=sha256:" + "a" * 64 in result.stdout
    assert "/var/run/docker.sock:/var/run/docker.sock" in result.stdout
    assert "fs-audit python -m core.application.tooling.quality.verify" in result.stdout
    assert "--tier 3 --execution external --repository-root /app" in result.stdout
    assert re.search(r"FS_COMPOSE_PROJECT_NAME=fs-oauth-generic-full-[0-9]+", result.stdout)
    assert (
        "FS_STACK_ENV_FILE=/tmp/flow-steward-external-tier-three-no-stack-env" in result.stdout
    )  # pragma: allowlist secret - an environment assignment, not a credential


def test_browser_fingerprint_covers_ui_consumer_schema_toolchain_and_helpers(
    tmp_path: Path,
) -> None:
    registry = load_check_registry(REGISTRY_PATH)
    check = next(row for row in registry.checks if row.id == "extension-oauth-browser")
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    representatives = (
        "assets/frontend/src/pages/extensions/view.tsx",
        "extensions/example_provider/tests/e2e/extension-oauth.spec.ts",
        "db/schema/001.sql",
        "infra/docker/Dockerfile",
        "requirements-audit.txt",
        "core/entrypoints/cli/scripts/check-oauth-browser-exit.sh",
    )
    for relative in representatives:
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("baseline\n", encoding="utf-8")
    context = FingerprintContext(root=repo, source="worktree")
    baseline = fingerprint_check(check, context)

    for relative in representatives:
        path = repo / relative
        path.write_text(f"changed:{relative}\n", encoding="utf-8")
        assert fingerprint_check(check, context) != baseline
        path.write_text("baseline\n", encoding="utf-8")


def test_runner_builds_exact_local_candidates_and_verifies_cleanup() -> None:
    source = RUNNER.read_text(encoding="utf-8")

    assert source.count("docker build --pull=false") == 3
    assert "--network=none" not in source
    assert source.count('--file "${ROOT_DIR}/infra/docker/Dockerfile" "${ROOT_DIR}"') == 3
    assert "up --detach --no-build --pull never oauth-browser" in source
    assert "wait oauth-browser" in source
    assert "browser_exit_code" in source
    assert "check-oauth-browser-exit.sh" in source
    assert "logs --no-color oauth-browser" in source
    # Startup diagnostics are captured before the cleanup trap runs: without
    # them the only evidence of a failure is "container X is unhealthy", and the
    # stack that produced it has already been removed.
    assert (
        "for diagnostic_service in oauth-db-schema-init oauth-db-upgrade pg-fs "
        "oauth-browser-seed fs-app traefik" in source
    )
    assert "logs --no-color --tail 200" in source
    assert "docker inspect --format" in source
    assert ".State.Health.Log" in source
    assert "--abort-on-container-exit" not in source
    assert "status --porcelain=v1 --untracked-files=all" not in source
    assert "core.application.tooling.quality.repository_snapshot" in source
    assert 'candidate_fingerprint="${initial_snapshot:0:16}"' in source
    assert "repository_snapshot)" in source
    assert "down --volumes --remove-orphans" in source
    assert "com.docker.compose.project=${PROJECT_NAME}" in source
    assert "remove_candidate_images || cleanup_status=$?" in source
    assert "docker image inspect" in source
    assert source.rsplit("trap - EXIT INT TERM", 1)[0].rstrip().endswith("cleanup_on_exit")
    assert '"oauth-browser"' in source
    # The isolation check walks the rendered dependency graph instead of a
    # hand-written service list. The list could not notice a service the base
    # graph added later, which is how an inherited host bind reached an
    # isolated stack unseen.
    assert 'ROOT_SERVICE = "oauth-browser"' in source
    assert 'services[name].get("depends_on")' in source
    assert "closure" in source
    assert "started_services = {" not in source


def test_browser_exit_parser_fails_closed_for_wait_or_container_failure() -> None:
    compose_v5_success = 'container "a1b2c3d4e5f6" exited with status code 0'
    compose_v5_failure = 'container "a1b2c3d4e5f6" exited with status code 7'

    assert (
        subprocess.run(["bash", str(RUNNER), "--verify-exit-helper"], check=False).returncode == 0
    )
    assert subprocess.run(["bash", str(EXIT_CHECK), "0", "0"], check=False).returncode == 0
    assert (
        subprocess.run(["bash", str(EXIT_CHECK), "0", compose_v5_success], check=False).returncode
        == 0
    )
    assert subprocess.run(["bash", str(EXIT_CHECK), "1", "0"], check=False).returncode != 0
    assert subprocess.run(["bash", str(EXIT_CHECK), "0", "7"], check=False).returncode != 0
    assert (
        subprocess.run(["bash", str(EXIT_CHECK), "0", compose_v5_failure], check=False).returncode
        != 0
    )
    for malformed in (
        "",
        "-1",
        "not-a-number",
        'container"a1b2c3d4e5f6"exitedwithstatuscode0',
        'prefix container "a1b2c3d4e5f6" exited with status code 0',
        'container "a1b2c3d4e5f6" exited with status code 0\nextra',
    ):
        assert (
            subprocess.run(["bash", str(EXIT_CHECK), "0", malformed], check=False).returncode != 0
        )


@pytest.mark.parametrize(
    "wait_output",
    (
        "0",
        'container "a1b2c3d4e5f6" exited with status code 0',
        'container "a1b2c3d4e5f6" exited with status code 7',
    ),
)
def test_runner_preserves_the_final_compose_wait_line(wait_output: str) -> None:
    result = subprocess.run(
        ["bash", str(RUNNER), "--verify-exit-extraction", wait_output],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout == wait_output


def test_rendered_compose_has_no_host_bind_or_published_port() -> None:
    env = {
        **os.environ,
        "FS_OAUTH_ACCEPTANCE_APP_IMAGE": "local/acceptance-app:test",
        "FS_OAUTH_ACCEPTANCE_AUDIT_IMAGE": "local/acceptance-audit:test",
        "FS_OAUTH_ACCEPTANCE_FRONTEND_IMAGE": "local/acceptance-frontend:test",
        "POSTGRES_DB": "hostile_inherited_db",
        "POSTGRES_USER": "hostile_inherited_user",
        "POSTGRES_PASSWORD": "hostile-inherited-password",
        # Prove acceptance services ignore host identities that do not own their volumes.
        "FS_HOST_UID": "501",
        "FS_HOST_GID": "20",
        "APP_PORT": "18443",
        "HTTP_PORT": "18080",
    }
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--project-name",
            "fs-oauth-generic-contract-oauth-browser",
            "--file",
            str(ROOT / "infra/docker/docker-compose.yaml"),
            "--file",
            str(OVERLAY),
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    rendered = json.loads(result.stdout)
    seed_command = rendered["services"]["oauth-browser-seed"]["command"]
    seed_base_url = urlsplit(seed_command[seed_command.index("--base-url") + 1])
    traefik_command = rendered["services"]["traefik"]["command"]
    websecure_listener = next(
        argument
        for argument in traefik_command
        if argument.startswith("--entrypoints.websecure.address=")
    )
    websecure_port = int(websecure_listener.rsplit(":", 1)[1])
    assert websecure_port == seed_base_url.port

    started = {
        "extension-dependencies",
        "frontend-build",
        "fs-app",
        "fs-mcp",
        "oauth-browser",
        "oauth-browser-seed",
        "oauth-cert-init",
        "oauth-db-schema-init",
        "oauth-db-upgrade",
        "oauth-provider",
        "oauth-runtime-permission-probe",
        "oauth-app-volumes-init",
        "pg-fs",
        "rabbitmq-fs",
        "traefik",
    }

    for name in started:
        service = rendered["services"][name]
        assert service.get("ports", []) == []
        assert all(volume.get("type") != "bind" for volume in service.get("volumes", []))

    assert set(rendered["services"]["fs-app"]["environment"]) == {
        "ARTIFACT_LOCAL_ROOT_DIR",
        "ARTIFACT_STORAGE_BACKEND",
        "CONTROL_LOOPS_ENABLED",
        "ENCRYPTION_KEY",
        "FS_ALLOW_PRIVATE_REMOTE_URLS",
        "FS_AUTH_BOOTSTRAP_ALLOW_TRUSTED_PROXY",
        "FS_AUTH_BOOTSTRAP_ENABLED",
        "FS_AUTH_BOOTSTRAP_LOCALHOST_ONLY",
        "FS_AUTH_BOOTSTRAP_TOKEN",
        "FS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT",
        "FS_AUTH_COOKIE_SECURE",
        "FS_AUTH_TRUSTED_ORIGINS",
        "FS_DB_DSN",
        "FS_EXTENSION_DEPS_FETCH",
        "FS_EXTENSIONS_ROOT",
        "FS_GZIP_ENABLED",
        "FS_LOAD_REPO_DOTENV",
        "FS_LOCAL_AUTH_ENABLED",
        "FS_LOG_FORMAT",
        "FS_LOG_LEVEL",
        "FS_PUBLIC_BASE_URL",
        "FS_RABBITMQ_TRANSPORT_MODE",
        "FS_SECRET_KEY",
        "FS_SERVE_SSL",
        "FS_STACK_ENVIRONMENT",
        "HOME",
        "LANGGRAPH_POSTGRES_URI",
        "MCP_URL",
        "PORT",
        "PYTHONPYCACHEPREFIX",
        "RABBITMQ_URL",
        "SSL_CERT_FILE",
        "WORKERS",
    }
    assert (
        rendered["services"]["fs-app"]["environment"]["FS_RABBITMQ_TRANSPORT_MODE"]
        == "compose-local"
    )
    assert rendered["services"]["fs-app"]["environment"]["FS_AUTH_BOOTSTRAP_TOKEN"]
    # AC17-R129: silence reads as production, and nothing else in this lane can say
    # otherwise -- the environment is overridden, and repository dotenv loading is off.
    assert rendered["services"]["fs-app"]["environment"]["FS_STACK_ENVIRONMENT"] == "development"
    assert (
        rendered["services"]["oauth-browser"]["environment"]["FS_OAUTH_EXPECTED_PUBLIC_BASE_URL"]
        == "https://traefik:8099"
    )
    assert (
        rendered["services"]["fs-app"]["environment"]["FS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT"]
        == "2099-01-01T00:00:00+00:00"
    )
    writable = rendered["services"]["oauth-app-volumes-init"]
    assert writable["user"] == "0:0"
    writable_command = " ".join(writable["command"])
    assert "find /w/extensions -mindepth 1 -maxdepth 1 -exec rm -r" in writable_command
    assert "cp -R /app/extensions/. /w/extensions/" in writable_command
    assert "tests.fixtures.preprovision_extension_oauth_browser" in writable_command
    assert "--extensions-root /w/extensions" in writable_command
    assert "--provider-base-url https://oauth-provider:4187" in writable_command
    assert writable_command.index("cp -R /app/extensions/.") < writable_command.index(
        "tests.fixtures.preprovision_extension_oauth_browser"
    )
    assert writable_command.index("tests.fixtures.preprovision_extension_oauth_browser") < (
        writable_command.index("chown -R 1000:1000")
    )
    assert writable_command.index("cp -R /app/extensions/.") < writable_command.index(
        "chown -R 1000:1000"
    )
    assert rendered["services"]["fs-app"]["user"] == "1000:1000"
    assert rendered["services"]["extension-dependencies"]["user"] == "1000:1000"
    for service_name in ("extension-dependencies", "fs-app", "oauth-runtime-permission-probe"):
        assert not any(
            volume["source"] == "oauth-browser-policy"
            for volume in rendered["services"][service_name]["volumes"]
        )
    for service_name in ("fs-app", "oauth-runtime-permission-probe"):
        assert any(
            volume["source"] == "oauth-browser-extensions"
            and volume["target"] == "/app/extensions"
            and volume["read_only"] is True
            for volume in rendered["services"][service_name]["volumes"]
        )
    assert any(
        volume["source"] == "oauth-browser-extensions"
        and volume["target"] == "/app/extensions"
        and volume.get("read_only", False) is False
        for volume in rendered["services"]["extension-dependencies"]["volumes"]
    )
    assert rendered["services"]["fs-app"]["depends_on"]["oauth-runtime-permission-probe"] == {
        "condition": "service_completed_successfully",
        "required": True,
    }
    upgrade = rendered["services"]["oauth-db-upgrade"]
    assert upgrade["command"] == [
        "python",
        "-m",
        "core.entrypoints.cli.click_app",
        "database",
        "upgrade",
        "--author",
        "oauth-browser-acceptance",
        "--maintenance-confirmed",
        "--confirm",
    ]
    assert upgrade["environment"] == {
        "FS_STACK_ENVIRONMENT": "development",
        "FS_DB_DSN": (
            "dbname=flow_steward user=flow_steward password=flow-steward-pass host=pg-fs port=5432"
        ),
        "FS_LOAD_REPO_DOTENV": "0",
    }
    assert upgrade["depends_on"] == {
        "oauth-app-volumes-init": {
            "condition": "service_completed_successfully",
            "required": True,
        },
        "pg-fs": {
            "condition": "service_healthy",
            "required": True,
        },
    }
    assert any(
        volume["source"] == "oauth-browser-extensions"
        and volume["target"] == "/app/extensions"
        and volume["read_only"] is True
        for volume in upgrade["volumes"]
    )
    assert rendered["services"]["fs-app"]["depends_on"]["oauth-db-upgrade"] == {
        "condition": "service_completed_successfully",
        "required": True,
    }
    pg_healthcheck = " ".join(rendered["services"]["pg-fs"]["healthcheck"]["test"])
    pg_environment = rendered["services"]["pg-fs"]["environment"]
    assert all(
        pg_environment.get(key) == expected
        for key, expected in {
            "POSTGRES_DB": "flow_steward",
            "POSTGRES_USER": "flow_steward",
            "POSTGRES_PASSWORD": "flow-steward-pass",
        }.items()
    ), "OAuth pg-fs credentials inherited from the outer audit environment"
    assert "pg_isready" not in pg_healthcheck
    assert "psql -U flow_steward -d flow_steward" in pg_healthcheck
    assert "workflow_runtime_dependencies_workflow_id_fkey" in pg_healthcheck
    assert "public.workflow_runtime_dependencies" in pg_healthcheck
    assert "public.registry_workflows" in pg_healthcheck
    assert "pg_constraint" in pg_healthcheck

    traefik = rendered["services"]["traefik"]
    dynamic_argument = next(
        argument
        for argument in traefik["command"]
        if argument.startswith("--providers.file.filename=")
    )
    dynamic_path = Path(dynamic_argument.split("=", 1)[1])
    dynamic_mount = next(
        volume for volume in traefik["volumes"] if volume["target"] == str(dynamic_path.parent)
    )
    cert_init = rendered["services"]["oauth-cert-init"]
    cert_mount = next(volume for volume in cert_init["volumes"] if volume["target"] == "/certs")
    generated_match = re.search(r"cat\s+>(/\S+)", "\n".join(cert_init["command"]))

    assert generated_match is not None
    generated_path = Path(generated_match.group(1))
    assert dynamic_mount["source"] == cert_mount["source"]
    assert generated_path.parent == Path(cert_mount["target"])
    assert generated_path.name == dynamic_path.name


def test_production_oauth_overlay_preserves_runtime_policy_isolation() -> None:
    production = ROOT / "infra/docker/docker-compose.prod.yaml"
    production_source = production.read_text(encoding="utf-8")
    env = {
        **os.environ,
        **{
            name: f"contract-{name.lower()}"
            for name in re.findall(r"\$\{([A-Z][A-Z0-9_]+):\?", production_source)
        },
        "FS_OAUTH_ACCEPTANCE_APP_IMAGE": "local/acceptance-app:test",
        "FS_OAUTH_ACCEPTANCE_AUDIT_IMAGE": "local/acceptance-audit:test",
        "FS_OAUTH_ACCEPTANCE_FRONTEND_IMAGE": "local/acceptance-frontend:test",
    }
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--project-name",
            "fs-oauth-generic-prod-contract-oauth-browser",
            "--file",
            str(production),
            "--file",
            str(OVERLAY),
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    services = json.loads(result.stdout)["services"]

    closure = {"oauth-browser"}
    pending = ["oauth-browser"]
    while pending:
        service_name = pending.pop()
        dependencies = services[service_name].get("depends_on") or {}
        for dependency in dependencies:
            if dependency not in closure:
                closure.add(dependency)
                pending.append(dependency)

    assert "fs-app" in closure
    assert "oauth-runtime-permission-probe" in closure
    assert "extension-dependencies" in closure
    for service_name in closure:
        for volume in services[service_name].get("volumes") or []:
            if volume.get("target") == "/app/extensions" and service_name != (
                "extension-dependencies"
            ):
                assert volume.get("read_only") is True, service_name
    for service_name in ("fs-app", "oauth-runtime-permission-probe"):
        environment = services[service_name].get("environment") or {}
        assert "FS_EXTENSION_INSTALL_POLICY_PATH" not in environment
        assert not any(
            "flow-steward-policy" in str(volume.get("target") or "")
            for volume in services[service_name].get("volumes") or []
        )


def test_preprovisioned_neutral_fixture_is_discovered_by_startup_scan(tmp_path: Path) -> None:
    extensions_root = tmp_path / "extensions"
    source = ROOT / "tests/fixtures/extensions/fixture_extension_oauth"

    installed = preprovision_extension_oauth_browser.preprovision_fixture(
        source=source,
        extensions_root=extensions_root,
        provider_base_url="https://oauth-provider:4187",
    )

    assert installed == extensions_root / "fixture_extension_oauth"
    scanned = scan_extensions(str(extensions_root))
    fixture = next(row for row in scanned if row.get("extension_key") == "fixture.extension-oauth")
    assert fixture["status"] == "valid"
    provider = fixture["compiled_manifest"]["runtime"]["extension_contract_v2"]["oauth_providers"][
        0
    ]
    assert provider["authorization_endpoint"]["url"] == "https://oauth-provider:4187/authorize"
    assert provider["token_endpoint"]["url"] == "https://oauth-provider:4187/token"
    assert provider["revocation_endpoint"]["url"] == "https://oauth-provider:4187/revoke"


def test_seed_and_provider_keep_real_tls_and_authenticated_service_paths() -> None:
    seed = (ROOT / "tests/fixtures/seed_extension_oauth_browser.py").read_text(encoding="utf-8")
    provider = (ROOT / "tests/fixtures/oauth_provider.py").read_text(encoding="utf-8")
    compose = OVERLAY.read_text(encoding="utf-8")
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert '"/api/auth/me"' in seed
    assert '"/api/extensions/install"' not in seed
    assert "required pre-provisioned extension is missing" in seed
    assert 'f"{prefix}/configurations"' in seed
    assert "--cert-file" in provider
    assert "ssl.PROTOCOL_TLS_SERVER" in provider
    assert "SSL_CERT_FILE: /certs/ca.crt" in compose
    assert "FS_PUBLIC_BASE_URL: https://traefik:8099" in compose
    assert "internal: true" in compose
    assert "PLAYWRIGHT_PACKAGE_VERSION" in compose
    assert "PYTHON_PLAYWRIGHT_VERSION" in compose
    assert "chromium.executablePath()" in compose
    assert "libnss3-tools" in dockerfile.split("FROM audit-system-tooling AS audit-tooling", 1)[0]
    browser_service = compose.split("oauth-browser:", 1)[1]
    assert "HOME: /run/oauth-browser-home" in browser_service
    assert 'mkdir -p "$${HOME}/.local/share/pki/nssdb"' in browser_service
    assert (
        'certutil -d "sql:$${HOME}/.local/share/pki/nssdb" -N --empty-password' in browser_service
    )
    assert 'certutil -d "sql:$${HOME}/.local/share/pki/nssdb" -A -t "C,,"' in browser_service
    assert "-i /certs/ca.crt" in browser_service
    assert "update-ca-certificates" in browser_service
    assert "apt-get" not in browser_service
    assert "ignoreHTTPSErrors" not in browser_service
    assert "ignoreHTTPSErrors" not in SPEC.read_text(encoding="utf-8")
    assert "FS_OAUTH_ACCEPTANCE_AUDIT_IMAGE" in compose.split("oauth-browser:", 1)[1]
    assert "--consumer-extensions-root" in compose
    assert "*/tests/e2e/*oauth*.spec.ts" in compose
    assert "oauth-browser-seed.json" in seed

    node_version = json.loads((ROOT / "assets/frontend/package.json").read_text(encoding="utf-8"))[
        "devDependencies"
    ]["@playwright/test"]
    python_version = next(
        line.split("==", 1)[1]
        for line in (ROOT / "requirements-audit.txt").read_text(encoding="utf-8").splitlines()
        if line.startswith("playwright==")
    )
    assert node_version == python_version


def test_browser_specs_verify_public_manifest_and_resolved_identity() -> None:
    generic_source = SPEC.read_text(encoding="utf-8")

    assert "Neutral Verified Account connected successfully." in generic_source
    assert 'getByText("External account connected successfully.")' in generic_source


def test_provider_retry_waits_for_the_exact_connection_response_before_readback() -> None:
    source = SPEC.read_text(encoding="utf-8")
    provider_flow = source.split(
        'test("real provider success, denial, reconnect failure, and closed redirect stay scoped"',
        1,
    )[1]
    retry_flow = provider_flow.split("const postConnectFailure", 1)[1].split("const reconnect", 1)[
        0
    ]

    wait_index = retry_flow.index("const retryResponsePromise = page.waitForResponse")
    click_index = retry_flow.index('getByRole("button", { name: "Retry" }).click()')
    response_index = retry_flow.index("const retryResponse = await retryResponsePromise")
    readback_index = retry_flow.index("const retriedResponse = await page.request.get")

    assert wait_index < click_index < response_index < readback_index
    assert "encodeURIComponent(pendingConnectionId)" in retry_flow
    assert 'outcome: "connected"' in retry_flow
    assert "connection_id: pendingConnectionId" in retry_flow
    assert "extension-oauth-connection-${encodeURIComponent(pendingConnectionId)}" in retry_flow


def test_fresh_seed_uses_canonical_bootstrap_header_and_rejects_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_post(url: str, **kwargs: object) -> SimpleNamespace:
        captured.update(url=url, **kwargs)
        return SimpleNamespace(status_code=403, text="bootstrap denied")

    monkeypatch.setattr(seed_extension_oauth_browser.httpx, "post", fake_post)

    with pytest.raises(RuntimeError, match="bootstrap failed: 403 bootstrap denied"):
        seed_extension_oauth_browser._bootstrap_admin(
            "https://traefik:8099", "canonical-bootstrap-token"
        )

    assert captured["headers"] == {AUTH_BOOTSTRAP_TOKEN_HEADER: "canonical-bootstrap-token"}


@pytest.mark.parametrize(
    ("failure", "expected_detail"),
    [
        ("http", "HTTP 503"),
        ("transport", "ConnectError: TLS verification failed"),
    ],
)
def test_seed_readiness_timeout_reports_last_bounded_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str, expected_detail: str
) -> None:
    attempts = 0
    sleeps: list[int] = []

    def fake_get(url: str, *, verify: bool, timeout: int) -> SimpleNamespace:
        nonlocal attempts
        attempts += 1
        assert url == "https://traefik:8099/health"
        assert verify is True
        assert timeout == 2
        if failure == "transport":
            request = seed_extension_oauth_browser.httpx.Request("GET", url)
            raise seed_extension_oauth_browser.httpx.ConnectError(
                "TLS verification failed", request=request
            )
        return SimpleNamespace(status_code=503)

    monkeypatch.setattr(seed_extension_oauth_browser.httpx, "get", fake_get)
    monkeypatch.setattr(seed_extension_oauth_browser.time, "sleep", sleeps.append)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "seed_extension_oauth_browser.py",
            "--base-url",
            "https://traefik:8099",
            "--bootstrap-token",
            "never-report-this-token",
            "--consumer-extensions-root",
            str(tmp_path / "extensions"),
            "--output",
            str(tmp_path / "acceptance.json"),
        ],
    )

    with pytest.raises(RuntimeError, match="Flow Steward app did not become ready") as caught:
        seed_extension_oauth_browser.main()

    message = str(caught.value)
    assert expected_detail in message
    assert "never-report-this-token" not in message
    detail = message.partition("last readiness failure: ")[2].rstrip(")")
    assert len(detail) <= 300
    assert attempts == 90
    assert sleeps == [1] * 90


def test_seed_login_owns_one_client_context_and_closes_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        def __init__(self, status_code: int, payload: dict[str, object]) -> None:
            self.status_code = status_code
            self._payload = payload
            self.content = b"{}"
            self.text = json.dumps(payload)

        def json(self) -> dict[str, object]:
            return self._payload

    class FakeClient:
        def __init__(self, responses: list[FakeResponse]) -> None:
            self.responses = responses
            self.headers: dict[str, str] = {}
            self.entered = 0
            self.closed = 0
            self.active = False

        def __enter__(self) -> FakeClient:
            self.entered += 1
            self.active = True
            return self

        def __exit__(self, *_args: object) -> None:
            self.active = False
            self.closed += 1

        def request(self, *_args: object, **_kwargs: object) -> FakeResponse:
            assert self.active, "authenticated requests must run inside the client context"
            return self.responses.pop(0)

    clients = [
        FakeClient(
            [
                FakeResponse(200, {"csrf_token": "csrf"}),
                FakeResponse(200, {"authenticated": True}),
            ]
        ),
        FakeClient([FakeResponse(401, {"error": "invalid"})]),
    ]
    monkeypatch.setattr(
        seed_extension_oauth_browser.httpx,
        "Client",
        lambda **_kwargs: clients.pop(0),
    )

    successful = clients[0]
    with seed_extension_oauth_browser._login("https://traefik:8099", "user", "password") as client:
        assert client is successful
        assert successful.active
    assert (successful.entered, successful.closed) == (1, 1)

    failing = clients[0]
    with (
        pytest.raises(RuntimeError, match="POST /api/auth/login failed with 401"),
        seed_extension_oauth_browser._login("https://traefik:8099", "user", "bad-password"),
    ):
        pytest.fail("failed login must not yield a client")
    assert (failing.entered, failing.closed) == (1, 1)


def test_seed_enables_an_existing_disabled_consumer_extension() -> None:
    class FakeResponse:
        def __init__(self, payload: dict[str, object]) -> None:
            self.status_code = 200
            self._payload = payload
            self.content = b"{}"
            self.text = json.dumps(payload)

        def json(self) -> dict[str, object]:
            return self._payload

    class FakeClient:
        def __init__(self) -> None:
            self.get_responses = [
                FakeResponse(
                    {
                        "extension_id": "vendor.consumer-oauth",
                        "install_id": "install-consumer",
                        "state": "disabled",
                    }
                ),
                FakeResponse(
                    {
                        "extension_id": "vendor.consumer-oauth",
                        "install_id": "install-consumer",
                        "state": "active",
                    }
                ),
            ]
            self.requests: list[tuple[str, str]] = []

        def get(self, _path: str) -> FakeResponse:
            return self.get_responses.pop(0)

        def request(self, method: str, path: str, **_kwargs: object) -> FakeResponse:
            self.requests.append((method, path))
            return FakeResponse({"status": "ok", "state": "active"})

    client = FakeClient()

    install_id = seed_extension_oauth_browser._install_id(
        client,  # type: ignore[arg-type]
        extension_id="vendor.consumer-oauth",
        ensure_enabled=True,
    )

    assert install_id == "install-consumer"
    assert client.requests == [("POST", "/api/extensions/vendor.consumer-oauth/enable")]


def test_seed_fails_if_startup_inventory_did_not_preprovision_extension() -> None:
    response = SimpleNamespace(status_code=404, text="missing")
    client = SimpleNamespace(get=lambda _path: response)

    with pytest.raises(RuntimeError, match="required pre-provisioned extension is missing"):
        seed_extension_oauth_browser._install_id(
            client,
            extension_id="fixture.extension-oauth",
        )


def test_manual_chrome_lane_is_explicit_loopback_only_and_self_cleaning() -> None:
    env = {
        **os.environ,
        "FS_OAUTH_ACCEPTANCE_APP_IMAGE": "local/acceptance-app:test",
        "FS_OAUTH_ACCEPTANCE_AUDIT_IMAGE": "local/acceptance-audit:test",
        "FS_OAUTH_ACCEPTANCE_FRONTEND_IMAGE": "local/acceptance-frontend:test",
        "FS_OAUTH_MANUAL_PORT": "18443",
        "FS_OAUTH_MANUAL_PROVIDER_PORT": "18444",
        "FS_OAUTH_PROVIDER_BASE_URL": "https://oauth.127.0.0.1.nip.io:18444",
        "FS_OAUTH_PROVIDER_INTERNAL_PORT": "18444",
    }
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--project-name",
            "fs-oauth-generic-manual-contract",
            "--file",
            str(ROOT / "infra/docker/docker-compose.yaml"),
            "--file",
            str(OVERLAY),
            "--file",
            str(MANUAL_OVERLAY),
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    rendered = json.loads(result.stdout)
    assert rendered["services"]["traefik"]["ports"] == [
        {
            "mode": "ingress",
            "target": 8099,
            "published": "18443",
            "protocol": "tcp",
            "host_ip": "127.0.0.1",
        }
    ]
    assert rendered["services"]["oauth-provider"]["ports"] == [
        {
            "mode": "ingress",
            "target": 18444,
            "published": "18444",
            "protocol": "tcp",
            "host_ip": "127.0.0.1",
        }
    ]
    host_access_network = "oauth-browser-host-access"
    host_access_services = {
        name
        for name, service in rendered["services"].items()
        if host_access_network in service.get("networks", {})
    }
    assert rendered["networks"]["default"]["internal"] is True
    assert rendered["networks"][host_access_network].get("internal", False) is False
    assert host_access_services == {"oauth-provider", "traefik"}
    assert rendered["services"]["fs-app"]["environment"]["FS_PUBLIC_BASE_URL"] == (
        "https://flow-steward.localhost:18443"
    )
    assert "https://oauth.127.0.0.1.nip.io:18444" in " ".join(
        rendered["services"]["oauth-app-volumes-init"]["command"]
    )
    runner = MANUAL_RUNNER.read_text(encoding="utf-8")
    provider_hostname_match = re.search(
        r'FS_OAUTH_PROVIDER_BASE_URL="https://([^:]+):\$\{PROVIDER_PORT\}"', runner
    )
    assert provider_hostname_match is not None
    provider_hostname = provider_hostname_match.group(1)
    assert provider_hostname == "oauth.127.0.0.1.nip.io"
    assert (
        provider_hostname
        in rendered["services"]["oauth-provider"]["networks"]["default"]["aliases"]
    )
    assert f"DNS:{provider_hostname}" in "\n".join(
        rendered["services"]["oauth-cert-init"]["command"]
    )
    operations = (ROOT / "docs/operations/extension-oauth-browser-acceptance.md").read_text(
        encoding="utf-8"
    )
    assert "clean committed worktree" in runner
    assert "oauth-browser-seed" in runner
    assert '"oauth-db-upgrade"' in runner
    assert "logs --no-color oauth-db-upgrade" in runner
    assert "down --volumes --remove-orphans" in runner
    assert "remove_images" in runner
    assert "-fingerprint -sha256" in runner
    assert 'delete-certificate -Z "$CA_SHA256"' in operations
    assert "delete-certificate -c" not in operations


@pytest.mark.parametrize("image", (None, "", "audit:latest", "sha256:abcd", "sha256:" + "A" * 64))
def test_external_tier_three_refuses_invalid_image_before_docker(tmp_path: Path, image) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker.log"
    docker = fake_bin / "docker"
    docker.write_text('#!/bin/sh\nprintf called >>"$FS_TEST_DOCKER_LOG"\nexit 91\n')
    docker.chmod(0o755)
    env = {key: value for key, value in os.environ.items() if key != "FS_TEST_AUDIT_IMAGE_ID"}
    env.update({"PATH": f"{fake_bin}:{env['PATH']}", "FS_TEST_DOCKER_LOG": str(docker_log)})
    if image is not None:
        env["FS_TEST_AUDIT_IMAGE_ID"] = image

    result = subprocess.run(
        ["bash", str(EXTERNAL_TIER_RUNNER), "--dry-run"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "FS_TEST_AUDIT_IMAGE_ID" in result.stderr
    assert not docker_log.exists()
