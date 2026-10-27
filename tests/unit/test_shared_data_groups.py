"""ADR 0029, decision B: both profiles reach the shared trees through the same groups.

Compact's roles and Scalable's services are the same processes packaged differently; a
file one of them writes into the artifact or extension tree must be writable by its
counterpart in the other profile. Derived: every Scalable service holds exactly the
shared groups (10100 artifacts, 10103 extension writers) of the Compact
role that does its job -- 10102 is Compact's own state and has no Scalable counterpart.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from core.infrastructure.config.compact_runtime import CompactRuntimeSettings
from core.infrastructure.runtime.compact.process_spec import build_process_specs
from core.infrastructure.runtime.compact.volume import CompactVolumeLayout

ROOT = Path(__file__).resolve().parents[2]
_COUNTERPART = {
    "fs-app": "app",
    "fs-worker": "worker-0",
    "control-plane": "control",
    "fs-extension-installer": "installer",
    "fs-extension-manager": "installer",
    "extension-dependencies": "installer",
}


def _shared_gids() -> set[int]:
    layout = CompactVolumeLayout()
    return {layout.artifact_gid, layout.extension_gid}


def test_scalable_services_hold_their_compact_counterparts_shared_groups() -> None:
    specs = build_process_specs(
        CompactRuntimeSettings.from_environment(
            {"FS_DEPLOYMENT_PROFILE": "compact", "FS_WORKER_PROCESSES": "1"}
        ),
        parent_environment={"PATH": "/usr/bin", "FS_WORKER_MCP_BEARER_TOKEN": "t"},
        public_port=8080,
        observation_fd=7,
    )
    roles = {spec.role: set(spec.supplementary_gids) & _shared_gids() for spec in specs}
    services = yaml.safe_load(
        (ROOT / "infra/docker/docker-compose.prod.yaml").read_text(encoding="utf-8")
    )["services"]

    for service, role in _COUNTERPART.items():
        declared = {int(gid) for gid in services[service].get("group_add") or []}
        assert declared == roles[role], (service, role)
    assert roles["app"] == {10100}  # non-vacuity: the anchor value


def test_only_the_installers_write_the_extension_tree() -> None:
    """AC17-R59 on both profiles: 10103 is the writers' group."""
    specs = build_process_specs(
        CompactRuntimeSettings.from_environment(
            {"FS_DEPLOYMENT_PROFILE": "compact", "FS_WORKER_PROCESSES": "1"}
        ),
        parent_environment={"PATH": "/usr/bin", "FS_WORKER_MCP_BEARER_TOKEN": "t"},
        public_port=8080,
        observation_fd=7,
    )
    writers = {
        spec.role
        for spec in specs
        if CompactVolumeLayout().extension_gid in spec.supplementary_gids
    }
    assert writers == {"installer"}
