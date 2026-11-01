"""Translate a deliberately small expression grammar; extract authentication inputs."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .contracts import ConversionBlocked, require
from .json_source import load_json
from .secrets import _sensitive_key

_PATH = re.compile(r"\$json((?:\.[A-Za-z_][A-Za-z0-9_]*|\[\d+\])*)")
_EXPRESSION = re.compile(r"=\s*\{\{\s*(.*?)\s*\}\}\s*", re.DOTALL)
_FIELD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")


def field_path(value: object, path: str) -> str:
    require(
        isinstance(value, str) and _FIELD.fullmatch(value),
        "field_unsupported",
        path,
        "A field path is not supported.",
    )
    assert isinstance(value, str)
    require(
        not {"__proto__", "constructor", "prototype"}.intersection(value.split(".")),
        "field_unsupported",
        path,
        "A field path is not supported.",
    )
    return value


def translate(value: Any, incoming: str, path: str, *, template: bool) -> Any:
    """Full $json paths preserve types; JavaScript and mixed templates are refused."""
    if isinstance(value, str):
        expression = _EXPRESSION.fullmatch(value)
        if expression:
            text = expression.group(1).strip()
            match = _PATH.fullmatch(text)
            if match:
                selector = incoming + match.group(1)
                return "{{ " + selector + " }}" if template else {"source": selector}
            try:
                literal = load_json(text)
            except (ValueError, RecursionError):
                literal = None
            else:
                return translate(literal, incoming, path, template=template)
            raise ConversionBlocked(
                "expression_unsupported",
                path,
                "An expression needs semantic adaptation.",
                "Translate against state selectors; do not execute JavaScript or copy it into templates.",
            )
        require(
            not value.startswith("=") and "{{" not in value and "{%" not in value,
            "expression_unsupported",
            path,
            "A source expression or target template needs explicit adaptation.",
        )
        return value
    if isinstance(value, dict):
        require(
            set(value) != {"source"},
            "literal_unsupported",
            path,
            "A literal collides with a target source-reference record.",
        )
        return {k: translate(v, incoming, path, template=template) for k, v in value.items()}
    if isinstance(value, list):
        return [translate(v, incoming, path, template=template) for v in value]
    return value


@dataclass
class CredentialInputs:
    """Store only generated input definitions, never original credential values."""

    fields: list[dict[str, Any]] = field(default_factory=list)

    def reference(self, value: Any, *, authorization: bool = False) -> str:
        require(
            isinstance(value, str),
            "credential_field_ambiguous",
            "parameters",
            "A credential-like field has an unqualified non-text value.",
        )
        prefix = ""
        if authorization and isinstance(value, str):
            for scheme in ("Bearer ", "Basic ", "Token "):
                if value.casefold().startswith(scheme.casefold()):
                    prefix = value[: len(scheme)]
                    break
        name = f"credential_{len(self.fields) + 1:04d}"
        self.fields.append(
            {
                "name": name,
                "type": "secret",
                "required": True,
                "description": "Supply the credential value after the authentication scheme already shown at its usage site."
                if prefix
                else "Supply the complete source field value, including any authentication scheme that was part of that field.",
            }
        )
        return prefix + "{{ state.inputs." + name + " }}"

    def body(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                k: (v if isinstance(v, dict) and set(v) == {"source"} else self.reference(v))
                if _sensitive_key(k)
                else self.body(v)
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [self.body(v) for v in value]
        return value

    def url(self, url: str, path: str) -> tuple[str, dict[str, str]]:
        try:
            parts = urlsplit(url)
            # Accessing port also validates its spelling and range.
            _ = parts.port
            valid = (
                parts.scheme in {"http", "https"}
                and parts.hostname
                and not parts.username
                and not parts.password
                and not parts.fragment
            )
        except ValueError:
            valid = False
        require(valid, "http_url_unsupported", path, "The URL needs explicit adaptation.")
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        require(
            len({k for k, _ in pairs}) == len(pairs),
            "http_option_unsupported",
            path,
            "Repeated query parameters need explicit adaptation.",
        )
        secrets = {k: self.reference(v) for k, v in pairs if _sensitive_key(k)}
        public = [(k, v) for k, v in pairs if k not in secrets]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(public), "")), secrets


def literal_json(value: Any, path: str) -> Any:
    """A parsed JSON parameter is literal; never interpret strings inside it."""
    if isinstance(value, str):
        require(
            "{{" not in value and "{%" not in value,
            "expression_unsupported",
            path,
            "Literal source data collides with target template syntax.",
        )
    elif isinstance(value, dict):
        require(
            set(value) != {"source"},
            "literal_unsupported",
            path,
            "Literal data collides with a target source record.",
        )
        for nested in value.values():
            literal_json(nested, path)
    elif isinstance(value, list):
        for nested in value:
            literal_json(nested, path)
    return value


def contains_source(value: Any) -> bool:
    """Detect references structurally rather than guessing expression whitespace."""
    if isinstance(value, dict):
        return set(value) == {"source"} or any(contains_source(v) for v in value.values())
    return isinstance(value, list) and any(contains_source(v) for v in value)
