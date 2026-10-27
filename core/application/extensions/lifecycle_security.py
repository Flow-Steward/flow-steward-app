"""Security scan helpers for extension lifecycle imports."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path


def _scan_extension_snapshot(
    payload: dict, *, artifact_type: str, artifact_id: str, account_id: str
) -> None:
    """Run inbound security scan on a extension artifact snapshot before DB persistence.

    Raises ``RuntimeError`` if the scanner decides to *block* the payload.
    Non-fatal errors (scanner unavailable, import errors) are silently ignored
    so that offline / minimal deployments can still import extensions.
    """
    try:
        from core.infrastructure.security.integration import scan_inbound

        text = json.dumps(payload, ensure_ascii=False)
        decision = scan_inbound(
            text,
            surface=f"extension_import/{artifact_type}",
            project_id=None,
            job_id=None,
            account_id=account_id,
        )
        if decision.action == "block":
            raise RuntimeError(
                f"SEC-010: extension artifact '{artifact_type}/{artifact_id}' blocked by security policy"
            )
    except RuntimeError:
        raise  # security block must propagate — bubble up to the caller
    except Exception:
        return  # scanner unavailable — non-fatal, allow import


def _scan_extension_bundle(bundle_dir: Path, *, extension_id: str, account_id: str) -> None:
    """Scan text files in an extension bundle directory before copying to target.

    Walks the bundle and concatenates text content of source files (*.py, *.js,
    *.ts, *.sh, *.yaml, *.yml, *.json, *.toml) for a single inbound scan call.
    Raises ``RuntimeError`` if the scanner decides to *block* the bundle.
    Non-fatal errors (scanner unavailable) are silently ignored.
    """
    _TEXT_EXTS = {".py", ".js", ".ts", ".sh", ".yaml", ".yml", ".json", ".toml", ".txt", ".md"}
    try:
        from core.infrastructure.security.integration import scan_inbound

        parts: list[str] = []
        for path in sorted(bundle_dir.rglob("*")):
            if path.is_file() and path.suffix.lower() in _TEXT_EXTS:
                with contextlib.suppress(Exception):
                    parts.append(
                        f"# {path.relative_to(bundle_dir)}\n{path.read_text(encoding='utf-8', errors='replace')}"
                    )
        if not parts:
            return
        combined = "\n".join(parts)
        decision = scan_inbound(
            combined,
            surface="extension_import/extension_bundle",
            project_id=None,
            job_id=None,
            account_id=account_id,
        )
        if decision.action == "block":
            raise RuntimeError(
                f"SEC-011: extension bundle for extension '{extension_id}' blocked by security policy"
            )
    except RuntimeError:
        raise  # security block must propagate
    except Exception:
        return  # scanner unavailable — non-fatal
