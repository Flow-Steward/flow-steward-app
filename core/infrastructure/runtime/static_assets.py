"""Static assets that fail as a server fault when the server cannot read them.

R144. Starlette's StaticFiles answers a PermissionError with 401. On Scalable the UI's
component scripts were unreadable to the service's uid -- a build-context permission
fault, not an authentication one -- and every request for them came back 401, which
reads as "you are not signed in" and sends the operator to look at credentials. An
unreadable file shipped in the image is the server's fault, so it fails as one: a 500,
logged with the path, from the application's own error handling.
"""

from __future__ import annotations

import os

from starlette.staticfiles import StaticFiles


class StaticAssetUnreadable(RuntimeError):
    """A static asset exists but the server process is not permitted to read it."""


class ServerFaultStaticFiles(StaticFiles):
    def lookup_path(self, path: str) -> tuple[str, os.stat_result | None]:
        try:
            return super().lookup_path(path)
        except PermissionError as exc:
            # Raised as something StaticFiles.get_response does not catch: every
            # OSError there becomes 401 or 404, and neither is true of this.
            raise StaticAssetUnreadable(
                f"static asset {path!r} is not readable by this process (uid {os.getuid()})"
            ) from exc


__all__ = ["ServerFaultStaticFiles", "StaticAssetUnreadable"]
