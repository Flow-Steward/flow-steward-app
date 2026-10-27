"""The page a Template step's HTML or Markdown preview is shown in.

The admin's own policy allows images only from this server, and a ``srcdoc``
frame inherits that policy, so a template's HTTPS images never loaded. This page
is served instead, with a policy of its own:

- ``sandbox allow-scripts`` without ``allow-same-origin``: the page runs in an
  opaque origin, so nothing in it can reach the app's cookies, storage or API.
  The sandbox comes from this header, not from the frame's ``sandbox``
  attribute: some embedded Chromium browsers run no script at all in a
  network-loaded frame that has the attribute, and the preview stayed blank;
- the only script allowed is the one below, pinned by its hash; inline handlers,
  ``javascript:`` URLs and any script the preview contains do not run;
- images may come from any HTTPS address or a ``data:`` URL; frames, forms,
  fetches, fonts and media are refused, and a click on a link does nothing.

The page never receives the preview from the server. The builder hands the
rendered HTML to it with ``postMessage``, and the script accepts it only from
the window that embeds it and only from this server's origin.
"""

from __future__ import annotations

import base64
import hashlib

TEMPLATE_PREVIEW_FRAME_PATH = "/app/template-preview-frame"

# Message types shared with assets/frontend/src/pages/workflows/detail/detail-template-preview.tsx.
_READY_MESSAGE = "flow-steward:template-preview-ready"
_CONTENT_MESSAGE = "flow-steward:template-preview"

_SCRIPT = (
    "(function(){"
    "var own=new URL(location.href).origin;"
    "window.addEventListener('message',function(event){"
    "if(event.source!==window.parent||event.origin!==own)return;"
    "var data=event.data;"
    f"if(!data||data.type!=='{_CONTENT_MESSAGE}'||typeof data.html!=='string')return;"
    "document.body.innerHTML=data.html;"
    "});"
    # A link in the preview would load another site into the frame.
    "document.addEventListener('click',function(event){"
    "if(event.target&&event.target.closest&&event.target.closest('a'))event.preventDefault();"
    "});"
    f"window.parent.postMessage({{type:'{_READY_MESSAGE}'}},own);"
    "})();"
)

_SCRIPT_HASH = base64.b64encode(hashlib.sha256(_SCRIPT.encode("utf-8")).digest()).decode("ascii")

TEMPLATE_PREVIEW_FRAME_CSP = "; ".join(
    (
        "sandbox allow-scripts",
        "default-src 'none'",
        f"script-src 'sha256-{_SCRIPT_HASH}'",
        "style-src 'unsafe-inline'",
        "img-src https: data:",
        "frame-src 'none'",
        "form-action 'none'",
        "base-uri 'none'",
        "frame-ancestors 'self'",
    )
)

TEMPLATE_PREVIEW_FRAME_HTML = (
    '<!doctype html><html><head><meta charset="utf-8">'
    '<meta name="referrer" content="no-referrer">'
    "<title>Template preview</title>"
    # Large images fit the frame instead of overflowing it.
    "<style>img{max-width:100%;height:auto}</style></head>"
    '<body style="margin:12px;font-family:system-ui,sans-serif;color:#111;background:#fff">'
    f"<script>{_SCRIPT}</script>"
    "</body></html>"
)

TEMPLATE_PREVIEW_FRAME_HEADERS = {
    "Content-Security-Policy": TEMPLATE_PREVIEW_FRAME_CSP,
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
}


__all__ = [
    "TEMPLATE_PREVIEW_FRAME_CSP",
    "TEMPLATE_PREVIEW_FRAME_HEADERS",
    "TEMPLATE_PREVIEW_FRAME_HTML",
    "TEMPLATE_PREVIEW_FRAME_PATH",
]
