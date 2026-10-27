"""Protocol probes for roles that cannot report themselves."""

from __future__ import annotations

from urllib.error import HTTPError
from urllib.request import Request, urlopen

#: The MCP server answers its endpoint with a 4xx to a bare GET: that is a
#: protocol answer, so the server is up. Only a 5xx or no answer is not.
_PROTOCOL_READY_BELOW = 500


def probe_http_role(url: str, *, timeout_seconds: float = 2.0) -> bool:
    """Whether a role answers its HTTP endpoint with anything but a server error."""
    if not url.startswith(("http://", "https://")):
        return False
    try:
        # The URL is operator configuration (MCP_URL), limited to http(s) above.
        request = Request(url, method="GET")  # nosec B310
        with urlopen(request, timeout=timeout_seconds) as response:  # nosec B310
            return int(response.status) < _PROTOCOL_READY_BELOW
    except HTTPError as exc:
        return int(exc.code) < _PROTOCOL_READY_BELOW
    except (OSError, ValueError):  # URLError is an OSError
        return False


__all__ = ["probe_http_role"]
