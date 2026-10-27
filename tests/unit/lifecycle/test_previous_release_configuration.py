"""AC17-R153: an installation written by the previous release must stay readable.

a8fa4e220 added `worker_replicas` to the configuration. Every existing installation
then failed its update at the end -- "The installed configuration is unreadable or
invalid" -- and every `config` command with it, on both profiles. The stack.toml the
previous release wrote lacks the new field, so it no longer counted as canonical and
the reader fell back to the installer journal, whose own check demanded "the complete
serialized shape" and refused the same missing field.

The fixtures are what d9ca8ea45 itself wrote, generated from that tree: the installer
journal's configuration object (32 keys, ending in worker_processes, as on the stands)
and the canonical stack.toml. The rule these tests hold: a field the current release
added since that shape is filled from its default; a key the model does not know is
still refused; and any field added from now on must be declared as added, with a
default, or the first test fails.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from core.application.installer import InstallConfig
from core.application.lifecycle.contracts import DeploymentConfig
from core.infrastructure.lifecycle import configuration_store as store_module
from core.infrastructure.lifecycle.configuration_store import LifecycleConfigurationStore

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures/lifecycle/previous-release-d9ca8ea45"
PROFILES = ("scalable", "compact")


def _previous_journal_config(profile: str) -> dict:
    return json.loads((FIXTURES / f"install-state-config-{profile}.json").read_text())


def test_every_field_since_the_previous_release_is_declared_as_added_with_a_default() -> None:
    """The guard for the next field: it cannot be added without deciding this."""
    previous = set(_previous_journal_config("scalable"))
    assert len(previous) == 32  # non-vacuity: the shape the stands were written in

    added = set(InstallConfig.model_fields) - previous

    assert added == set(store_module._FIELDS_ADDED_SINCE_PREVIOUS_SHAPE)
    for name in added:
        assert not InstallConfig.model_fields[name].is_required(), name


def _installation(tmp_path: Path, profile: str) -> LifecycleConfigurationStore:
    state = tmp_path / ".flow-steward"
    state.mkdir()
    shutil.copy(FIXTURES / f"stack-{profile}.toml", state / "stack.toml")
    journal = {"config": _previous_journal_config(profile)}
    (state / "install-state.json").write_text(json.dumps(journal), encoding="utf-8")
    return LifecycleConfigurationStore(tmp_path)


@pytest.mark.parametrize("profile", PROFILES)
def test_the_previous_releases_installation_reads_with_the_new_fields_defaulted(
    tmp_path: Path, profile: str
) -> None:
    store = _installation(tmp_path, profile)

    active = store.read_active()

    assert active is not None
    assert active.deployment_profile == profile
    assert active.worker_replicas == 1
    assert active.public_host == "fs.example.test"  # read, not defaulted


@pytest.mark.parametrize("profile", PROFILES)
def test_the_previous_journal_alone_still_reads(tmp_path: Path, profile: str) -> None:
    """The fallback the reader takes for an older, partial stack.toml."""
    store = _installation(tmp_path, profile)

    config = store._installer_journal_config()

    assert config["worker_replicas"] == 1
    assert config["deployment_profile"] == profile


def test_a_key_the_model_does_not_know_is_still_refused(tmp_path: Path) -> None:
    store = _installation(tmp_path, "scalable")
    journal = {"config": {**_previous_journal_config("scalable"), "worker_replicaz": 3}}
    (tmp_path / ".flow-steward/install-state.json").write_text(json.dumps(journal))

    with pytest.raises(ValueError, match="unexpected: worker_replicaz"):
        store._installer_journal_config()


def test_a_truncated_journal_is_still_refused(tmp_path: Path) -> None:
    """Defaults are for fields added since; a field the previous release wrote is not one."""
    store = _installation(tmp_path, "scalable")
    truncated = _previous_journal_config("scalable")
    del truncated["public_host"]
    (tmp_path / ".flow-steward/install-state.json").write_text(json.dumps({"config": truncated}))

    with pytest.raises(ValueError, match="missing: public_host"):
        store._installer_journal_config()


@pytest.mark.parametrize("profile", PROFILES)
def test_a_saved_previous_configuration_validates(profile: str) -> None:
    """Rollback reads the previous configuration back; that is the old shape too."""
    previous = _previous_journal_config(profile)

    assert DeploymentConfig.model_validate(previous).worker_replicas == 1
