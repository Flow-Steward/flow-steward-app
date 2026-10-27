"""Attachment download shared by the downloadable diagnostic reports.

The browser starts the download with a one-time token; the response sets a
short-lived cookie named after that token, so the page can tell the operator
that the download started while the report is still being rendered.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Callable

from fastapi import Request
from fastapi.responses import StreamingResponse

REPORT_DOWNLOAD_TOKEN_PATTERN = r"^[A-Za-z0-9_-]{16,64}$"
REPORT_DOWNLOAD_COOKIE_PREFIX = "fs_job_report_download_"


def valid_report_download_token(token: str | None) -> bool:
    return bool(token) and re.fullmatch(REPORT_DOWNLOAD_TOKEN_PATTERN, str(token)) is not None


def report_download_response(
    request: Request,
    *,
    filename: str,
    download_token: str,
    render: Callable[[], bytes],
) -> StreamingResponse:
    """Stream ``render()`` as a JSON attachment and confirm the start with a cookie."""

    async def _chunks() -> AsyncIterator[bytes]:
        # A leading JSON whitespace byte flushes the attachment headers
        # immediately, so the browser can confirm that the download started
        # while the complete redacted report is rendered off-loop.
        yield b" "
        rendered = await asyncio.to_thread(render)
        chunk_size = 1024 * 1024
        for offset in range(0, len(rendered), chunk_size):
            yield rendered[offset : offset + chunk_size]

    response = StreamingResponse(
        _chunks(),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
    forwarded_proto = str(request.headers.get("x-forwarded-proto", ""))
    forwarded_proto = forwarded_proto.split(",", 1)[0].strip().lower()
    request_scheme = str(getattr(getattr(request, "url", None), "scheme", ""))
    # Not HttpOnly on purpose: the page reads this cookie to see that the download
    # started. It carries no secret, only "1", and lives for two minutes.
    response.set_cookie(  # NOSONAR
        key=f"{REPORT_DOWNLOAD_COOKIE_PREFIX}{download_token}",
        value="1",
        max_age=120,
        path="/",
        secure=forwarded_proto == "https" or request_scheme == "https",
        samesite="lax",
    )
    return response


__all__ = [
    "REPORT_DOWNLOAD_TOKEN_PATTERN",
    "report_download_response",
    "valid_report_download_token",
]
