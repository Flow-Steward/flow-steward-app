from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.responses import JSONResponse, RedirectResponse

from core.application.jobs.job_artifact_access import resolve_job_artifact_download
from core.application.management.errors import ServiceConflictError, ServiceNotFoundError
from core.entrypoints.api import dependencies
from core.entrypoints.api.jobs import create_api_jobs_router


def _artifact(*, project_id: str = "project-a", job_id: str = "job-a") -> dict:
    return {
        "artifact_id": "art-1",
        "owner_scope": {"project_id": project_id, "job_id": job_id},
        "filename": "catalog.csv",
        "content_type": "text/csv",
        "size_bytes": 42,
        "sha256": "a" * 64,
        "object_key": "artifacts/art-1/catalog.csv",
        "persistence_status": "available",
        "expired": False,
    }


def test_job_artifact_access_returns_owned_available_descriptor() -> None:
    download = resolve_job_artifact_download(
        job={"id": "job-a", "project_id": "project-a"},
        artifact_id="art-1",
        resolve_artifact=lambda _artifact_id: _artifact(),
    )

    assert download.artifact_id == "art-1"
    assert download.filename == "catalog.csv"
    assert download.content_type == "text/csv"
    assert download.size_bytes == 42


def test_a_row_without_a_type_leaves_the_preview_type_empty() -> None:
    """The preview falls back to the extension only when the row declares no type."""
    metadata = _artifact()
    metadata.pop("content_type")
    metadata["filename"] = "notes.txt"

    download = resolve_job_artifact_download(
        job={"id": "job-a", "project_id": "project-a"},
        artifact_id="art-1",
        resolve_artifact=lambda _artifact_id: metadata,
    )

    assert download.preview_content_type == ""
    assert download.content_type == "application/octet-stream"


def test_job_artifact_access_accepts_recorded_child_producer_after_parent_adoption() -> None:
    metadata = _artifact(project_id="project-a", job_id="parent-a")
    metadata["producer_job_id"] = "child-a"

    download = resolve_job_artifact_download(
        job={"id": "child-a", "project_id": "project-a"},
        artifact_id="art-1",
        resolve_artifact=lambda _artifact_id: metadata,
    )

    assert download.artifact_id == "art-1"


def test_job_artifact_access_accepts_complete_legacy_metadata_without_status() -> None:
    metadata = _artifact()
    metadata.pop("persistence_status")

    download = resolve_job_artifact_download(
        job={"id": "job-a", "project_id": "project-a"},
        artifact_id="art-1",
        resolve_artifact=lambda _artifact_id: metadata,
    )

    assert download.artifact_id == "art-1"


@pytest.mark.parametrize(
    ("project_id", "job_id"),
    [("project-b", "job-a"), ("project-a", "job-b")],
)
def test_job_artifact_access_hides_foreign_owner_scope(project_id: str, job_id: str) -> None:
    with pytest.raises(ServiceNotFoundError):
        resolve_job_artifact_download(
            job={"id": "job-a", "project_id": "project-a"},
            artifact_id="art-1",
            resolve_artifact=lambda _artifact_id: _artifact(
                project_id=project_id,
                job_id=job_id,
            ),
        )


def test_job_artifact_access_rejects_incomplete_object() -> None:
    metadata = _artifact()
    metadata["persistence_status"] = "preparing"

    with pytest.raises(ServiceConflictError):
        resolve_job_artifact_download(
            job={"id": "job-a", "project_id": "project-a"},
            artifact_id="art-1",
            resolve_artifact=lambda _artifact_id: metadata,
        )


def test_job_artifact_access_rejects_incomplete_legacy_metadata_without_checksum() -> None:
    metadata = _artifact()
    metadata.pop("persistence_status")
    metadata["sha256"] = ""

    with pytest.raises(ServiceConflictError):
        resolve_job_artifact_download(
            job={"id": "job-a", "project_id": "project-a"},
            artifact_id="art-1",
            resolve_artifact=lambda _artifact_id: metadata,
        )


def test_job_artifact_route_redirects_to_signed_storage_without_reading_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query_service = SimpleNamespace(
        get_job_artifact_download=lambda *_args, **_kwargs: SimpleNamespace(
            artifact_id="art-1",
            filename="catalog.csv",
            content_type="text/csv",
            size_bytes=42,
        )
    )
    monkeypatch.setattr(
        dependencies,
        "_artifact_presigned_url",
        lambda artifact_id, *, method, ttl_seconds, filename: (
            f"https://storage.example/{artifact_id}?method={method}"
            f"&ttl={ttl_seconds}&filename={filename}"
        ),
    )
    router = create_api_jobs_router(
        resolve_user_role=lambda _request: "operator",
        job_query_service=lambda: query_service,
        job_command_service=lambda: SimpleNamespace(),
        approval_query_service=lambda: SimpleNamespace(),
        request_scope=lambda _request, **_kwargs: None,
        resolve_actor_username=lambda *_args, **_kwargs: "operator",
        optional_actor_username=lambda *_args, **_kwargs: "operator",
        service_error_response=lambda exc: JSONResponse(
            {"error": str(exc)}, status_code=getattr(exc, "status_code", 500)
        ),
        api_error_response=lambda exc, **_kwargs: JSONResponse(
            {"error": str(exc)}, status_code=500
        ),
        get_logger_only=lambda: None,
        watchdog_scan=lambda *_args, **_kwargs: {},
        parse_worker_pid=lambda *_args, **_kwargs: None,
        terminate_worker_pid=lambda *_args, **_kwargs: {},
        append_runtime_journal=lambda *_args, **_kwargs: {},
        build_rerun_workflow_selection=lambda *_args, **_kwargs: ({}, {}),
        load_replay_snapshot=lambda *_args, **_kwargs: {},
        enqueue_task=lambda *_args, **_kwargs: 1,
        retryable_interrupted_step_status="retryable_interrupted_step",
        err_database_not_configured="Database unavailable",
    )
    endpoint = next(
        route.endpoint
        for route in router.routes
        if getattr(route, "path", "") == "/api/jobs/{job_id}/artifacts/{artifact_id}"
    )

    response = asyncio.run(endpoint(SimpleNamespace(), "job-a", "art-1"))

    assert isinstance(response, RedirectResponse)
    assert response.status_code == 307
    assert response.headers["location"] == (
        "https://storage.example/art-1?method=GET&ttl=600&filename=catalog.csv"
    )


def _preview_endpoint(
    content_type: str,
    *,
    size_bytes: int = 8,
    preview_content_type=None,
    filename: str = "cover image.png",
):
    query_service = SimpleNamespace(
        get_job_artifact_download=lambda *_args, **_kwargs: SimpleNamespace(
            artifact_id="art-1",
            filename=filename,
            content_type=content_type,
            preview_content_type=preview_content_type,
            size_bytes=size_bytes,
        )
    )
    router = create_api_jobs_router(
        resolve_user_role=lambda _request: "operator",
        job_query_service=lambda: query_service,
        job_command_service=lambda: SimpleNamespace(),
        approval_query_service=lambda: SimpleNamespace(),
        request_scope=lambda _request, **_kwargs: None,
        resolve_actor_username=lambda *_args, **_kwargs: "operator",
        optional_actor_username=lambda *_args, **_kwargs: "operator",
        service_error_response=lambda exc: JSONResponse(
            {"error": str(exc)}, status_code=getattr(exc, "status_code", 500)
        ),
        api_error_response=lambda exc, **_kwargs: JSONResponse(
            {"error": str(exc)}, status_code=500
        ),
        get_logger_only=lambda: None,
        watchdog_scan=lambda *_args, **_kwargs: {},
        parse_worker_pid=lambda *_args, **_kwargs: None,
        terminate_worker_pid=lambda *_args, **_kwargs: {},
        append_runtime_journal=lambda *_args, **_kwargs: {},
        build_rerun_workflow_selection=lambda *_args, **_kwargs: ({}, {}),
        load_replay_snapshot=lambda *_args, **_kwargs: {},
        enqueue_task=lambda *_args, **_kwargs: 1,
        retryable_interrupted_step_status="retryable_interrupted_step",
        err_database_not_configured="Database unavailable",
    )
    return next(
        route.endpoint
        for route in router.routes
        if getattr(route, "path", "") == "/api/jobs/{job_id}/artifacts/{artifact_id}/preview"
    )


def test_job_artifact_preview_serves_an_image_inline_from_any_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads: list[tuple[str, int]] = []
    monkeypatch.setattr(
        dependencies,
        "_artifact_read_bytes",
        lambda artifact_id, *, max_size_bytes: (
            reads.append((artifact_id, max_size_bytes)) or b"\x89PNG...."
        ),
    )

    response = asyncio.run(_preview_endpoint("image/png")(SimpleNamespace(), "job-a", "art-1"))

    assert response.status_code == 200
    assert response.body == b"\x89PNG...."
    assert response.media_type == "image/png"
    assert response.headers["content-disposition"] == ("inline; filename*=UTF-8''cover%20image.png")
    assert response.headers["content-security-policy"].startswith("sandbox;")
    assert reads == [("art-1", 20 * 1024 * 1024)]


def test_job_artifact_preview_opens_a_pdf_inline_without_a_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dependencies, "_artifact_read_bytes", lambda _artifact_id, *, max_size_bytes: b"%PDF-1.7"
    )

    response = asyncio.run(
        _preview_endpoint("application/pdf")(SimpleNamespace(), "job-a", "art-1")
    )

    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("inline;")
    assert "content-security-policy" not in response.headers


@pytest.mark.parametrize(
    ("content_type", "size_bytes", "status"),
    [
        ("image/svg+xml", 8, 415),
        ("text/html", 8, 415),
        ("text/csv", 8, 415),
        ("image/png", 20 * 1024 * 1024 + 1, 413),
    ],
)
def test_job_artifact_preview_refuses_scriptable_and_oversized_files_without_reading_them(
    monkeypatch: pytest.MonkeyPatch, content_type: str, size_bytes: int, status: int
) -> None:
    def must_not_read(*_args, **_kwargs):
        raise AssertionError("the body must not be read")

    monkeypatch.setattr(dependencies, "_artifact_read_bytes", must_not_read)

    response = asyncio.run(
        _preview_endpoint(content_type, size_bytes=size_bytes)(SimpleNamespace(), "job-a", "art-1")
    )

    assert response.status_code == status


def test_job_artifact_preview_limit_comes_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FS_FILE_PREVIEW_MAX_BYTES", "4096")
    monkeypatch.setattr(dependencies, "_artifact_read_bytes", lambda *_args, **_kwargs: b"\x89PNG")

    too_big = asyncio.run(
        _preview_endpoint("image/png", size_bytes=4097)(SimpleNamespace(), "job-a", "art-1")
    )
    fits = asyncio.run(
        _preview_endpoint("image/png", size_bytes=4096)(SimpleNamespace(), "job-a", "art-1")
    )

    assert too_big.status_code == 413
    assert fits.status_code == 200


def test_presentation_mime_cannot_activate_an_inline_preview(monkeypatch):
    monkeypatch.setattr(
        dependencies,
        "artifact_read_bytes",
        lambda *_args, **_kwargs: pytest.fail("presentation cannot authorize a preview"),
    )
    endpoint = _preview_endpoint("application/pdf", preview_content_type="application/octet-stream")
    response = asyncio.run(endpoint(SimpleNamespace(), "job-a", "art-1"))
    assert response.status_code == 415


@pytest.mark.parametrize(
    ("content_type", "filename"),
    [
        ("text/plain", "notes.txt"),
        ("text/plain; charset=utf-8", "notes"),
        ("text/markdown", "README.md"),
        ("application/json; charset=UTF-8", "result.json"),
        ("", "notes.txt"),
        ("", "README.MD"),
        ("", "result.json"),
    ],
)
def test_job_artifact_preview_serves_text_as_plain_characters(
    monkeypatch: pytest.MonkeyPatch, content_type: str, filename: str
) -> None:
    """TXT, Markdown and JSON open in the run view as text, never as a page."""
    body = '# Título\n<script>alert(1)</script>\n{"a": 1}\n'.encode()
    monkeypatch.setattr(
        dependencies, "_artifact_read_bytes", lambda _artifact_id, *, max_size_bytes: body
    )

    response = asyncio.run(
        _preview_endpoint(content_type, filename=filename, size_bytes=len(body))(
            SimpleNamespace(), "job-a", "art-1"
        )
    )

    assert response.status_code == 200
    assert response.body == body
    assert response.media_type == "text/plain; charset=utf-8"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"].startswith("sandbox;")
    assert response.headers["content-disposition"].startswith("inline;")


def test_a_declared_type_is_not_overridden_by_a_text_extension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dependencies,
        "_artifact_read_bytes",
        lambda *_args, **_kwargs: pytest.fail("a declared non-text type is not previewed"),
    )

    for declared in ("application/octet-stream", "application/zip", "text/html"):
        response = asyncio.run(
            _preview_endpoint(declared, filename="notes.txt")(SimpleNamespace(), "job-a", "art-1")
        )
        assert response.status_code == 415, declared


def test_text_that_is_not_utf8_is_refused_with_a_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        dependencies, "_artifact_read_bytes", lambda *_args, **_kwargs: "café".encode("latin-1")
    )

    response = asyncio.run(
        _preview_endpoint("text/plain", filename="notes.txt")(SimpleNamespace(), "job-a", "art-1")
    )

    assert response.status_code == 422
    assert "UTF-8" in response.body.decode()


def test_text_over_the_preview_limit_is_refused_without_reading_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FS_FILE_PREVIEW_MAX_BYTES", "4096")
    monkeypatch.setattr(
        dependencies,
        "_artifact_read_bytes",
        lambda *_args, **_kwargs: pytest.fail("an oversized file must not be read"),
    )

    response = asyncio.run(
        _preview_endpoint("application/json", filename="big.json", size_bytes=4097)(
            SimpleNamespace(), "job-a", "art-1"
        )
    )

    assert response.status_code == 413
