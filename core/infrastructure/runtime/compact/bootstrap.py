"""Single-owner, restart-safe Compact bootstrap composition."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from core.infrastructure.runtime.compact.volume import CompactVolumeLayout, prepare_compact_volume


@dataclass(frozen=True, slots=True)
class CompactBootstrapResult:
    phases: tuple[str, ...]
    state_path: Path


class CompactBootstrap:
    """Compose existing idempotent schema, seed, and identity operations once."""

    def __init__(
        self,
        *,
        data_root: Path,
        schema: Callable[[], None],
        seed: Callable[[], None],
        identity: Callable[[], None],
        verify_schema: Callable[[], None],
        layout: CompactVolumeLayout | None = None,
        extension_dependencies: Callable[[Path, CompactVolumeLayout], object] | None = None,
        runtime_secrets: Callable[[Path, CompactVolumeLayout], object] | None = None,
    ) -> None:
        self._data_root = Path(data_root)
        self._schema = schema
        self._seed = seed
        self._identity = identity
        self._verify_schema = verify_schema
        self._layout = layout
        self._extension_dependencies = extension_dependencies
        self._runtime_secrets = runtime_secrets

    def run(self) -> CompactBootstrapResult:
        phases: list[str] = []
        selected_layout = self._layout or CompactVolumeLayout()
        prepare_compact_volume(self._data_root, layout=selected_layout)
        phases.append("volume")
        if self._runtime_secrets is not None:
            # First thing after the volume exists, and before anything that could write
            # an encrypted value: the state directory has to be there to hold the keys,
            # and nothing after this point may run without them.
            self._runtime_secrets(self._data_root, selected_layout)
        phases.append("secrets")
        if self._extension_dependencies is not None:
            # Before anything that could serve a request:
            # Scalable gates every consumer on this same step completing, so a Compact
            # runtime that started without it would be the profile where an extension
            # is enabled and then fails on its own imports.
            self._extension_dependencies(self._data_root / "extensions", selected_layout)
        phases.append("dependencies")
        self._schema()
        phases.append("schema")
        self._seed()
        phases.append("seed")
        self._identity()
        phases.append("identity")
        self._verify_schema()
        phases.append("verified")
        state_path = self._data_root / "state" / "compact-bootstrap.json"
        self._write_state(state_path, phases)
        return CompactBootstrapResult(phases=tuple(phases), state_path=state_path)

    @staticmethod
    def _write_state(path: Path, phases: list[str]) -> None:
        payload = json.dumps({"version": 2, "phases": phases}, sort_keys=True) + "\n"
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary)
