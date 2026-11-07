from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core.application.tooling.resource_cleanup.service import ResourceCleanup
from core.infrastructure.agent_resources.gateway import NativeResources
from core.infrastructure.agent_resources.journal import ResourceJournal

OWNER = "cleanup-tests"


@pytest.fixture
def cleanup(tmp_path, monkeypatch):
    native = NativeResources()
    monkeypatch.setattr(native, "directory_consumers", lambda resource: False)
    monkeypatch.setattr(native, "discover", lambda ledger: [])
    monkeypatch.setattr(native, "inventory", dict)
    monkeypatch.setattr(native, "select_audit_image", lambda script: None)
    journal = ResourceJournal(tmp_path / "registry")
    original_workspace = journal.workspace
    monkeypatch.setattr(
        journal,
        "workspace",
        lambda owner, token: (
            tmp_path.resolve() / "managed" / "workspaces" / original_workspace(owner, token).name
        ),
    )
    return ResourceCleanup(journal, native)


def test_cleanup_is_preview_and_requires_release(cleanup):
    cleanup.initialize(OWNER)
    resource = cleanup.create_directory(OWNER, "scratch")
    p = Path(resource.identity)
    (p / "result.txt").write_text("disposable")
    assert cleanup.cleanup(OWNER, apply=True).blocked
    assert p.exists()
    cleanup.release(OWNER, resource.resource_id)
    assert cleanup.cleanup(OWNER).planned == (resource.resource_id,)
    assert p.exists()
    result = cleanup.cleanup(OWNER, apply=True)
    assert result.removed == (resource.resource_id,)
    assert not p.exists()
    assert not cleanup.cleanup(OWNER, apply=True).blocked


def test_foreign_owner_cannot_reuse_ledger(cleanup):
    cleanup.initialize(OWNER)
    with pytest.raises(ValueError):
        cleanup.release("../other", "anything")


@pytest.mark.parametrize("name", ["..", "../foreign", "/tmp", ".", "a/b"])
def test_directory_creation_rejects_escape(cleanup, name):
    cleanup.initialize(OWNER)
    with pytest.raises(ValueError):
        cleanup.create_directory(OWNER, name)


def test_replaced_directory_is_not_deleted(cleanup):
    cleanup.initialize(OWNER)
    resource = cleanup.create_directory(OWNER, "scratch")
    p = Path(resource.identity)
    p.rename(p.with_name("original"))
    p.mkdir()
    (p / "important").write_text("foreign data")
    cleanup.release(OWNER, resource.resource_id)
    assert cleanup.cleanup(OWNER, apply=True).blocked
    assert (p / "important").read_text() == "foreign data"


def test_symlink_does_not_delete_foreign_directory(cleanup, tmp_path):
    cleanup.initialize(OWNER)
    resource = cleanup.create_directory(OWNER, "scratch")
    p = Path(resource.identity)
    p.rename(p.with_name("original"))
    target = tmp_path / "foreign"
    target.mkdir()
    (target / "keep").write_text("keep")
    p.symlink_to(target, target_is_directory=True)
    cleanup.release(OWNER, resource.resource_id)
    assert cleanup.cleanup(OWNER, apply=True).blocked
    assert (target / "keep").exists()


def test_git_checkout_is_preserved_even_if_released(cleanup):
    cleanup.initialize(OWNER)
    resource = cleanup.create_directory(OWNER, "source")
    (Path(resource.identity) / ".git").write_text("gitdir: /important")
    cleanup.release(OWNER, resource.resource_id)
    assert cleanup.cleanup(OWNER, apply=True).blocked
    assert Path(resource.identity).exists()


def test_retained_consumer_blocks_cleanup(cleanup):
    cleanup.initialize(OWNER)
    resource = cleanup.create_directory(OWNER, "scratch")
    cleanup.retain(
        OWNER,
        resource.resource_id,
        consumer="review",
        reason="live review",
        release_event="review finished",
        review_at="2030-01-01T00:00:00Z",
    )
    with pytest.raises(ValueError, match="consumer"):
        cleanup.release(OWNER, resource.resource_id)
    receipt = cleanup.cleanup(OWNER, apply=True)
    assert resource.resource_id in receipt.retained
    assert not receipt.removed


def test_live_run_blocks_cleanup(cleanup):
    cleanup.initialize(OWNER)
    resource = cleanup.create_directory(OWNER, "scratch")
    cleanup.release(OWNER, resource.resource_id)
    with cleanup.journal.locked():
        ledger = cleanup.journal.load(OWNER)
        ledger.run_pid = os.getpid()
        cleanup.journal.save(ledger)
    assert cleanup.cleanup(OWNER, apply=True).blocked
    assert Path(resource.identity).exists()


def test_borrower_in_another_ledger_prevents_directory_removal(cleanup):
    cleanup.initialize(OWNER)
    cleanup.initialize("reviewer")
    resource = cleanup.create_directory(OWNER, "scratch")
    cleanup.track("reviewer", "directory", resource.identity, borrowed=True)
    cleanup.release(OWNER, resource.resource_id)
    assert cleanup.cleanup(OWNER, apply=True).blocked
    assert Path(resource.identity).exists()


def test_legacy_or_corrupt_ledger_is_not_automatically_adopted(cleanup):
    cleanup.journal.root.mkdir(parents=True)
    (cleanup.journal.root / f"{OWNER}.json").write_text(json.dumps({"owner": OWNER}))
    with pytest.raises(ValueError, match="schema"):
        cleanup.cleanup(OWNER, apply=True)


def test_automatic_run_cleans_scratch_after_child_failure(cleanup):
    from core.application.tooling.resource_cleanup.lifecycle import run_owned

    cleanup.initialize(OWNER)
    resource = cleanup.create_directory(OWNER, "output")
    result = run_owned(cleanup, OWNER, ["python", "-c", "raise SystemExit(7)"])
    assert result.command_exit_code == 7
    assert result.cleanup_ok
    assert not Path(resource.identity).exists()
    assert cleanup.journal.load(OWNER).run_pid is None


def test_run_success_cleans_automatically_and_is_repeatable(cleanup):
    from core.application.tooling.resource_cleanup.lifecycle import run_owned

    cleanup.initialize(OWNER)
    for _ in range(2):
        result = run_owned(
            cleanup,
            OWNER,
            [
                "python",
                "-c",
                'import os,pathlib; pathlib.Path(os.environ["FS_TASK_SCRATCH"], "file").write_text("temporary")',
            ],
        )
        assert result.exit_code == 0
        assert result.cleanup_ok
        assert not Path(cleanup.journal.load(OWNER).workspace).exists()


def test_missing_command_still_cleans_and_preserves_exit_code(cleanup):
    from core.application.tooling.resource_cleanup.lifecycle import run_owned

    cleanup.initialize(OWNER)
    result = run_owned(cleanup, OWNER, ["/nonexistent/owned-command"])
    assert result.command_exit_code == 127
    assert result.cleanup_ok
    assert not Path(cleanup.journal.load(OWNER).workspace).exists()


def test_background_process_group_prevents_automatic_deletion(cleanup, monkeypatch):
    from core.application.tooling.resource_cleanup.lifecycle import run_owned

    cleanup.initialize(OWNER)
    monkeypatch.setattr(cleanup.native, "process_group_alive", lambda pgid: True)
    result = run_owned(cleanup, OWNER, ["python", "-c", "pass"])
    assert result.command_exit_code == 0
    assert result.exit_code == 2
    assert not result.cleanup_ok
    assert Path(cleanup.journal.load(OWNER).workspace).exists()


def test_directory_with_docker_bind_consumer_is_preserved(cleanup, monkeypatch):
    cleanup.initialize(OWNER)
    resource = cleanup.create_directory(OWNER, "scratch")
    cleanup.release(OWNER, resource.resource_id)
    monkeypatch.setattr(cleanup.native, "directory_consumers", lambda resource: True)
    assert cleanup.cleanup(OWNER, apply=True).blocked
    assert Path(resource.identity).exists()


def test_unsupported_resource_kind_fails_closed(cleanup):
    cleanup.initialize(OWNER)
    with pytest.raises(ValueError, match="unsupported"):
        cleanup.track(OWNER, "shared_build_cache", "default")


def test_cleanup_errors_do_not_hide_successful_command_result(cleanup, monkeypatch):
    from core.application.tooling.resource_cleanup.lifecycle import run_owned

    cleanup.initialize(OWNER)
    monkeypatch.setattr(cleanup.native, "directory_consumers", lambda resource: True)
    result = run_owned(cleanup, OWNER, ["python", "-c", "pass"])
    assert result.command_exit_code == 0
    assert result.exit_code == 2
    assert not result.cleanup_ok
    receipt = json.loads((cleanup.journal.root / "receipts" / f"{OWNER}.json").read_text())
    assert receipt["command_exit_code"] == 0
    assert receipt["blocked"]


def test_unknown_workspace_residue_is_not_silently_deleted(cleanup):
    cleanup.initialize(OWNER)
    resource = cleanup.create_directory(OWNER, "scratch")
    workspace = Path(cleanup.journal.load(OWNER).workspace)
    (workspace / "unregistered").write_text("preserve this")
    cleanup.release(OWNER, resource.resource_id)
    result = cleanup.cleanup(OWNER, apply=True)
    assert "workspace" in result.blocked
    assert (workspace / "unregistered").read_text() == "preserve this"


def test_invalid_peer_journal_blocks_before_any_removal(cleanup):
    cleanup.initialize(OWNER)
    resource = cleanup.create_directory(OWNER, "scratch")
    cleanup.release(OWNER, resource.resource_id)
    (cleanup.journal.root / "foreign.json").write_text("{invalid")
    with pytest.raises(ValueError):
        cleanup.cleanup(OWNER, apply=True)
    assert Path(resource.identity).exists()


def test_discovery_failure_keeps_resources_and_command_result(cleanup, monkeypatch):
    from core.application.tooling.resource_cleanup.lifecycle import run_owned

    cleanup.initialize(OWNER)

    def unavailable(ledger):
        raise ValueError("Docker inspection unavailable")

    monkeypatch.setattr(cleanup.native, "discover", unavailable)
    result = run_owned(cleanup, OWNER, ["python", "-c", "raise SystemExit(9)"])
    assert result.command_exit_code == 9
    assert result.exit_code == 9
    assert not result.cleanup_ok
    assert Path(cleanup.journal.load(OWNER).workspace).exists()


def test_retained_consumers_can_release_independently(cleanup):
    cleanup.initialize(OWNER)
    resource = cleanup.create_directory(OWNER, "scratch")
    for consumer in ("review-one", "review-two"):
        cleanup.retain(
            OWNER,
            resource.resource_id,
            consumer=consumer,
            reason="review",
            release_event="review ends",
            review_at="2030-01-01T00:00:00Z",
        )
    cleanup.release(OWNER, resource.resource_id, consumer="review-one")
    record = cleanup.journal.load(OWNER).resources[0]
    assert record.state == "retained"
    assert record.consumers == ["review-two"]
    cleanup.release(OWNER, resource.resource_id, consumer="review-two")
    assert cleanup.cleanup(OWNER, apply=True).removed == (resource.resource_id,)


def test_quality_gate_gets_private_project_labels_and_cache_export(cleanup, tmp_path):
    cleanup.initialize(OWNER)
    scratch = cleanup.create_directory(OWNER, "scratch")
    cache = cleanup.create_directory(OWNER, "cache")
    checkout = tmp_path / "checkout"
    script = checkout / "core/entrypoints/cli/scripts/lint.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/bash\n")
    (checkout / "infra/docker").mkdir(parents=True)
    (checkout / "infra/docker/docker-compose.yaml").write_text("services: {}")
    (checkout / ".env").write_text("")
    ledger = cleanup.journal.load(OWNER)
    env = cleanup.native.prepare_environment(
        ledger, ["bash", str(script), "--fast"], {}, scratch.identity, cache.identity
    )
    assert env["FS_COMPOSE_PROJECT_NAME"] == "fs-owned-" + ledger.token
    assert env["FS_INSTALLER_STATE_DIR"].startswith(cache.identity + "/")
    overlay = json.loads((Path(scratch.identity) / "owned-quality.yaml").read_text())
    for name in ("fs-audit", "pg-audit", "rabbitmq-audit"):
        assert overlay["services"][name]["labels"]["org.flowsteward.resource-token"] == ledger.token
    assert "volumes" not in overlay
    assert "sonarqube" not in overlay["services"]


def test_managed_quality_automatically_uses_its_private_builder(cleanup, monkeypatch):
    from core.application.tooling.resource_cleanup.lifecycle import run_owned

    cleanup.initialize(OWNER)
    created, executed = [], []

    def prepare(ledger, command, environment, scratch, cache):
        return {**environment, "FS_OWNED_QUALITY": ledger.token}

    def create(ledger):
        name = "fs-owned-" + ledger.token
        created.append(name)
        return name, {
            "owner": ledger.owner,
            "container_id": "builder-container",
            "volume_name": "builder-cache",
        }

    validate = cleanup.native.validate
    remove = cleanup.native.remove
    monkeypatch.setattr(cleanup.native, "prepare_environment", prepare)
    monkeypatch.setattr(cleanup.native, "create_builder", create)
    monkeypatch.setattr(
        cleanup.native,
        "validate",
        lambda ledger, r: True if r.kind == "builder" else validate(ledger, r),
    )
    monkeypatch.setattr(
        cleanup.native,
        "remove",
        lambda ledger, r: None if r.kind == "builder" else remove(ledger, r),
    )

    def execute(command, environment, on_start):
        executed.append(environment)
        return 0

    monkeypatch.setattr(cleanup.native, "execute_owned", execute)
    assert run_owned(cleanup, OWNER, ["quality-command"]).cleanup_ok
    assert len(created) == 1
    assert executed[0]["BUILDX_BUILDER"] == created[0]


def test_unrelated_docker_inventory_change_is_not_reported_as_success(cleanup, monkeypatch):
    cleanup.initialize(OWNER)
    resource = cleanup.create_directory(OWNER, "scratch")
    cleanup.release(OWNER, resource.resource_id)
    snapshots = iter([{"containers": ["foreign-id"]}, {"containers": []}])
    monkeypatch.setattr(cleanup.native, "inventory", lambda: next(snapshots), raising=False)
    receipt = cleanup.cleanup(OWNER, apply=True)
    assert "inventory" in receipt.blocked
    assert receipt.inventory_changes["containers"]["removed"] == ["foreign-id"]


@pytest.mark.parametrize(
    "arguments,environment", [(["--with-sonar"], {}), (["--fast"], {"SONAR_ENABLED": "1"})]
)
def test_managed_quality_rejects_all_shared_sonar_entrypoints(
    cleanup, tmp_path, arguments, environment
):
    cleanup.initialize(OWNER)
    script = tmp_path / "checkout/core/entrypoints/cli/scripts/lint.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/bash\n")
    with pytest.raises(ValueError, match="Sonar"):
        cleanup.native.prepare_environment(
            cleanup.journal.load(OWNER),
            ["bash", str(script), *arguments],
            environment,
            str(tmp_path),
            str(tmp_path),
        )


@pytest.mark.parametrize("children_alive", [False, True])
def test_managed_ready_image_is_borrowed_without_private_builder_or_deletion(
    cleanup, tmp_path, monkeypatch, children_alive
):
    from core.application.tooling.resource_cleanup.lifecycle import run_owned
    from core.infrastructure.agent_resources import quality_environment

    image_id = "sha256:" + "a" * 64
    checkout = tmp_path / "checkout"
    script = checkout / "core/entrypoints/cli/scripts/lint.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/bash\n")
    (checkout / "infra/docker").mkdir(parents=True)
    (checkout / "infra/docker/docker-compose.yaml").write_text("services: {}")
    (checkout / ".env").write_text("")
    borrowed = []
    capture = cleanup.native.capture

    def capture_borrowed(ledger, kind, identity, *, borrowed=False):
        if identity == image_id:
            assert borrowed
            return {"native_id": image_id, "daemon_id": "daemon"}
        return capture(ledger, kind, identity, borrowed=borrowed)

    def prepare(ledger, command, environment, scratch, cache):
        return quality_environment.prepare(
            ledger, command, environment, scratch, cache, select_image=lambda path: image_id
        )

    def execute(command, environment, on_start):
        if children_alive:
            on_start(987654)
        assert environment["FS_BORROWED_AUDIT_IMAGE"] == image_id
        assert "BUILDX_BUILDER" not in environment
        import yaml

        class ComposeLoader(yaml.SafeLoader):
            pass

        ComposeLoader.add_constructor("!reset", lambda loader, node: None)
        overlay = yaml.load(
            (Path(environment["FS_TASK_SCRATCH"]) / "owned-quality.yaml").read_text(),
            Loader=ComposeLoader,
        )
        assert overlay["services"]["fs-audit"]["image"] == image_id
        assert overlay["services"]["fs-audit"]["build"] is None
        ledger = cleanup.journal.load(OWNER)
        borrowed.extend(r for r in ledger.resources if not r.disposable)
        assert borrowed[-1].identity == image_id
        assert borrowed[-1].state == "active"
        return 0

    monkeypatch.setattr(cleanup.native, "capture", capture_borrowed)
    monkeypatch.setattr(cleanup.native, "prepare_environment", prepare)
    monkeypatch.setattr(cleanup.native, "execute_owned", execute)
    monkeypatch.setattr(
        cleanup.native,
        "create_builder",
        lambda ledger: pytest.fail("ready image must not create a builder"),
    )
    monkeypatch.setattr(cleanup.native, "process_group_alive", lambda pgid: children_alive)
    for _ in range(1 if children_alive else 2):
        outcome = run_owned(cleanup, OWNER, ["bash", str(script), "--checks", "ruff"])
        assert outcome.cleanup_ok is not children_alive
    resources = cleanup.journal.load(OWNER).resources
    images = [r for r in resources if r.identity == image_id]
    assert len(images) == 1
    assert images[0].state == ("active" if children_alive else "released")
    assert not images[0].disposable


@pytest.mark.parametrize("same_daemon", [False, True])
def test_borrowed_immutable_image_blocks_owner_alias_cleanup(cleanup, monkeypatch, same_daemon):
    proof = {"native_id": "sha256:" + "a" * 64, "daemon_id": "daemon"}
    monkeypatch.setattr(cleanup.native, "capture", lambda *args, **kwargs: proof)
    cleanup.initialize(OWNER)
    alias = cleanup.track(OWNER, "image_tag", "fs-owned/" + "a" * 32 + ":audit")
    cleanup.release(OWNER, alias.resource_id)
    cleanup.initialize("borrower")
    if not same_daemon:
        monkeypatch.setattr(
            cleanup.native,
            "capture",
            lambda *args, **kwargs: {**proof, "daemon_id": "another-daemon"},
        )
    cleanup.track("borrower", "image_tag", proof["native_id"], borrowed=True)
    monkeypatch.setattr(cleanup.native, "validate", lambda *args: True)
    monkeypatch.setattr(cleanup.native, "remove", lambda *args: None)
    receipt = cleanup.cleanup(OWNER, apply=True)
    assert (alias.resource_id in receipt.blocked) is same_daemon
    assert (alias.resource_id in receipt.removed) is not same_daemon
