#!/usr/bin/env python3
"""Deterministic loopback OAuth provider for offline browser acceptance tests."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import ssl
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar
from urllib.parse import parse_qs, urlencode, urlsplit


@dataclass(frozen=True)
class AuthorizationCode:
    client_id: str
    code_challenge: str
    redirect_uri: str
    scenario: str
    scope: str


class OAuthFixtureHandler(BaseHTTPRequestHandler):
    server_version = "FlowStewardOAuthFixture/1"
    _lock = threading.Lock()
    _next_code = 0
    _codes: ClassVar[dict[str, AuthorizationCode]] = {}
    _refresh_tokens: ClassVar[dict[str, tuple[str, str]]] = {}
    _access_tokens: ClassVar[set[str]] = set()
    _clients: ClassVar[dict[str, set[str]]] = {
        "backup-application-client": {"fixture-credential"},
        "isolated-application-client": {"fixture-credential"},
        "primary-application-client": {"fixture-credential", "fixed-credential"},
    }

    def _json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _form(self) -> dict[str, str] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length < 0 or length > 16_384:
            return None
        parsed = parse_qs(self.rfile.read(length).decode(), keep_blank_values=True)
        if len(parsed) > 32 or any(len(values) != 1 for values in parsed.values()):
            return None
        return {key: values[0] for key, values in parsed.items() if values}

    @classmethod
    def _issue_code(cls, record: AuthorizationCode) -> str:
        with cls._lock:
            cls._next_code += 1
            code = f"fixture-code-{cls._next_code}"
            cls._codes[code] = record
        return code

    @staticmethod
    def _client_is_valid(client_id: str, client_secret: str) -> bool:
        return client_secret in OAuthFixtureHandler._clients.get(client_id, set())

    @staticmethod
    def _redirect_is_valid(value: str) -> bool:
        try:
            parsed = urlsplit(value)
            allowed_origin = os.environ.get(
                "FS_OAUTH_ALLOWED_REDIRECT_ORIGIN", "https://traefik:8099"
            )
            allowed = urlsplit(allowed_origin)
            segments = parsed.path.split("/")
        except (TypeError, ValueError):
            return False
        if allowed.path not in {"", "/"} or allowed.query or allowed.fragment or len(segments) != 6:
            return False
        capability = segments[-1]
        return (
            parsed.scheme == allowed.scheme
            and parsed.netloc == allowed.netloc
            and segments[:5]
            == [
                "",
                "webhook",
                "extensions",
                "fixture.extension-oauth",
                "oauth_authorization",
            ]
            and 1 <= len(capability) <= 256
            and not parsed.query
            and not parsed.fragment
        )

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        if parsed.path == "/health":
            self._json(HTTPStatus.OK, {"status": "ok"})
            return
        if parsed.path != "/authorize":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        redirect_uri = (query.get("redirect_uri") or [""])[-1]
        state = (query.get("state") or [""])[-1]
        hint = (query.get("login_hint") or [""])[-1]
        client_id = (query.get("client_id") or [""])[-1]
        code_challenge = (query.get("code_challenge") or [""])[-1]
        scope = (query.get("scope") or [""])[-1]
        scenario = hint.partition("@")[0] if "@" in hint else "success"
        if scenario not in {
            "authorization_required",
            "deny",
            "invalid_client",
            "invalid_grant",
            "post_connect_failure",
            "success",
            "unavailable",
        }:
            scenario = "success"
        if (
            (query.get("response_type") or [""])[-1] != "code"
            or (query.get("code_challenge_method") or [""])[-1] != "S256"
            or not self._client_is_valid(client_id, "fixture-credential")
            or not self._redirect_is_valid(redirect_uri)
            or not state
            or len(state) > 4096
            or not code_challenge
            or len(code_challenge) > 128
            or "mail.read" not in scope.split()
        ):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request"})
            return
        callback = urlsplit(redirect_uri)
        result = {"state": state}
        if scenario == "deny":
            result["error"] = "access_denied"
        else:
            result["code"] = self._issue_code(
                AuthorizationCode(
                    client_id=client_id,
                    code_challenge=code_challenge,
                    redirect_uri=redirect_uri,
                    scenario=scenario,
                    scope=scope,
                )
            )
        separator = "&" if callback.query else "?"
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", f"{redirect_uri}{separator}{urlencode(result)}")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        form = self._form()
        if form is None:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request"})
            return
        if parsed.path == "/revoke":
            client_id = form.get("client_id", "")
            if not self._client_is_valid(client_id, form.get("client_secret", "")):
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "invalid_client"})
                return
            token = form.get("token", "")
            with self._lock:
                known = token in self._access_tokens or token in self._refresh_tokens
                self._access_tokens.discard(token)
                self._refresh_tokens.pop(token, None)
            if not known:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_token"})
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if parsed.path != "/token":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        grant_type = form.get("grant_type", "")
        client_id = form.get("client_id", "")
        if not self._client_is_valid(client_id, form.get("client_secret", "")):
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "invalid_client"})
            return
        scope = ""
        if grant_type == "authorization_code":
            code = form.get("code", "")
            with self._lock:
                record = self._codes.pop(code, None)
            if record is None:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_grant"})
                return
            verifier = form.get("code_verifier", "")
            actual_challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .rstrip(b"=")
                .decode()
            )
            if (
                record.client_id != client_id
                or record.redirect_uri != form.get("redirect_uri", "")
                or record.code_challenge != actual_challenge
            ):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_grant"})
                return
            scenario = record.scenario
            scope = record.scope
        elif grant_type == "refresh_token":
            refresh_token = form.get("refresh_token", "")
            with self._lock:
                refresh = self._refresh_tokens.get(refresh_token)
            if refresh is None or refresh[0] != client_id:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_grant"})
                return
            scenario = refresh[1]
            scope = form.get("scope", "mail.read mail.write")
        else:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "unsupported_grant_type"})
            return
        if scenario in {"invalid_client", "invalid_grant", "authorization_required"}:
            self._json(HTTPStatus.BAD_REQUEST, {"error": scenario})
            return
        if scenario == "unavailable":
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "temporarily_unavailable"})
            return
        with self._lock:
            serial = len(self._access_tokens) + 1
            access_token = f"fixture-access-{scenario}-{serial}"
            refresh_token = f"fixture-refresh-{serial}"
            self._access_tokens.add(access_token)
            self._refresh_tokens[refresh_token] = (client_id, "success")
        self._json(
            HTTPStatus.OK,
            {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "token_type": "Bearer",
                "expires_in": 0 if scenario == "post_connect_failure" else 120,
                "scope": scope,
            },
        )

    def log_message(self, _format: str, *_args: object) -> None:
        return


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=0, type=int)
    parser.add_argument("--cert-file")
    parser.add_argument("--key-file")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), OAuthFixtureHandler)
    if bool(args.cert_file) != bool(args.key_file):
        parser.error("--cert-file and --key-file must be supplied together")
    if args.cert_file and args.key_file:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(args.cert_file, args.key_file)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    print(json.dumps({"host": args.host, "port": server.server_port}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
