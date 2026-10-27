"""Fixed Compact runtime transport payloads, separate from gateway orchestration."""

_COMPACT_APP_READINESS_SCRIPT = """
import json

from core.entrypoints.cli.lifecycle_runtime import _runtime_health_payload

print(
    json.dumps(
        _runtime_health_payload("127.0.0.1", 8099),
        separators=(",", ":"),
    )
)
"""

_COMPACT_RUNTIME_STATE_STAGE_SCRIPT = """
import json
import os
import sys
from pathlib import Path

from core.infrastructure.runtime.compact.volume import CompactVolumeLayout, prepare_compact_volume

root = Path("/data")
prepare_compact_volume(root, layout=CompactVolumeLayout())
envelope = json.load(sys.stdin)
maintenance_state = envelope.get("maintenance_state")
worker_token = str(envelope.get("worker_token") or "").strip()
if not worker_token:
    raise ValueError("worker machine token is required")
marker = root / "state/.flow-steward/runtime/maintenance-state.json"
if maintenance_state is None:
    marker.unlink(missing_ok=True)
else:
    if not isinstance(maintenance_state, dict):
        raise ValueError("maintenance state must be an object")
    payload = (json.dumps(maintenance_state, sort_keys=True) + "\\n").encode()
    temporary = marker.with_name(".maintenance-state.json.profile-switch")
    temporary.unlink(missing_ok=True)
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o640,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chown(temporary, 0, 10102)
        os.chmod(temporary, 0o640)
        os.replace(temporary, marker)
    finally:
        temporary.unlink(missing_ok=True)
token_path = root / "state/worker-mcp-token"
token_temporary = token_path.with_name(".worker-mcp-token.profile-switch")
token_temporary.unlink(missing_ok=True)
descriptor = os.open(
    token_temporary,
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
    0o600,
)
try:
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(worker_token + "\\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(token_temporary, 0o600)
    os.replace(token_temporary, token_path)
finally:
    token_temporary.unlink(missing_ok=True)
""".strip()

_COMPACT_RUNTIME_STATE_EXPORT_SCRIPT = """
import json
from pathlib import Path

root = Path("/data/state")
worker_token = (root / "worker-mcp-token").read_text(encoding="utf-8").strip()
if not worker_token:
    raise ValueError("worker machine token is required")
marker = root / ".flow-steward/runtime/maintenance-state.json"
maintenance_state = json.loads(marker.read_text(encoding="utf-8")) if marker.exists() else None
print(json.dumps({"maintenance_state": maintenance_state, "worker_token": worker_token}))
""".strip()
