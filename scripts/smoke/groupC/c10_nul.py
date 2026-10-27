#!/usr/bin/env python3
"""C10 — NUL byte in a text field should give a typed 4xx, not a bare 500."""

import http.cookiejar
import json
import ssl
import sys
import urllib.request

BASE = "https://localhost:8099"
ACCOUNT = "00000000-0000-0000-0000-000000000002"
NUL = chr(0)

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(
    urllib.request.HTTPSHandler(context=ctx),
    urllib.request.HTTPCookieProcessor(cj),
)


def call(method, path, body=None, csrf=""):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Origin", BASE)
    if data:
        req.add_header("Content-Type", "application/json")
    if csrf:
        req.add_header("x-csrf-token", csrf)
    try:
        with opener.open(req) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


email, password = sys.argv[1], sys.argv[2]
st, bd = call(
    "POST", "/api/auth/login", {"email": email, "password": password, "account_id": ACCOUNT}
)
if st != 200:
    print("LOGIN FAILED", st, bd[:200])
    sys.exit(2)
csrf = json.loads(bd)["csrf_token"]

cases = [
    (
        "POST /api/projects name",
        "POST",
        "/api/projects",
        {
            "name": "regC-nul" + NUL + "x",
            "workspace_id": "00000000-0000-0000-0000-000000000003",
        },
    ),
    (
        "POST /api/projects description",
        "POST",
        "/api/projects",
        {
            "name": "regC-nul-desc",
            "workspace_id": "00000000-0000-0000-0000-000000000003",
            "description": "regC" + NUL + "desc",
        },
    ),
    (
        "PUT user display_name",
        "PUT",
        "/api/auth/users/1302a07f-164d-4e4a-842d-9c05ce286be5",
        {"display_name": "regC" + NUL + "nul"},
    ),
    (
        "POST /api/guardrails display_name",
        "POST",
        "/api/guardrails",
        {
            "display_name": "regC" + NUL + "gr",
            "enabled": True,
            "mode": "deterministic",
            "rule_type": "forbidden_patterns",
            "severity": "error",
            "config": {"check_in": "diff", "patterns": ["x"]},
            "agents": ["developer"],
        },
    ),
    (
        "POST /api/workspaces display_name",
        "POST",
        "/api/workspaces",
        {"workspace_key": "regc-nul-ws", "display_name": "regC" + NUL + "ws"},
    ),
]

worst = 0
for label, m, p, b in cases:
    st, bd = call(m, p, b, csrf)
    verdict = "FAIL(500)" if st >= 500 else "ok"
    if st >= 500:
        worst = 1
    print(f"{label:38s} -> HTTP {st} {verdict}  {bd[:170]}")
sys.exit(worst)
