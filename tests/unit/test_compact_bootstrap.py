from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.entrypoints.cli import compact_runtime
from core.infrastructure.runtime.compact.bootstrap import CompactBootstrap
from core.infrastructure.runtime.compact.provenance import (
    EmbeddedImageIdentity,
    verify_compact_environment_identity,
    verify_embedded_image_identity,
)
from core.infrastructure.runtime.compact.volume import (
    CompactVolumeLayout,
    prepare_compact_volume,
)


def _layout() -> CompactVolumeLayout:
    return CompactVolumeLayout.for_current_user()


def test_compact_volume_preparation_is_idempotent_and_exact(tmp_path: Path) -> None:
    tmp_path.chmod(0o755)

    first = prepare_compact_volume(tmp_path, layout=_layout())
    marker = tmp_path / "extensions" / "keep.txt"
    marker.write_text("preserve", encoding="utf-8")
    marker.chmod(0o640)
    second = prepare_compact_volume(tmp_path, layout=_layout())

    assert first == second
    assert marker.read_text(encoding="utf-8") == "preserve"
    assert marker.stat().st_mode & 0o777 == 0o640
    assert (tmp_path / "artifacts").stat().st_mode & 0o7777 == 0o2770
    assert (tmp_path / "logs" / "worker").stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "state" / ".flow-steward" / "runtime").stat().st_mode & 0o7777 == 0o2750


def test_compact_volume_rejects_symlink_ancestor_without_touching_target(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o755)
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / "logs").symlink_to(outside, target_is_directory=True)

    layout = _layout()

    with pytest.raises(ValueError, match=r"symlink|directory"):
        prepare_compact_volume(tmp_path, layout=layout)

    assert list(outside.iterdir()) == []


def test_compact_volume_rejects_unsafe_restored_mode_without_rewriting_it(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o755)
    extensions = tmp_path / "extensions"
    extensions.mkdir(mode=0o777)
    extensions.chmod(0o777)
    before = extensions.stat()

    layout = _layout()

    with pytest.raises(ValueError, match="mode"):
        prepare_compact_volume(tmp_path, layout=layout)

    after = extensions.stat()
    assert (after.st_mode & 0o7777, after.st_uid, after.st_gid) == (
        before.st_mode & 0o7777,
        os.getuid(),
        os.getgid(),
    )


@pytest.mark.parametrize(
    "field",
    ("index_digest", "manifest_digest", "config_digest", "revision", "role", "platform"),
)
def test_embedded_image_identity_rejects_every_mismatch(field: str) -> None:
    expected = EmbeddedImageIdentity(
        index_digest="sha256:index",
        manifest_digest="sha256:manifest",
        config_digest="sha256:config",
        revision="a" * 40,
        role="compact-runtime",
        platform="linux/arm64",
    )
    values = expected.as_dict()
    values[field] = "mismatch"

    actual = EmbeddedImageIdentity.from_mapping(values)

    with pytest.raises(ValueError, match=field.replace("_", " ")):
        verify_embedded_image_identity(expected=expected, actual=actual)


def test_embedded_image_identity_reports_every_mismatch_with_both_values() -> None:
    """AC17-R91: one mismatch per rebuild, and no values, cost a full install each.

    The verifier returned on the first mismatched field and named only the field,
    so an operator learned about the next wrong field only after rebuilding the
    image and reinstalling.
    """
    expected = EmbeddedImageIdentity(
        index_digest="sha256:index",
        manifest_digest="sha256:manifest",
        config_digest="sha256:config",
        revision="a" * 40,
        role="compact-runtime",
        platform="linux/arm64",
    )
    actual = EmbeddedImageIdentity(
        index_digest="sha256:index",
        manifest_digest="sha256:manifest",
        config_digest="sha256:config",
        revision="unknown",
        role="compact-runtime",
        platform="unknown",
    )

    with pytest.raises(ValueError) as raised:
        verify_embedded_image_identity(expected=expected, actual=actual)

    message = str(raised.value)
    assert "revision" in message
    assert "platform" in message
    assert "a" * 40 in message
    assert "unknown" in message
    assert "role" not in message


def test_embedded_image_identity_accepts_exact_match() -> None:
    identity = EmbeddedImageIdentity(
        index_digest="sha256:index",
        manifest_digest="sha256:manifest",
        config_digest="sha256:config",
        revision="b" * 40,
        role="compact-runtime",
        platform="linux/amd64",
    )

    verify_embedded_image_identity(expected=identity, actual=identity)


def test_compact_environment_identity_requires_exact_release_metadata() -> None:
    values = {
        "IMAGE_REVISION": "c" * 40,
        "IMAGE_ROLE": "compact-runtime",
        "IMAGE_PLATFORM": "linux/amd64",
    }
    environment = {f"FS_EMBEDDED_{name}": value for name, value in values.items()} | {
        f"FS_COMPACT_{name}": value for name, value in values.items()
    }

    assert verify_compact_environment_identity(environment).role == "compact-runtime"
    environment["FS_COMPACT_IMAGE_PLATFORM"] = "linux/arm64"
    with pytest.raises(ValueError, match="platform"):
        verify_compact_environment_identity(environment)


def test_compact_runtime_does_not_treat_operator_digest_env_as_attestation() -> None:
    environment = {
        "FS_EMBEDDED_IMAGE_REVISION": "c" * 40,
        "FS_EMBEDDED_IMAGE_ROLE": "compact-runtime",
        "FS_EMBEDDED_IMAGE_PLATFORM": "linux/amd64",
        "FS_COMPACT_IMAGE_REVISION": "c" * 40,
        "FS_COMPACT_IMAGE_ROLE": "compact-runtime",
        "FS_COMPACT_IMAGE_PLATFORM": "linux/amd64",
        "FS_COMPACT_IMAGE_INDEX_DIGEST": "sha256:operator-supplied",
        "FS_COMPACT_IMAGE_MANIFEST_DIGEST": "sha256:operator-supplied",
        "FS_COMPACT_IMAGE_CONFIG_DIGEST": "sha256:operator-supplied",
    }

    assert verify_compact_environment_identity(environment).revision == "c" * 40


@pytest.mark.parametrize("interrupt_phase", ("schema", "seed", "identity", "verify"))
def test_compact_bootstrap_can_resume_after_each_interruption(
    tmp_path: Path, interrupt_phase: str
) -> None:
    tmp_path.chmod(0o755)
    completed: set[str] = set()
    calls: list[str] = []

    def phase(name: str):
        def run() -> None:
            calls.append(name)
            if name == interrupt_phase and name not in completed:
                completed.add(name)
                raise RuntimeError(f"interrupted after {name}")
            completed.add(name)

        return run

    bootstrap = CompactBootstrap(
        data_root=tmp_path,
        schema=phase("schema"),
        seed=phase("seed"),
        identity=phase("identity"),
        verify_schema=phase("verify"),
        layout=_layout(),
    )

    with pytest.raises(RuntimeError, match="interrupted"):
        bootstrap.run()
    result = bootstrap.run()

    assert result.phases == (
        "volume",
        "secrets",
        "dependencies",
        "schema",
        "seed",
        "identity",
        "verified",
    )
    assert result.state_path.stat().st_mode & 0o777 == 0o600
    assert calls.count(interrupt_phase) == 2


def test_compact_entrypoint_runs_complete_database_initialization(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[str] = []
    monkeypatch.setenv("FS_DB_DSN", "postgresql://example")
    # Exercise the steady-state branch without inheriting an operator setup token.
    monkeypatch.delenv("FS_AUTH_BOOTSTRAP_TOKEN", raising=False)
    monkeypatch.delenv("FS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT", raising=False)
    monkeypatch.setattr(
        compact_runtime.init_db,
        "bootstrap_canonical_schema_if_empty",
        lambda **_kwargs: calls.append("schema"),
    )
    monkeypatch.setattr(
        compact_runtime.upgrade_db,
        "baseline_migration_history",
        lambda **_kwargs: calls.append("baseline"),
    )
    monkeypatch.setattr(
        compact_runtime.init_db,
        "initialize_database",
        lambda: calls.append("initialize"),
    )
    monkeypatch.setattr(compact_runtime, "create_identity_store", lambda _dsn: object())
    monkeypatch.setattr(
        compact_runtime.WorkerIdentityBootstrapService,
        "ensure",
        lambda self, **_kwargs: self._secret_store.replace("token"),
    )
    monkeypatch.setattr(compact_runtime, "_run_schema_compatibility_check", lambda: None)

    class _Bootstrap:
        def __init__(self, **kwargs) -> None:
            self._kwargs = kwargs

        def run(self) -> None:
            self._kwargs["schema"]()
            self._kwargs["seed"]()
            self._kwargs["identity"]()
            self._kwargs["verify_schema"]()

    monkeypatch.setattr(compact_runtime, "CompactBootstrap", _Bootstrap)
    monkeypatch.setattr(compact_runtime, "create_user_counter", lambda _dsn: lambda: 1)

    assert compact_runtime._bootstrap(tmp_path).worker_token == "token"
    assert calls == ["schema", "baseline", "initialize"]


def test_compact_bootstrap_materializes_hashed_setup_token_for_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import hashlib
    import json

    runtime = tmp_path / "state" / ".flow-steward" / "runtime"
    runtime.mkdir(parents=True)
    monkeypatch.setenv("FS_AUTH_BOOTSTRAP_TOKEN", "setup-secret")
    monkeypatch.setenv("FS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT", "2099-01-01T00:00:00Z")
    monkeypatch.setattr(os, "chown", lambda *_args, **_kwargs: None)

    compact_runtime._sync_bootstrap_token_state(tmp_path, user_count=lambda: 0)

    state = runtime / "bootstrap-token-state.json"
    assert json.loads(state.read_text(encoding="utf-8")) == {
        "expires_at": "2099-01-01T00:00:00Z",
        "token_sha256": hashlib.sha256(b"setup-secret").hexdigest(),
    }
    assert state.stat().st_mode & 0o777 == 0o640


def test_the_image_starts_where_no_installer_ever_ran() -> None:
    """AC17-R109: the published image could not start outside our own installer.

    Anywhere the installer had not run -- a PaaS, `docker run`, a person trying the
    product -- the FS_COMPACT_IMAGE_* triple is absent, and emptiness was rejected
    before the comparison, so "nobody claimed anything about this image" was
    indistinguishable from "the claim is empty". The image knows its own revision,
    role and platform: they are baked in at build time, which is the whole point of
    the embedded identity.
    """
    identity = verify_compact_environment_identity(
        {
            "FS_EMBEDDED_IMAGE_REVISION": "d" * 40,
            "FS_EMBEDDED_IMAGE_ROLE": "compact-runtime",
            "FS_EMBEDDED_IMAGE_PLATFORM": "linux/amd64",
        }
    )

    assert identity.revision == "d" * 40
    assert identity.role == "compact-runtime"


def test_release_metadata_that_disagrees_with_the_image_still_stops_the_runtime() -> None:
    """The check itself is not what was broken, so it keeps working."""
    with pytest.raises(ValueError, match="revision"):
        verify_compact_environment_identity(
            {
                "FS_EMBEDDED_IMAGE_REVISION": "d" * 40,
                "FS_EMBEDDED_IMAGE_ROLE": "compact-runtime",
                "FS_EMBEDDED_IMAGE_PLATFORM": "linux/amd64",
                "FS_COMPACT_IMAGE_REVISION": "e" * 40,
                "FS_COMPACT_IMAGE_ROLE": "compact-runtime",
                "FS_COMPACT_IMAGE_PLATFORM": "linux/amd64",
            }
        )


def test_partial_release_metadata_names_the_variables_it_is_missing() -> None:
    """Half a claim is a mistake, and the refusal has to say which half.

    All or nothing is the rule; the previous message said only that some field was
    empty, without naming the side or the variable, and was found by reading the
    source.
    """
    with pytest.raises(ValueError, match="FS_COMPACT_IMAGE_PLATFORM"):
        verify_compact_environment_identity(
            {
                "FS_EMBEDDED_IMAGE_REVISION": "d" * 40,
                "FS_EMBEDDED_IMAGE_ROLE": "compact-runtime",
                "FS_EMBEDDED_IMAGE_PLATFORM": "linux/amd64",
                "FS_COMPACT_IMAGE_REVISION": "d" * 40,
                "FS_COMPACT_IMAGE_ROLE": "compact-runtime",
            }
        )


def test_an_image_built_without_its_identity_says_so() -> None:
    """The one case that really is an empty identity, named as itself."""
    with pytest.raises(ValueError, match="FS_EMBEDDED_IMAGE_REVISION"):
        verify_compact_environment_identity(
            {
                "FS_EMBEDDED_IMAGE_ROLE": "compact-runtime",
                "FS_EMBEDDED_IMAGE_PLATFORM": "linux/amd64",
            }
        )


def test_an_unmanaged_runtime_mints_records_and_announces_its_own_setup_link(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC17-R117: PID 1 is the only thing running on such an installation.

    No installer wrote a token into the environment, so nothing will unless the
    runtime does it itself, and nothing else is in a position to: every command that
    issues one is an installer command that runs on the operator's machine.
    """
    import hashlib
    import json

    runtime = tmp_path / "state" / ".flow-steward" / "runtime"
    runtime.mkdir(parents=True)
    for name in (
        "FS_AUTH_BOOTSTRAP_TOKEN",
        "FS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT",
        "FS_AUTH_BOOTSTRAP_ENABLED",
        "FS_LOCAL_AUTH_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FS_PUBLIC_BASE_URL", "https://flow.example.eu")
    monkeypatch.setattr(os, "chown", lambda *_args, **_kwargs: None)

    state_file = compact_runtime._sync_bootstrap_token_state(tmp_path, user_count=lambda: 0)

    state = runtime / "bootstrap-token-state.json"
    assert state_file == str(state)
    payload = json.loads(state.read_text(encoding="utf-8"))
    assert state.stat().st_mode & 0o777 == 0o640

    announced = [line for line in capsys.readouterr().err.splitlines() if "#setup-token=" in line]
    assert len(announced) == 1
    token = announced[0].rsplit("#setup-token=", 1)[1].strip()
    assert announced[0].startswith("Flow Steward has no administrator yet.")
    assert "https://flow.example.eu/bootstrap#setup-token=" in announced[0]
    # the token itself is never stored, only the hash the request path compares against
    assert payload["token_sha256"] == hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert token not in state.read_text(encoding="utf-8")


def test_an_installation_with_an_administrator_is_told_why_no_link_appeared(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A silent decline is the shape that costs rounds; the reason is one line."""
    (tmp_path / "state" / ".flow-steward" / "runtime").mkdir(parents=True)
    for name in (
        "FS_AUTH_BOOTSTRAP_TOKEN",
        "FS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT",
        "FS_AUTH_BOOTSTRAP_ENABLED",
        "FS_LOCAL_AUTH_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)

    state_file = compact_runtime._sync_bootstrap_token_state(tmp_path, user_count=lambda: 3)

    captured = capsys.readouterr().err
    assert state_file == ""
    assert "#setup-token=" not in captured
    assert "already has its first administrator" in captured


def test_the_app_is_told_which_state_file_to_honour() -> None:
    """Without this the minted link exists and nothing consults it."""
    environment: dict[str, str] = {}

    compact_runtime._publish_bootstrap_state_file(environment, "/data/state/x.json")

    assert environment["FS_AUTH_BOOTSTRAP_STATE_FILE"] == "/data/state/x.json"


def test_a_state_file_someone_else_named_is_not_respelled() -> None:
    environment = {"FS_AUTH_BOOTSTRAP_STATE_FILE": "/run/flow-steward-runtime/state.json"}

    compact_runtime._publish_bootstrap_state_file(environment, "/data/state/x.json")

    assert environment["FS_AUTH_BOOTSTRAP_STATE_FILE"] == "/run/flow-steward-runtime/state.json"


def test_an_empty_state_file_name_is_not_published() -> None:
    """The empty string is not absent downstream; Compose taught us that twice."""
    environment: dict[str, str] = {}

    compact_runtime._publish_bootstrap_state_file(environment, "")

    assert "FS_AUTH_BOOTSTRAP_STATE_FILE" not in environment


def test_the_app_process_actually_receives_the_state_file_we_publish() -> None:
    """The name has to survive the supervisor's allowlist, not just be set here."""
    from core.infrastructure.config.compact_runtime import CompactRuntimeSettings
    from core.infrastructure.runtime.compact.process_spec import build_process_specs

    settings = CompactRuntimeSettings.from_environment(
        {"FS_DEPLOYMENT_PROFILE": "compact", "FS_WORKER_PROCESSES": "1"}
    )
    parent = {
        "FS_WORKER_MCP_BEARER_TOKEN": "worker-token",
        "FS_AUTH_BOOTSTRAP_STATE_FILE": "/data/state/.flow-steward/runtime/state.json",
    }

    specs = build_process_specs(
        settings,
        parent_environment=parent,
        public_port=8080,
        observation_fd=1,
    )

    app = next(spec for spec in specs if spec.role == "app")
    assert app.environment["FS_AUTH_BOOTSTRAP_STATE_FILE"] == (
        "/data/state/.flow-steward/runtime/state.json"
    )


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_the_compact_runtime_runs_every_python_prestart_gate_scalable_enforces() -> None:
    """AC17-R120: Compact had no step materializing extension dependencies.

    Derived from what Scalable already declares rather than from a list of steps I
    happen to remember. A service that other services name with
    `condition: service_completed_successfully` is, by that declaration, something the
    profile refuses to run without; if it is a `python -m` module, the Compact runtime
    -- which has no services to gate on -- has to reach the same module from the one
    process that starts it.

    Not a packaging question: `bundled_dependencies` has been a declared entrypoint of
    the Compact slice all along, so the module was in the image and nothing invoked it.
    That is why the closure is taken from the entrypoint that actually starts Compact.

    The one gate deliberately outside this is `dev-dataset-fixture`, whose command is
    psql rather than a module. Read it before excusing it: it drops a development-only
    reader role and schema if they exist. Compact is production-only by validation, so
    that role is never created there and there is nothing to drop.
    """
    import importlib.util
    import sys

    import yaml

    root = _repository_root()
    compose = yaml.safe_load(
        (root / "infra/docker/docker-compose.prod.yaml").read_text(encoding="utf-8")
    )
    services = compose["services"]
    gated = {
        name
        for spec in services.values()
        for name, condition in (spec.get("depends_on") or {}).items()
        if isinstance(condition, dict)
        and condition.get("condition") == "service_completed_successfully"
    }
    module_gates = {}
    for name in sorted(gated):
        command = services[name].get("command") or []
        if (
            isinstance(command, list)
            and len(command) >= 3
            and str(command[0]).startswith("python")
            and command[1] == "-m"
        ):
            module_gates[name] = str(command[2])
    assert module_gates, "no python pre-start gate found; this guard would be vacuous"

    helper_path = root / "infra/docker/materialize_source_slice.py"
    spec = importlib.util.spec_from_file_location("compact_gate_closure", helper_path)
    assert spec is not None
    assert spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    # Registered before execution: the helper defines dataclasses, and dataclasses
    # resolves annotations through sys.modules[cls.__module__].
    sys.modules[spec.name] = helper
    spec.loader.exec_module(helper)
    closure = helper.static_python_paths(
        source_root=root,
        entrypoints=["core.entrypoints.cli.compact_runtime"],
    )

    for service, module in module_gates.items():
        expected = module.replace(".", "/") + ".py"
        assert expected in closure, (
            f"Scalable refuses to start its consumers until '{service}' has run "
            f"'{module}', and nothing on the path from the Compact entrypoint reaches it"
        )


def test_extension_dependencies_are_materialized_before_the_schema(
    tmp_path: Path,
) -> None:
    """Order is the point: installed extensions get their dependencies before anything serves."""
    tmp_path.chmod(0o755)
    order: list[str] = []

    def phase(name: str):
        def run() -> None:
            order.append(name)

        return run

    def dependencies(extensions_root: Path, layout: CompactVolumeLayout) -> None:
        order.append(f"dependencies:{extensions_root.name}:{layout.extension_gid}")

    result = CompactBootstrap(
        data_root=tmp_path,
        schema=phase("schema"),
        seed=phase("seed"),
        identity=phase("identity"),
        verify_schema=phase("verify"),
        layout=_layout(),
        extension_dependencies=dependencies,
    ).run()

    assert order[0] == f"dependencies:extensions:{_layout().extension_gid}"
    assert order[1] == "schema"
    assert result.phases == (
        "volume",
        "secrets",
        "dependencies",
        "schema",
        "seed",
        "identity",
        "verified",
    )


def test_the_compact_entrypoint_wires_the_real_materializer() -> None:
    """The step existing and the runtime using it are two different facts."""
    import inspect

    from core.infrastructure.runtime.compact.extension_dependencies import (
        materialize_compact_extension_dependencies,
    )

    source = inspect.getsource(compact_runtime._bootstrap)
    assert "extension_dependencies=materialize_compact_extension_dependencies" in source
    assert callable(materialize_compact_extension_dependencies)


def test_the_materialized_site_is_handed_to_the_extensions_group(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Compact's bundles are installer-owned and this runs as PID 1.

    Without the ownership pass pip's output lands root-owned inside a `2750`
    installer-owned tree: readable today only by the default mode, unwritable by the
    process that later re-materializes it, and looser than everything around it. The
    same trap bit Scalable from the other side (AC17-R116, third obstacle).
    """
    from core.infrastructure.runtime.compact import extension_dependencies as materializer

    bundle = tmp_path / "acme_storage"
    site = bundle / ".fs-python" / "0.1.6" / "site"
    (site / "paramiko").mkdir(parents=True)
    (site / "paramiko" / "__init__.py").write_text("x", encoding="utf-8")
    script = site / "bin" / "sftp-tool"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o777)

    monkeypatch.setattr(
        materializer, "materialize_bundled_extension_dependencies", lambda _root: [{"ok": 1}]
    )
    chowned: list[tuple[str, int, int]] = []
    real_chown = os.chown

    def record(path, uid, gid, **kwargs):
        chowned.append((str(path), uid, gid))
        if (uid, gid) == (os.getuid(), os.getgid()):
            real_chown(path, uid, gid, **kwargs)

    monkeypatch.setattr(os, "chown", record)

    evidence = materializer.materialize_compact_extension_dependencies(
        tmp_path, CompactVolumeLayout()
    )

    assert evidence == ({"ok": 1},)
    owners = {(uid, gid) for _path, uid, gid in chowned}
    assert owners == {(CompactVolumeLayout().installer_uid, CompactVolumeLayout().extension_gid)}
    touched = {path for path, _uid, _gid in chowned}
    assert str(site / "paramiko" / "__init__.py") in touched
    assert str(bundle / ".fs-python") in touched
    # ADR 0029, decision B: the extension tree's modes, shared with Scalable's installer
    assert (site / "paramiko").stat().st_mode & 0o7777 == 0o2775
    assert (site / "paramiko" / "__init__.py").stat().st_mode & 0o777 == 0o664
    # an executable keeps its executable bit; the mode rule is the bundle's own
    assert script.stat().st_mode & 0o777 == 0o775


def test_a_bundle_whose_wheels_do_not_materialize_stops_the_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fail-closed, for the reason Scalable is: a runtime that came up with half its
    extensions unusable is found by a user, and one that refused to come up is found
    by the operator starting it."""
    from core.infrastructure.runtime.compact import extension_dependencies as materializer

    def explode(_root):
        raise RuntimeError("Shipped extension 'x' did not materialize bundled wheels")

    monkeypatch.setattr(materializer, "materialize_bundled_extension_dependencies", explode)

    layout = _layout()

    with pytest.raises(RuntimeError, match="did not materialize"):
        materializer.materialize_compact_extension_dependencies(tmp_path, layout)


def test_compact_platform_commands_can_read_the_installation_they_manage() -> None:
    """AC17-R137: `proxy-fragment` told a READY installation to run ./install.sh.

    Platform commands resolve the installed configuration through
    `_lifecycle_configuration_store()`, which falls back to `<repo>/.flow-steward`
    when nothing points it elsewhere. In the Compact image that path is a symlink into
    the data volume, which holds runtime state and never the installation's own
    answers -- the installer writes those on the host. So the store found an empty
    directory and reported, correctly for what it could see, that nothing was
    installed.

    Scalable never had this because its own fs-cli already sets the same variable. The
    fix is that Compact sets it too. Release upgrade is a one-shot platform command
    that atomically advances installer-owned release state, so this narrowly scoped
    CLI service needs a read-write mount while ordinary runtime services remain
    read-only. The operation log keeps its separate runtime-volume mount.
    """
    import yaml

    compose = yaml.safe_load(
        (_repository_root() / "infra/docker/docker-compose.compact.yaml").read_text(
            encoding="utf-8"
        )
    )
    service = compose["services"]["fs-cli"]
    state_root = str((service.get("environment") or {}).get("FS_CONFIGURATION_STATE_ROOT") or "")

    assert state_root, (
        "the Compact platform dispatcher is not told where the installed configuration "
        "is, so every command that reads it answers as though nothing were installed"
    )
    # Not split(":"): the source is `${FS_INSTALLER_STATE_DIR:-../../.flow-steward}`
    # and its own default contains a colon. I made this exact mistake in the HOME guard
    # earlier and fixed it there; repeating it is what a regex anchored on the
    # container path prevents.
    import re

    mounts = {}
    for item in service.get("volumes") or []:
        if not isinstance(item, str):
            continue
        match = re.search(r":(/[^:]+)(?::(\w+))?$", item)
        if match:
            mounts[match.group(1)] = item
    assert state_root in mounts, f"{state_root} is named but nothing is mounted there"
    assert mounts[state_root].endswith(":rw"), (
        "the one-shot platform container must be able to atomically advance "
        "installer-owned release state during a verified upgrade"
    )
    assert (service.get("environment") or {}).get("FS_INSTALLER_STATE_DIR") == state_root
