"""Deployment delivery of the operator's agent text-file policy."""

from pathlib import Path

import pytest
import yaml

from core.infrastructure.config import env_registry
from core.infrastructure.runtime.compact.process_spec import _RUNTIME_ENV, _environment

ROOT = Path(__file__).resolve().parents[2]
NAME = "FS_AGENT_TEXT_FILE_EXTENSIONS"
DEFAULT = "txt,md,markdown,json,xml,csv,tsv,yaml,yml,rtf"


def test_agent_text_policy_is_operator_configurable_with_canonical_default():
    spec = env_registry.env_spec_map()[NAME]
    assert spec.operator_settable is True
    assert env_registry.registry_default_value(NAME) == DEFAULT


@pytest.mark.parametrize(
    "filename", ["docker-compose.yaml", "docker-compose.prod.yaml", "docker-compose.compact.yaml"]
)
def test_compose_defaults_absent_and_empty_agent_text_policy(filename):
    data = yaml.safe_load((ROOT / "infra/docker" / filename).read_text())
    section = (
        "x-compact-runtime-environment" if "compact" in filename else "x-app-worker-environment"
    )
    assert data[section][NAME] == "${FS_AGENT_TEXT_FILE_EXTENSIONS:-" + DEFAULT + "}"


def test_compact_children_drop_empty_and_preserve_custom_agent_text_policy():
    assert NAME in _RUNTIME_ENV
    assert NAME not in _environment({NAME: ""}, _RUNTIME_ENV)
    assert NAME not in _environment({}, _RUNTIME_ENV)
    assert _environment({NAME: "custom"}, _RUNTIME_ENV)[NAME] == "custom"


def test_compact_app_and_worker_receive_custom_agent_text_policy():
    from core.infrastructure.config.compact_runtime import CompactRuntimeSettings
    from core.infrastructure.runtime.compact.process_spec import build_process_specs

    settings = CompactRuntimeSettings.from_environment(
        {"FS_DEPLOYMENT_PROFILE": "compact", "FS_WORKER_PROCESSES": "1"}
    )
    specs = build_process_specs(
        settings,
        parent_environment={
            "PATH": "/usr/bin",
            "FS_WORKER_MCP_BEARER_TOKEN": "dummy",
            NAME: "txt,xml,foo",
        },
        public_port=8080,
        observation_fd=7,
    )
    roles = {spec.role: spec for spec in specs}
    assert roles["app"].environment[NAME] == "txt,xml,foo"
    assert roles["worker-0"].environment[NAME] == "txt,xml,foo"
