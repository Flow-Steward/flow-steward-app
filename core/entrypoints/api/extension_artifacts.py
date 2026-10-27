"""The platform serving artifact bodies to extensions its storage cannot presign.

AC17-R123. An extension subprocess reaches an artifact through a URL the host gives
it. Object storage mints that URL; a filesystem backend has none to mint, so the
platform serves the object itself and these are the two ends of that.

Authorization is the token in the request header and nothing else. There is no
session here by design -- the subprocess environment is an allowlist carrying no
credential -- so every check is on the token: it names one object, one kind and one
method, it expires, and it was signed by this deployment. Nothing about the caller is
inferred from the network it arrives on.

The read half hands back the same stream the browser download already uses for this
case. The write half puts the body under the row's own key and runs the existing
finalization, so the checksum and status come from the code that owns that question
rather than from a second implementation living here.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from core.application.extensions.runtime_grants import PLATFORM_SERVED_ARTIFACT_PATH
from core.infrastructure.security.artifact_access_tokens import (
    HEADER_NAME,
    ArtifactAccessTokenError,
    verify_artifact_access_token,
)

# Concatenated rather than interpolated: in an f-string the two real path
# parameters have to be written as doubled braces, which reads as though the
# prefix were a third one.
_ROUTE = PLATFORM_SERVED_ARTIFACT_PATH + "/{kind}/{object_id}"
_MAX_UPLOAD_BYTES = 100 * 1024 * 1024
_JSON = "application/json"
# The one answer every refusal gives, whatever it was that did not check out.
_REFUSED = "Artifact access token is not valid for this request"


def _refused(detail: str) -> Response:
    # One status and one shape for every refusal. A caller holding a token learns
    # whether it worked, never why it did not: separating "expired" from "wrong
    # object" tells an attacker which half to vary.
    return Response(
        content=f'{{"error":"{detail}"}}',
        status_code=403,
        media_type=_JSON,
    )


def create_extension_artifacts_router(*, artifact_plane: Any, logger: Any) -> APIRouter:
    router = APIRouter(tags=["extension-artifacts"])

    def _authorized(request: Request, *, kind: str, object_id: str, method: str) -> bool:
        try:
            verify_artifact_access_token(
                str(request.headers.get(HEADER_NAME) or ""),
                object_id=object_id,
                kind=kind,
                method=method,
            )
        except ArtifactAccessTokenError:
            return False
        return True

    @router.get(_ROUTE, operation_id="get_extension_artifact_content")
    async def get_extension_artifact_content(request: Request, kind: str, object_id: str):
        if not _authorized(request, kind=kind, object_id=object_id, method="GET"):
            return _refused(_REFUSED)
        if kind != "artifact":
            return _refused(_REFUSED)
        try:
            # Opening the object reads its first chunk -- a network round trip on S3 --
            # so it runs off the event loop, like the write below.
            stream = await run_in_threadpool(artifact_plane.artifact_local_stream, object_id)
        except Exception:
            # The token was accepted three lines ago, so nothing after this point is
            # an authorization failure. Answering 403 here would send an operator to
            # look at credentials for what is a storage fault.
            logger.warning("Extension artifact read failed", exc_info=True)
            stream = None
        if stream is None:
            # Authorized and unservable are different answers, and this one is not the
            # caller's fault: the token was good and the body is not there.
            return Response(
                content='{"error":"Artifact content is not available"}',
                status_code=503,
                media_type=_JSON,
            )
        chunks, size_bytes = stream
        return StreamingResponse(
            _iterate(chunks),
            media_type="application/octet-stream",
            headers={"Content-Length": str(size_bytes)},
        )

    @router.put(_ROUTE, operation_id="put_extension_artifact_content")
    async def put_extension_artifact_content(request: Request, kind: str, object_id: str):
        if not _authorized(request, kind=kind, object_id=object_id, method="PUT"):
            return _refused(_REFUSED)
        if kind != "artifact":
            return _refused(_REFUSED)
        body = await _bounded_body(request)
        if body is None:
            return Response(
                content='{"error":"Artifact upload is too large"}',
                status_code=413,
                media_type=_JSON,
            )
        try:
            # P2: storage I/O is synchronous; run inline it stalled every other request.
            written = await run_in_threadpool(
                artifact_plane.write_artifact_content,
                object_id,
                body=body,
                content_type=str(request.headers.get("content-type") or ""),
            )
        except ValueError:
            # An artifact body is written once; a second attempt with a live token is
            # refused here rather than trusted to the extension's own bookkeeping.
            return _refused(_REFUSED)
        except Exception:
            logger.warning("Extension artifact write failed", exc_info=True)
            return Response(
                content='{"error":"Artifact upload could not be stored"}',
                status_code=503,
                media_type=_JSON,
            )
        if written is None:
            return Response(
                content='{"error":"Artifact upload could not be stored"}',
                status_code=503,
                media_type=_JSON,
            )
        return Response(status_code=204)

    return router


async def _bounded_body(request: Request) -> bytes | None:
    """The request body, or None as soon as it is known to exceed the limit.

    P2: the body was read in full and measured afterwards, so a client could make the
    server hold any amount before hearing 413. A declared length over the limit is
    refused before anything is read; an undeclared or understated one is refused at
    the chunk that crosses it.
    """
    declared = str(request.headers.get("content-length") or "").strip()
    if declared.isdigit() and int(declared) > _MAX_UPLOAD_BYTES:
        return None
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > _MAX_UPLOAD_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _iterate(chunks: Iterator[bytes]) -> Iterator[bytes]:
    yield from chunks


__all__ = ["create_extension_artifacts_router"]
