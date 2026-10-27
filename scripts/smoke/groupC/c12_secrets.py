#!/usr/bin/env python3
"""C12 — sweep GET endpoints for password hashes / raw secrets.

Also does a write-then-read roundtrip on a project secret to confirm the plaintext
is not echoed back.
"""

import http.cookiejar
import json
import re
import ssl
import sys
import urllib.error
import urllib.request

BASE = "https://localhost:8099"
ACCOUNT = "00000000-0000-0000-0000-000000000002"
SENTINEL = "regC-SENTINEL-SECRET-9f3a2b"

PATTERNS = [
    ("password_hash key", re.compile(r'"password_hash"')),
    ("bcrypt hash", re.compile(r"\$2[aby]\$[0-9]{2}\$")),
    ("pbkdf2/argon2/scrypt", re.compile(r"(pbkdf2[:$]|argon2[id]?\$|scrypt[:$])")),
    ("token_hash key", re.compile(r'"token_hash"')),
    ("client_secret value", re.compile(r'"client_secret"\s*:\s*"[^"]{4,}"')),
    ("private key block", re.compile(r"BEGIN [A-Z ]*PRIVATE KEY")),
    ("openai-style key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("sentinel plaintext", re.compile(re.escape(SENTINEL))),
]

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(
    urllib.request.HTTPSHandler(context=ctx), urllib.request.HTTPCookieProcessor(cj)
)
CSRF = ""


def call(method, path, body=None, timeout=20):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Origin", BASE)
    if data:
        req.add_header("Content-Type", "application/json")
    if CSRF:
        req.add_header("x-csrf-token", CSRF)
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # timeout etc.
        return 0, f"<{type(e).__name__}: {e}>"


email, password = sys.argv[1], sys.argv[2]
st, bd = call(
    "POST", "/api/auth/login", {"email": email, "password": password, "account_id": ACCOUNT}
)
if st != 200:
    print("LOGIN FAILED", st, bd[:200])
    sys.exit(2)
CSRF = json.loads(bd)["csrf_token"]

st, bd = call("GET", "/api/projects?per_page=1")
pid = json.loads(bd)["projects"][0]["id"] if st == 200 else ""

paths = [
    "/api/auth/me",
    "/api/auth/users?per_page=200",
    "/api/auth/tokens",
    "/api/auth/accounts",
    "/api/auth/service-accounts",
    "/api/model-providers",
    "/api/llm-provider-extensions",
    "/api/providers",
    "/api/extensions",
    "/api/workspaces",
    f"/api/projects/{pid}",
    f"/api/projects/{pid}/connections",
    f"/api/projects/{pid}/providers",
    f"/api/projects/{pid}/secrets",
    "/api/guardrails?per_page=200",
    "/api/agents?per_page=200",
    "/api/trash?per_page=100",
]

# write a sentinel secret first so the read sweep can detect echo-back
wrote = []
for p, body in (
    (
        f"/api/projects/{pid}/secrets",
        {"secret_key": "regc_probe_secret", "secret_value": SENTINEL},  # pragma: allowlist secret
    ),  # smoke sentinel; value is SENTINEL
):
    st, bd = call("POST", p, body)
    print(f"[setup] POST {p} -> {st} {bd[:140]}")
    if st < 300:
        wrote.append(p)

bad = 0
for p in paths:
    st, bd = call("GET", p)
    hits = [name for name, rx in PATTERNS if rx.search(bd)]
    if hits:
        bad = 1
        print(f"LEAK  HTTP {st:3d} {p}  :: {', '.join(hits)}")
    else:
        print(f"clean HTTP {st:3d} {p}")

for p in wrote:
    call("DELETE", p + "/regc_probe_secret")
print("RESULT:", "FAIL" if bad else "PASS")
sys.exit(bad)
