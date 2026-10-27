"""AC17-R178: a Compact update never removed a superseded runtime image.

Live on fs-acc (2026-09-25): 31 flow-steward-compact images after one day of updates,
26.7 GB of images, while doctor's release_retention said ok -- Compact kept no release
directories, and retention looked at nothing else. The Scalable rule now holds on Compact
too: the running revision, its rollback, the newest others up to the count; only this
installation's own images, never one a container uses."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from core.application.doctor import repairs
from core.application.installer import InstallConfig
from core.infrastructure.installer import release_retention
from core.infrastructure.installer.gateway import DockerInstallerGateway
from core.infrastructure.installer.process import ProcessOutcome
from core.infrastructure.installer.state_store import InstallerStateStore

_COMPACT = InstallConfig(environment="production", deployment_profile="compact")
_PROJECT = "fs-acc"


def _rev(digit: str) -> str:
    return digit * 40


def _id(digit: str) -> str:
    return "sha256:" + digit * 64


@dataclass
class _Image:
    image_id: str
    revision: str
    created: str
    project: str = _PROJECT  # "" for one built before the label
    tags: tuple[str, ...] = ()
    size: int = 2 * 1024**3


@dataclass
class _Docker:
    images: list[_Image]
    in_use: set[str] = field(default_factory=set)  # by a container of any project
    commands: list[tuple[str, ...]] = field(default_factory=list)

    def run(self, command, **_kwargs):
        key = tuple(command)
        self.commands.append(key)
        if key[:1] == ("git",) and "rev-parse" in key:
            return ProcessOutcome(command=key, returncode=0, stdout=_rev("d") + "\n")
        if key[:3] == ("docker", "image", "ls"):
            label = key[key.index("--filter") + 1].removeprefix("label=")
            name, _, project = label.partition("=")
            assert name == release_retention.INSTALLATION_LABEL
            ids = [image.image_id for image in self.images if image.project == project]
            return ProcessOutcome(command=key, returncode=0, stdout="\n".join(ids) + "\n")
        if key[:3] == ("docker", "image", "inspect"):
            wanted = set(key[5:])
            if "join .RepoTags" in key[4]:  # as the Docker of fs-acc answers it
                error = (
                    'template parsing error: template: :1:89: executing "" at <.RepoTags>: '
                    "wrong type for value; expected []string; got []interface {}"
                )
                return ProcessOutcome(command=key, returncode=1, stderr="\n".join([error] * 2))
            lines = [
                f"{i.image_id}\t{i.revision}\t{i.created}\t{i.size}\t{json.dumps(list(i.tags))}"
                for i in self.images
                if i.image_id in wanted
            ]
            gone = [f"Error: No such image: {image}" for image in sorted(wanted) if not any(
                i.image_id == image for i in self.images)]  # fmt: skip
            return ProcessOutcome(
                command=key,
                returncode=1 if gone else 0,
                stdout="\n".join(lines) + "\n",
                stderr="\n".join(gone),
            )
        if key[:3] == ("docker", "ps", "--all"):
            image = key[-1].removeprefix("ancestor=")
            return ProcessOutcome(
                command=key, returncode=0, stdout="c0ffee\n" if image in self.in_use else ""
            )
        if key[:3] == ("docker", "image", "rm"):
            for target in key[3:]:
                self.images = [
                    i for i in self.images if target != i.image_id and target not in i.tags
                ]
            return ProcessOutcome(command=key, returncode=0)
        return ProcessOutcome(command=key, returncode=0)

    def removed(self) -> list[tuple[str, ...]]:
        return [key[3:] for key in self.commands if key[:3] == ("docker", "image", "rm")]


def _gateway(tmp_path: Path, docker: _Docker, *, current_image: str = "") -> DockerInstallerGateway:
    (tmp_path / "infra/docker").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".env.prod").write_text(f"FS_COMPACT_IMAGE={current_image}\n", encoding="utf-8")
    return DockerInstallerGateway(
        repo_root=tmp_path,
        state_store=InstallerStateStore(tmp_path / ".flow-steward", lifecycle_state_root=tmp_path),
        process=docker,
        environment={"FS_COMPOSE_PROJECT_NAME": _PROJECT},
    )


def _tag(revision: str) -> tuple[str, ...]:
    return (f"flow-steward-compact:{revision[:12]}",)


def _update_images() -> list[_Image]:
    """Current C, its rollback P, and three older builds of this installation."""
    return [
        _Image(_id("c"), _rev("c"), "2026-09-25T15:00:00Z", tags=_tag(_rev("c"))),
        _Image(_id("b"), _rev("b"), "2026-09-25T14:00:00Z", tags=_tag(_rev("b"))),
        _Image(_id("3"), _rev("3"), "2026-09-25T13:00:00Z", tags=_tag(_rev("3"))),
        _Image(_id("2"), _rev("2"), "2026-09-25T12:00:00Z", tags=_tag(_rev("2"))),
        _Image(_id("1"), _rev("1"), "2026-09-25T11:00:00Z", tags=_tag(_rev("1"))),
    ]


def _prune(gateway: DockerInstallerGateway) -> str:
    log = gateway.state_store.open_log("r178")
    try:
        return gateway.prune_superseded_releases(
            _COMPACT, current=_rev("c"), previous=_rev("b"), log=log
        )
    finally:
        log.close()


def test_an_update_retains_images_when_exclusive_rollback_ownership_is_unproven(
    tmp_path: Path,
) -> None:
    docker = _Docker(_update_images(), in_use={_id("2")})  # a container still runs on it
    gateway = _gateway(tmp_path, docker, current_image=_id("c"))

    summary = _prune(gateway)

    assert docker.removed() == []
    assert {image.image_id for image in docker.images} == {
        _id("c"),
        _id("b"),
        _id("3"),
        _id("2"),
        _id("1"),
    }
    assert summary.startswith(
        "Retained 3 Compact images: exclusive installation ownership is unproven"
    )
    assert "another installation's rollback" in summary


def test_another_installations_image_is_never_touched(tmp_path: Path) -> None:
    """The owner's production can share the host; its images carry its own project."""
    theirs = _Image(_id("9"), _rev("9"), "2026-09-25T10:00:00Z", project="fs-demo",
                    tags=_tag(_rev("9")))  # fmt: skip
    docker = _Docker([*_update_images(), theirs])
    gateway = _gateway(tmp_path, docker, current_image=_id("c"))

    _prune(gateway)

    assert theirs in docker.images
    assert not any(_id("9") in key for key in docker.commands)  # not even inspected


def test_an_image_built_before_the_label_is_ours_only_by_this_installations_log(
    tmp_path: Path,
) -> None:
    ours_unlabelled = _Image(_id("4"), _rev("4"), "2026-09-25T09:00:00Z", project="")
    someone_unlabelled = _Image(_id("5"), _rev("5"), "2026-09-25T08:00:00Z", project="")
    docker = _Docker([*_update_images(), ours_unlabelled, someone_unlabelled])
    gateway = _gateway(tmp_path, docker, current_image=_id("c"))
    gateway.state_store.logs_dir.mkdir(parents=True, exist_ok=True)
    (gateway.state_store.logs_dir / "install-20260925T090000Z-a.log").write_text(
        f"2026-09-25T09:00:00+00:00 Built Compact runtime image flow-steward-compact:"
        f"{_rev('4')[:12]} ({_id('4')}) at revision {_rev('4')}.\n",
        encoding="utf-8",
    )

    _prune(gateway)

    assert docker.removed() == []
    assert ours_unlabelled in docker.images
    assert someone_unlabelled in docker.images


def test_an_image_someone_else_tagged_and_the_recorded_image_are_kept(tmp_path: Path) -> None:
    images = _update_images()
    images[2] = _Image(_id("3"), _rev("3"), "2026-09-25T13:00:00Z",
                       tags=(*_tag(_rev("3")), "registry.example/backup:keep"))  # fmt: skip
    images[4] = _Image(_id("1"), "", "2026-09-25T11:00:00Z", tags=_tag(_rev("1")))
    docker = _Docker(images)
    # .env.prod records the undated one: an operator pinned it, whatever it says
    gateway = _gateway(tmp_path, docker, current_image=_id("1"))

    _prune(gateway)

    assert docker.removed() == []


def test_doctor_counts_the_superseded_compact_images(tmp_path: Path) -> None:
    docker = _Docker(_update_images())
    gateway = _gateway(tmp_path, docker, current_image=_id("c"))
    gateway.state_store.root.mkdir(parents=True, exist_ok=True)
    gateway.record_installed_releases(current=_rev("c"), previous=_rev("b"))

    plan = gateway.superseded_releases(_COMPACT, current=_rev("c"))

    assert plan.kept == (_rev("c"), _rev("b"))
    assert plan.ambiguous_images == (*_tag(_rev("3")), *_tag(_rev("2")), *_tag(_rev("1")))
    assert docker.removed() == []  # a read removes nothing

    @dataclass
    class _Probe:
        profile: str = "compact"

        def retention(self):
            return repairs.RetentionFacts(
                ambiguous_images=plan.ambiguous_images, kept=plan.kept, free_bytes=50 * 1024**3,
                minimum_free_bytes=4 * 1024**3,
            )  # fmt: skip

    (check,) = [c for c in repairs.repair_checks() if c.check_id == "release_retention"]
    finding = check.diagnose(_Probe())
    assert (finding.status, finding.repairable) == ("warn", False)
    assert "3 images retained" in finding.cause
    assert "unproven ownership" in finding.next_step


def test_a_compact_build_is_labelled_with_its_installation(tmp_path: Path) -> None:
    docker = _Docker([])
    gateway = _gateway(tmp_path, docker)
    log = gateway.state_store.open_log("r178-build")
    try:
        gateway._build_compact_runtime_image(_COMPACT, log=log)
    except Exception:
        pass  # the fake builds nothing; the command is what is asserted
    finally:
        log.close()

    (build,) = [key for key in docker.commands if key[:3] == ("docker", "buildx", "build")]
    label = build[build.index("--label") + 1]
    assert label == f"{release_retention.INSTALLATION_LABEL}={_PROJECT}"


def test_legacy_fs_acc_logs_inventory_images_without_proving_exclusive_ownership(
    tmp_path: Path,
) -> None:
    """R178 live on fs-acc (8608af223): 57 install logs, 18 "Built Compact runtime image"
    lines, "Validated exact prebuilt image role=compact" lines for images it did not build,
    no installed-releases.json -- and nothing was removed: `join .RepoTags` was a template
    error for every image, and retention found nothing to do."""
    current, previous = _rev("f"), _rev("e")
    built = [(_id(str(n % 10)) if n < 10 else "sha256:" + f"{n:02d}" * 32, "%040x" % (n + 1))
             for n in range(18)]  # fmt: skip
    images = [
        _Image(image_id, revision, f"2026-09-25T{n:02d}:00:00Z", project="",
               tags=_tag(revision))
        for n, (image_id, revision) in enumerate(built)
    ]  # fmt: skip
    images.append(_Image(_id("f"), current, "2026-09-25T20:00:00Z", tags=_tag(current)))
    images.append(_Image(_id("e"), previous, "2026-09-25T19:00:00Z", project="",
                         tags=_tag(previous)))  # fmt: skip
    validated_only = _Image(_id("a"), _rev("a"), "2026-09-25T01:00:00Z", project="",
                            tags=_tag(_rev("a")))  # fmt: skip
    images.append(validated_only)
    docker = _Docker(images)
    gateway = _gateway(tmp_path, docker, current_image=_id("f"))
    logs = gateway.state_store.logs_dir
    logs.mkdir(parents=True, exist_ok=True)
    for n in range(57):
        lines = [f"2026-09-25T00:00:{n:02d}+00:00 Running: docker compose ps"]
        if n < 18:
            image_id, revision = built[n]
            lines.append(f"Built Compact runtime image flow-steward-compact:{revision[:12]} "
                         f"({image_id}) at revision {revision}.")  # fmt: skip
        if n == 18:
            lines.append(f"Built Compact runtime image flow-steward-compact:{previous[:12]} "
                         f"({_id('e')}) at revision {previous}.")  # fmt: skip
        if n == 30:
            lines.append(f"Validated exact prebuilt image role=compact id={_id('a')} "
                         f"revision={_rev('a')}.")  # fmt: skip
        if n == 56:
            lines.append(f"Updated from {previous} to {current}. The schema check passed.")
        (logs / f"install-20260925T{n:06d}Z-x.log").write_text("\n".join(lines) + "\n", "utf-8")
    assert not (gateway.state_store.root / release_retention.RECORD_NAME).exists()
    log = gateway.state_store.open_log("r178-live")

    summary = gateway.prune_superseded_releases(_COMPACT, current=current, previous="", log=log)
    log.close()

    left = {image.image_id for image in docker.images}
    assert left == {image.image_id for image in images}
    assert docker.removed() == []
    assert summary.startswith("Retained 18 Compact images")
    assert f"protects {current[:12]}, {previous[:12]}" in summary


def test_a_docker_that_cannot_describe_the_images_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never "within the retention" when retention could not look."""
    from core.infrastructure.installer import gateway_worker_scale as worker_scale_module

    monkeypatch.setattr(
        worker_scale_module,
        "_COMPACT_IMAGE_FACTS",
        worker_scale_module._COMPACT_IMAGE_FACTS.replace("json .RepoTags", 'join .RepoTags ","'),
    )
    docker = _Docker(_update_images())
    gateway = _gateway(tmp_path, docker, current_image=_id("c"))

    summary = _prune(gateway)

    assert summary.startswith("Superseded Compact images were not removed: Docker could not")
    assert "expected []string" in summary
    assert docker.removed() == []
    current = _rev("c")
    with pytest.raises(OSError, match="could not describe"):
        gateway.superseded_releases(_COMPACT, current=current)


def test_build_label_does_not_prove_exclusive_rollback_ownership(tmp_path: Path) -> None:
    docker = _Docker(_update_images())
    gateway = _gateway(tmp_path, docker, current_image=_id("c"))
    other_installation = tmp_path / "other"
    other_installation.mkdir()
    (other_installation / "installed-releases.json").write_text(
        json.dumps({"current": _rev("9"), "previous": _rev("3")}), encoding="utf-8"
    )

    summary = _prune(gateway)

    assert docker.removed() == []
    assert any(image.image_id == _id("3") for image in docker.images)
    assert "ownership" in summary
