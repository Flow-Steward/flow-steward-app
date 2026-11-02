"""Worker-owned, invocation-scoped access to files selected for an agent step."""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from core.application.orchestration.expected_input_sources import resolve_expected_input_source

_MAX_FILES = 10
_MAX_FILE_BYTES = 50 * 1024 * 1024
_MAX_TOTAL_FILE_BYTES = 100 * 1024 * 1024
_MAX_TEXT_CHARS = 8192
_MAX_TOTAL_TEXT_CHARS = 50 * 1024 * 1024
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_ARTIFACT = re.compile(r"^art_[A-Za-z0-9_-]+$")
_TEXT_TYPES = frozenset({"text/plain", "text/markdown", "application/json"})


class AgentFileAccessError(ValueError):
    """A requested file cannot be safely exposed to an agent."""


@dataclass(frozen=True)
class _AuthorizedFile:
    name: str
    artifact_id: str
    filename: str
    content_type: str
    size_bytes: int
    sha256: str


class AgentFileContext:
    """Private handle table for one agent invocation; no artifact IDs reach the model."""

    def __init__(self, runtime: AgentFileRuntime, files: list[_AuthorizedFile]) -> None:
        self._runtime = runtime
        self._files = {item.name: item for item in files}
        self._text_chars_read = 0

    @property
    def plane(self) -> Any:
        """Host-only artifact plane used to issue extension grants."""
        return self._runtime.plane

    @property
    def grant_scope(self) -> dict[str, str]:
        """Worker-verified scope for host-only grant preparation."""
        return {
            "account_id": self._runtime.account_id,
            "project_id": self._runtime.project_id,
            "job_id": self._runtime.job_id,
        }

    def list_files(self) -> list[dict[str, Any]]:
        return [
            {
                "name": f.name,
                "filename": f.filename,
                "content_type": f.content_type,
                "size_bytes": f.size_bytes,
            }
            for f in self._files.values()
        ]

    def _selected(self, name: str) -> _AuthorizedFile:
        selected = self._files.get(name)
        if selected is None:
            raise AgentFileAccessError("File is unavailable")
        if self._runtime._authorize(selected.name, selected.artifact_id) != selected:
            raise AgentFileAccessError("File is unavailable")
        return selected

    def authorized_artifact_id(self, name: str) -> str:
        """For host-only grant preparation, never a model-facing response."""
        selected = self._selected(name)
        self.read_bytes(name, max_size_bytes=_MAX_FILE_BYTES)
        return selected.artifact_id

    def read_bytes(self, name: str, *, max_size_bytes: int) -> bytes:
        selected = self._selected(name)
        if (
            not isinstance(max_size_bytes, int)
            or isinstance(max_size_bytes, bool)
            or max_size_bytes <= 0
        ):
            raise AgentFileAccessError("Invalid file size limit")
        limit = min(max_size_bytes, _MAX_FILE_BYTES)
        if selected.size_bytes > limit:
            raise AgentFileAccessError("File exceeds the size limit")
        try:
            body = self._runtime.plane.read_artifact_bytes(
                selected.artifact_id, max_size_bytes=limit
            )
        except Exception as exc:
            raise AgentFileAccessError("File is unavailable") from exc
        if (
            not isinstance(body, bytes)
            or len(body) != selected.size_bytes
            or hashlib.sha256(body).hexdigest() != selected.sha256
        ):
            raise AgentFileAccessError("File integrity check failed")
        return body

    def read_text(self, name: str, offset: int, max_chars: int) -> dict[str, Any]:
        selected = self._selected(name)
        if selected.content_type.split(";", 1)[0].lower() not in _TEXT_TYPES:
            raise AgentFileAccessError("File is not supported text")
        if (
            not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 0
            or not isinstance(max_chars, int)
            or isinstance(max_chars, bool)
            or not 1 <= max_chars <= _MAX_TEXT_CHARS
        ):
            raise AgentFileAccessError("Invalid text range")
        try:
            text = self.read_bytes(name, max_size_bytes=_MAX_FILE_BYTES).decode(
                "utf-8", errors="strict"
            )
        except UnicodeDecodeError as exc:
            raise AgentFileAccessError("File is not valid UTF-8") from exc
        if offset > len(text):
            raise AgentFileAccessError("Invalid text range")
        part = text[offset : offset + max_chars]
        if self._text_chars_read + len(part) > _MAX_TOTAL_TEXT_CHARS:
            raise AgentFileAccessError("File read limit exceeded")
        self._text_chars_read += len(part)
        end = offset + len(part)
        return {"text": part, "next_offset": end, "eof": end == len(text)}


class AgentFileRuntime:
    """File authorization bound to an independently verified durable job owner."""

    def __init__(self, *, plane: Any, account_id: str, project_id: str, job_id: str) -> None:
        self._plane = plane
        self.account_id = str(account_id or "").strip()
        self.project_id = str(project_id or "").strip()
        self.job_id = str(job_id or "").strip()

    @property
    def plane(self) -> Any:
        if callable(self._plane):
            self._plane = self._plane()
        return self._plane

    def _authorize(self, name: str, artifact_id: str) -> _AuthorizedFile:
        if not all(
            (self.plane, self.account_id, self.project_id, self.job_id)
        ) or not _ARTIFACT.fullmatch(artifact_id):
            raise AgentFileAccessError("File is unavailable")
        try:
            meta = self.plane.get_artifact(artifact_id)
            from core.application.jobs.job_artifact_access import artifact_metadata_is_downloadable

            if (
                not isinstance(meta, dict)
                or str(meta.get("artifact_id") or "").strip() != artifact_id
                or not artifact_metadata_is_downloadable(meta)
            ):
                raise AgentFileAccessError("File is unavailable")
            owner = meta.get("owner_scope")
            if not isinstance(owner, Mapping) or any(
                str(owner.get(k) or "").strip() != value
                for k, value in (("project_id", self.project_id), ("job_id", self.job_id))
            ):
                raise AgentFileAccessError("File is unavailable")
            # Webhook intake predates a project/job; normal replay adoption adds
            # those owners but not an account. The worker already derived this
            # account from that exact durable project. A present account must agree.
            owner_account = str(owner.get("account_id") or "").strip()
            if owner_account and owner_account != self.account_id:
                raise AgentFileAccessError("File is unavailable")
            expiry = meta.get("expires_at")
            if meta.get("expired") or (expiry is not None and float(expiry) <= time.time()):
                raise AgentFileAccessError("File is unavailable")
            size = meta.get("size_bytes")
            digest = str(meta.get("sha256") or "").lower()
            if (
                not isinstance(size, int)
                or isinstance(size, bool)
                or not 0 <= size <= _MAX_FILE_BYTES
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
            ):
                raise AgentFileAccessError("File is unavailable")
            return _AuthorizedFile(
                name,
                artifact_id,
                str(meta.get("filename") or "file"),
                str(meta.get("content_type") or "application/octet-stream"),
                size,
                digest,
            )
        except AgentFileAccessError:
            raise
        except Exception as exc:
            raise AgentFileAccessError("File is unavailable") from exc

    def bind_files(self, bindings: list[dict[str, str]], state: dict[str, Any]) -> AgentFileContext:
        if not isinstance(bindings, list) or len(bindings) > _MAX_FILES:
            raise AgentFileAccessError("Invalid file bindings")
        files = []
        names: set[str] = set()
        for binding in bindings:
            if not isinstance(binding, Mapping):
                raise AgentFileAccessError("Invalid file binding")
            name = str(binding.get("name") or "").strip()
            source = str(binding.get("source") or "").strip()
            if (
                not _NAME.fullmatch(name)
                or name in names
                or not source.startswith("state.")
                or len(source) <= 6
            ):
                raise AgentFileAccessError("Invalid file binding")
            names.add(name)
            value, error = resolve_expected_input_source(state, source, name)
            if error:
                raise AgentFileAccessError("File binding is unavailable")
            if isinstance(value, Mapping):
                value = value.get("artifact_handle") or value.get("artifact_id")
            value = str(value or "").strip()
            artifact_id = value.removeprefix("artifact:")
            files.append(self._authorize(name, artifact_id))
            if sum(item.size_bytes for item in files) > _MAX_TOTAL_FILE_BYTES:
                raise AgentFileAccessError("File size limit exceeded")
        return AgentFileContext(self, files)


RuntimeBinding = AgentFileRuntime | Callable[[], AgentFileRuntime | None] | None
_current_runtime: ContextVar[RuntimeBinding] = ContextVar("agent_file_runtime", default=None)


def current_agent_file_runtime() -> AgentFileRuntime | None:
    value = _current_runtime.get()
    return value() if callable(value) else value


@contextmanager
def bind_agent_file_runtime(runtime: RuntimeBinding) -> Iterator[None]:
    token = _current_runtime.set(runtime)
    try:
        yield
    finally:
        _current_runtime.reset(token)


def runtime_from_durable_job(
    *, store: Any, job: Any, job_id: str, plane_provider: Any
) -> AgentFileRuntime | None:
    """Resolve owner only from durable job and project rows, never queue payloads."""
    if (
        not isinstance(job, Mapping)
        or str(job.get("id") or "").strip() != str(job_id or "").strip()
    ):
        return None
    project_id = str(job.get("project_id") or "").strip()
    if not project_id or not callable(plane_provider):
        return None
    try:
        project = store.get_project(project_id)
        if not isinstance(project, Mapping):
            return None
        if str(project.get("id") or project.get("project_id") or "").strip() != project_id:
            return None
        account_id = str(project.get("account_id") or "").strip()
        if not account_id:
            return None
        job_account = str(job.get("account_id") or "").strip()
        if job_account and job_account != account_id:
            return None
        return AgentFileRuntime(
            plane=plane_provider, account_id=account_id, project_id=project_id, job_id=job_id
        )
    except Exception:
        return None
