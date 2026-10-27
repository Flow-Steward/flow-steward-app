"""Strict, routed HTTP probes; also a one-shot stdlib-only Docker probe helper."""

from __future__ import annotations

import base64
import http.client
import io
import json
import signal
import socket
import ssl
import sys
from urllib import parse as urlparse
from urllib import request as urlrequest

MAX_PUBLIC_PROBE_BODY = 512 * 1024
MAX_BOOTSTRAP_PROBE_BODY = 2 * 1024 * 1024


def public_probe_body_limit(url: str) -> int:
    """Bound the larger bootstrap schema projection independently of health."""
    if urlparse.urlsplit(url).path == "/api/app/bootstrap":
        return MAX_BOOTSTRAP_PROBE_BODY
    return MAX_PUBLIC_PROBE_BODY


class BufferedPublicResponse(io.BytesIO):
    """A bounded HTTP result returned across the disposable process boundary."""

    def __init__(self, status: int, body: bytes) -> None:
        super().__init__(body)
        self.status = status


class _RoutedHTTPConnection(http.client.HTTPConnection):
    """Dial a bootstrap-only host route while preserving the request authority."""

    _dial_host = ""
    _dial_port = 0

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._dial_host, self._dial_port),
            self.timeout,
            self.source_address,  # type: ignore[attr-defined]
        )


class _RoutedHTTPSConnection(http.client.HTTPSConnection):
    """Dial a bootstrap-only host route without weakening TLS identity checks."""

    _dial_host = ""
    _dial_port = 0
    _server_hostname: str | None = None

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self._dial_host, self._dial_port),
            self.timeout,
            self.source_address,  # type: ignore[attr-defined]
        )
        try:
            self.sock = self._context.wrap_socket(  # type: ignore[attr-defined]
                raw_socket,
                server_hostname=self._server_hostname or self.host,
            )
        except Exception:
            raw_socket.close()
            raise


def _routed_urlopen(
    request: urlrequest.Request,
    *,
    dial_host: str,
    dial_port: int,
    server_hostname: str | None,
    timeout: int,
    context: ssl.SSLContext | None,
) -> http.client.HTTPResponse:
    """Open an installer probe through an exact dial route."""
    parsed = urlparse.urlsplit(request.full_url)
    request_host = parsed.hostname
    if not request_host:
        raise OSError("probe URL has no host")

    if parsed.scheme == "https":
        if context is None or server_hostname is None:
            raise OSError("HTTPS probe requires a TLS context and server hostname")
        routed_host = dial_host
        routed_port = dial_port
        tls_server_hostname = server_hostname

        class HTTPSRoutedConnection(_RoutedHTTPSConnection):
            _dial_host = routed_host
            _dial_port = routed_port
            _server_hostname = tls_server_hostname

        connection: http.client.HTTPConnection = HTTPSRoutedConnection(
            request_host, port=parsed.port, timeout=timeout, context=context
        )
    elif parsed.scheme == "http":
        routed_host = dial_host
        routed_port = dial_port

        class HTTPRoutedConnection(_RoutedHTTPConnection):
            _dial_host = routed_host
            _dial_port = routed_port

        connection = HTTPRoutedConnection(request_host, port=parsed.port, timeout=timeout)
    else:
        raise OSError(f"unsupported probe URL scheme: {parsed.scheme}")

    target = parsed.path or "/"
    if parsed.query:
        target = f"{target}?{parsed.query}"
    try:
        connection.request(request.get_method(), target, headers=dict(request.header_items()))
        return connection.getresponse()
    except http.client.HTTPException as exc:
        connection.close()
        raise OSError(str(exc)) from exc
    except Exception:
        connection.close()
        raise


def _probe_once() -> dict[str, object]:
    """Read public trust data from stdin; emit only the bounded HTTP result."""
    data = json.loads(sys.stdin.read(1024 * 1024))
    context = None
    if data["server_hostname"] is not None:
        context = ssl.create_default_context(cadata=data["ca_certificates"])
    request = urlrequest.Request(data["url"], headers={"Host": data["authority"]})
    with _routed_urlopen(
        request,
        dial_host=data["dial_host"],
        dial_port=data["dial_port"],
        server_hostname=data["server_hostname"],
        timeout=10,
        context=context,
    ) as response:
        body_limit = public_probe_body_limit(data["url"])
        body = response.read(body_limit + 1)
        if len(body) > body_limit:
            raise OSError("public probe response exceeds its size limit")
        return {"status": response.status, "body": base64.b64encode(body).decode("ascii")}


def main() -> None:
    def deadline(_signum: int, _frame: object) -> None:
        raise TimeoutError("public probe exceeded its wall-clock deadline")

    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(15)
    try:
        result = _probe_once()
    except ssl.SSLCertVerificationError as exc:
        result = {"error_kind": "certificate", "message": str(exc)[:1024]}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result = {"error_kind": "probe", "message": str(exc)[:1024]}
    finally:
        signal.alarm(0)
    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
