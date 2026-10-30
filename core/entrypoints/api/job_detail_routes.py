"""Scoped job detail, report, event and artifact HTTP routes."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse

from core.application.management import ServiceError, ServiceUnavailableError
from core.entrypoints.api import dependencies as api_dependencies
from core.entrypoints.api.report_download import (
    REPORT_DOWNLOAD_TOKEN_PATTERN,
    report_download_response,
    valid_report_download_token,
)
from core.infrastructure.config.workflow_file_limits import file_preview_max_bytes


def _job_report_json(payload: dict[str, Any]) -> bytes:
    """Serialize an already-redacted report with the download's native scalar policy."""
    return json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":")).encode(
        "utf-8"
    )


def _artifact_download_headers(download: Any, size_bytes: int) -> dict[str, str]:
    """Attachment headers for a streamed artifact, from validated metadata only.

    The filename is percent-encoded into the RFC 5987 form with an empty safe
    set, so a name carrying quotes, newlines, non-ASCII or path separators
    cannot break out of the header or suggest a directory to the browser —
    ``quote`` leaves ``/`` alone by default, which would let ``a/b.csv`` or
    ``../b.csv`` through intact.

    ``size_bytes`` is the length of the body about to be streamed, not the
    length the metadata row remembers: declaring the second would promise a
    client bytes this response is not going to send.
    """
    encoded_filename = quote(download.filename, safe="")
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}",
    }
    if size_bytes > 0:
        headers["Content-Length"] = str(size_bytes)
    return headers


#: Types a browser renders inline without running anything. SVG is left out:
#: it is a document that can carry script.
_INLINE_PREVIEW_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf"}
)
# Text a run view reads as plain characters. Served as text/plain whatever the
# declared type, so neither Markdown nor JSON is ever interpreted by the browser.
_TEXT_PREVIEW_TYPES = frozenset({"text/plain", "text/markdown", "application/json"})
_TEXT_PREVIEW_EXTENSIONS = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".json": "application/json",
}
_PREVIEW_TOO_LARGE = "The file is too large to preview"


def _preview_media_type(declared: str, filename: str) -> str:
    """The type a preview is chosen by: the declared one, or the extension when none is.

    A declared type always wins, so a file recorded as, say, application/zip is not
    previewed as text because its name ends in .txt. Only text types are inferred.
    """
    media_type = declared.split(";", 1)[0].strip().lower()
    if media_type:
        return media_type
    return _TEXT_PREVIEW_EXTENSIONS.get(PurePosixPath(filename).suffix.lower(), "")


def _text_preview_response(body: bytes, filename: str) -> Response:
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        return JSONResponse(
            {"error": "The file is not UTF-8 text and cannot be previewed; download it instead"},
            status_code=422,
        )
    return Response(
        content=text.encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f"inline; filename*=UTF-8''{quote(filename, safe='')}",
            "Cache-Control": "private, max-age=300",
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "X-Content-Type-Options": "nosniff",
        },
    )


@dataclass(frozen=True)
class JobDetailRouteDependencies:
    """Existing scoped services and response adapters used by job detail routes."""

    api_error_response: Callable[..., JSONResponse]
    err_database_not_configured: str
    get_logger_only: Callable[[], Any]
    job_command_service: Callable[[], Any]
    job_query_service: Callable[[], Any]
    request_scope: Callable[..., Any]
    service_error_response: Callable[..., JSONResponse]


def register_job_detail_routes(router: APIRouter, dependencies: JobDetailRouteDependencies) -> None:
    """Attach detail, report and artifact routes in their existing router position."""
    api_error_response = dependencies.api_error_response
    err_database_not_configured = dependencies.err_database_not_configured
    get_logger_only = dependencies.get_logger_only
    job_command_service = dependencies.job_command_service
    job_query_service = dependencies.job_query_service
    request_scope = dependencies.request_scope
    service_error_response = dependencies.service_error_response

    @router.get("/api/jobs/{job_id}/report")
    async def api_job_diagnostic_report(
        request: Request,
        job_id: str,
        download_token: Annotated[
            str | None,
            Query(min_length=16, max_length=64, pattern=REPORT_DOWNLOAD_TOKEN_PATTERN),
        ] = None,
    ):
        """Download the complete redacted persisted diagnostic report."""
        filename = f"flow-steward-job-{quote(str(job_id or 'report'))}-report.json"
        if download_token and valid_report_download_token(download_token):
            # Access is checked before the attachment starts streaming: once the
            # headers are flushed, an authorization failure can no longer be an error.
            try:
                query_service = job_query_service()
                scope = request_scope(request)
                await asyncio.to_thread(
                    query_service.validate_job_diagnostic_report_access,
                    job_id,
                    scope=scope,
                )
            except ServiceError as e:
                return service_error_response(e)
            except Exception as e:
                return api_error_response(e)

            def _render_report() -> bytes:
                payload = asyncio.run(
                    query_service.get_job_diagnostic_report(
                        job_id,
                        scope=scope,
                    )
                )
                return _job_report_json(payload)

            return report_download_response(
                request,
                filename=filename,
                download_token=download_token,
                render=_render_report,
            )

        try:
            payload = await job_query_service().get_job_diagnostic_report(
                job_id,
                scope=request_scope(request),
            )
            return Response(
                _job_report_json(payload),
                media_type="application/json",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)

    @router.get("/api/jobs/{job_id}/result")
    async def api_job_result(
        request: Request,
        job_id: str,
        include_messages_limit: Annotated[int, Query(ge=1, le=2000)] = 100,
        include_logs_limit: Annotated[int, Query(ge=1, le=2000)] = 50,
    ):
        """Return the canonical agent handoff bundle for one scoped job."""
        from core.application.reports.job_result_bundle import build_job_result_bundle

        try:
            return await build_job_result_bundle(
                job_query_service=job_query_service(),
                job_id=job_id,
                scope=request_scope(request),
                include_messages_limit=include_messages_limit,
                include_logs_limit=include_logs_limit,
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)

    @router.get("/api/jobs/{job_id}/trace")
    async def api_job_trace(
        request: Request,
        job_id: str,
        limit: Annotated[int, Query()] = 500,
    ):
        """Return one scoped trace projection for a job."""
        try:
            payload = job_query_service().get_trace(
                job_id,
                limit=limit,
                scope=request_scope(request),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return JSONResponse(payload.dump())

    @router.get("/api/jobs/{job_id}/incident")
    async def api_job_incident_drilldown(
        request: Request, job_id: str, limit: Annotated[int, Query(ge=1, le=200)] = 100
    ):
        """Return one-shot incident drill-down payload to locate root cause fast."""
        try:
            payload = job_query_service().get_incident_drilldown(
                job_id,
                limit=limit,
                scope=request_scope(request),
            )
        except ServiceError as e:
            return service_error_response(e, request)
        except Exception as e:
            return api_error_response(e, request=request)
        return JSONResponse(payload.dump())

    @router.delete("/api/jobs/{job_id}")
    async def api_job_delete(request: Request, job_id: str):
        """Trash (soft-delete) one job. Logs and observability data are preserved."""
        try:
            payload = job_command_service().delete_job(
                job_id,
                scope=request_scope(request),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return JSONResponse({"status": payload.status, "job_id": payload.job_id})

    @router.get("/api/jobs/{job_id}/cost-breakdown")
    async def api_job_cost_breakdown(request: Request, job_id: str):
        """Return per-agent, per-model cost breakdown for a job."""
        try:
            payload = job_query_service().get_cost_breakdown(
                job_id,
                scope=request_scope(request),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return JSONResponse(payload.dump())

    @router.get("/api/jobs/{job_id}/events")
    async def api_job_events(
        request: Request,
        job_id: str,
        since_id: str = "",
        limit: Annotated[int, Query(ge=1, le=2000)] = 200,
    ):
        """Return incremental semantic events for a job. Supports opaque ?since_id cursors."""
        try:
            payload = job_query_service().list_events(
                job_id,
                since_id=since_id,
                limit=limit,
                scope=request_scope(request),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return JSONResponse(payload.dump())

    @router.get("/api/jobs/{job_id}/messages")
    async def api_job_messages(
        request: Request,
        job_id: str,
        since_id: str = "",
        before_id: str = "",
        newest: bool = False,
        step_id: str = "",
        producer: str = "",
        skill: str = "",
        status: str = "",
        limit: int = 25,
    ):
        """Return AgentMessageV1 rows for a job with incremental + attribute filters."""
        try:
            payload = job_query_service().list_messages(
                job_id,
                since_id=since_id,
                before_id=before_id,
                newest=newest,
                step_id=step_id,
                producer=producer,
                skill=skill,
                status=status,
                limit=limit,
                scope=request_scope(request),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return JSONResponse(payload.dump())

    @router.get("/api/jobs/{job_id}/messages/{message_id}/output-value")
    async def api_job_step_output_value(
        request: Request, job_id: str, message_id: str, field: str = ""
    ):
        """Return the full value of one step output that Job Details shows cut short."""
        try:
            payload = job_query_service().get_step_output_value(
                job_id,
                message_id,
                field,
                scope=request_scope(request),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return JSONResponse(payload)

    @router.get("/api/jobs/{job_id}/attachments/{attachment_id}")
    async def api_job_attachment_download(request: Request, job_id: str, attachment_id: str):
        """Download a persisted job attachment after normal job read authorization."""
        try:
            await job_query_service().get_job_status(
                job_id,
                scope=request_scope(request),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)

        bl = get_logger_only()
        if not bl or not hasattr(bl, "get_attachments"):
            return JSONResponse({"error": err_database_not_configured}, status_code=503)

        try:
            attachments = bl.get_attachments(job_id)
        except Exception as e:
            return api_error_response(e)

        selected = None
        normalized_attachment_id = str(attachment_id or "").strip()
        for attachment in attachments or []:
            row_id = str((attachment or {}).get("id") or "").strip()
            if row_id == normalized_attachment_id:
                selected = dict(attachment)
                break
        if selected is None:
            return JSONResponse({"error": "Attachment not found"}, status_code=404)

        content = selected.get("content")
        if content is None:
            return JSONResponse({"error": "Attachment content is unavailable"}, status_code=404)
        if not isinstance(content, bytes):
            content = bytes(content)

        filename = str(selected.get("filename") or "attachment.bin").strip() or "attachment.bin"
        media_type = (
            str(selected.get("mime_type") or "application/octet-stream").strip()
            or "application/octet-stream"
        )
        disposition = f"attachment; filename*=UTF-8''{quote(filename)}"
        return Response(
            content=content,
            media_type=media_type,
            headers={"Content-Disposition": disposition},
        )

    @router.get("/api/jobs/{job_id}/artifacts/{artifact_id}")
    async def api_job_artifact_download(request: Request, job_id: str, artifact_id: str):
        """Authorize an artifact, then redirect to storage or stream it back.

        Authorization always runs first. A backend that can grant a browser URL
        keeps the redirect, so S3 downloads never travel through this process.
        ``local_fs`` has no such URL, and streaming the authorized object is the
        only way its artifacts can be downloaded at all.
        """
        try:
            download = job_query_service().get_job_artifact_download(
                job_id,
                artifact_id,
                scope=request_scope(request),
            )
            signed_url = api_dependencies.artifact_presigned_url(
                download.artifact_id,
                method="GET",
                ttl_seconds=600,
                filename=download.filename,
            )
            if not signed_url:
                local_stream = api_dependencies.artifact_local_stream(download.artifact_id)
                if local_stream is None:
                    raise ServiceUnavailableError("Artifact download is not available")
                chunks, size_bytes = local_stream
                return StreamingResponse(
                    chunks,
                    media_type=download.content_type,
                    headers=_artifact_download_headers(download, size_bytes),
                )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return RedirectResponse(url=signed_url, status_code=307)

    @router.get("/api/jobs/{job_id}/artifacts/{artifact_id}/preview")
    async def api_job_artifact_preview(request: Request, job_id: str, artifact_id: str):
        """Serve a generated image, PDF or text file inline, so the run view can show it.

        Same authorization as a download. Only types a browser renders without
        running anything are served inline (no SVG, no HTML), within a size
        bound (``FS_FILE_PREVIEW_MAX_BYTES``), and from any storage backend: the body is read here rather than
        redirected, because a storage URL is another origin the page's image
        policy does not allow. TXT, Markdown and JSON come back as UTF-8
        ``text/plain`` for the page to show as characters.
        """
        try:
            download = job_query_service().get_job_artifact_download(
                job_id,
                artifact_id,
                scope=request_scope(request),
            )
            preview_type = getattr(download, "preview_content_type", None)
            content_type = _preview_media_type(
                str(download.content_type if preview_type is None else preview_type),
                download.filename,
            )
            if content_type not in _INLINE_PREVIEW_TYPES | _TEXT_PREVIEW_TYPES:
                return JSONResponse({"error": "This file type has no preview"}, status_code=415)
            preview_limit = file_preview_max_bytes()
            if download.size_bytes > preview_limit:
                return JSONResponse({"error": _PREVIEW_TOO_LARGE}, status_code=413)
            body = await asyncio.to_thread(
                api_dependencies.artifact_read_bytes,
                download.artifact_id,
                max_size_bytes=preview_limit,
            )
            if body is None:
                raise ServiceUnavailableError("Artifact preview is not available")
        except ValueError:
            return JSONResponse({"error": _PREVIEW_TOO_LARGE}, status_code=413)
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        if content_type in _TEXT_PREVIEW_TYPES:
            return _text_preview_response(body, download.filename)
        encoded_filename = quote(download.filename, safe="")
        headers = {
            "Content-Disposition": f"inline; filename*=UTF-8''{encoded_filename}",
            "Cache-Control": "private, max-age=300",
        }
        if content_type.startswith("image/"):
            # An image opened on its own gets no script and no origin. A PDF
            # keeps the app's policy: Chrome will not render one in a sandbox.
            headers["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src 'self'"
        return Response(content=body, media_type=content_type, headers=headers)
