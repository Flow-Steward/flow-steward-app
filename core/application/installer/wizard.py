"""Pure policy for a short installation form and deterministic scripted defaults."""

from __future__ import annotations

from typing import Any

from core.application.installer.spec import INSTALLER_SPEC
from core.application.lifecycle.routing_policy import INGRESS_TLS_MODES
from core.application.management.command_specs.contracts import FieldSpec

_ACME_DIRECTORY = "https://acme-v02.api.letsencrypt.org/directory"
_ORDER = (
    "environment",
    "deployment_profile",
    "ingress_mode",
    "proxy_placement",
    "proxy_kind",
    "tls_mode",
    "public_host",
    "file_storage",
)


def ordered_fields() -> tuple[FieldSpec, ...]:
    priority = {key: index for index, key in enumerate(_ORDER)}
    return tuple(
        sorted(INSTALLER_SPEC.fields, key=lambda field: priority.get(field.key, len(priority)))
    )


def normalize_install_values(
    values: dict[str, Any], *, supplied_keys: set[str] | None = None
) -> dict[str, Any]:
    explicit = frozenset(values if supplied_keys is None else supplied_keys)
    return InstallerWizardPolicy(locked_keys=explicit).normalize(values, explicit)


class InstallerWizardPolicy:
    """Resolve dependent defaults without replacing deliberately supplied CLI flags."""

    def __init__(
        self, *, locked_keys: frozenset[str] = frozenset(), advanced: bool = False
    ) -> None:
        self.locked_keys = locked_keys
        self.advanced = advanced

    def normalize(self, values: dict[str, Any], explicit_keys: frozenset[str]) -> dict[str, Any]:
        normalized = dict(values)
        if "tls_mode" in self.locked_keys and "ingress_mode" not in self.locked_keys:
            normalized["ingress_mode"] = {
                "reverse_proxy": "external_proxy",
                "external_resolver": "external_traefik",
                "external_certificate": "external_traefik",
                "platform_managed": "platform",
            }.get(str(normalized.get("tls_mode")), "bundled_traefik")
        for field in INSTALLER_SPEC.fields:
            if field.key not in normalized and field.default is not None:
                normalized[field.key] = (
                    list(field.default) if isinstance(field.default, tuple) else field.default
                )
        if (
            normalized.get("environment") == "development"
            and "deployment_profile" not in self.locked_keys
        ):
            normalized["deployment_profile"] = "scalable"
        if (
            normalized.get("deployment_profile") == "scalable"
            and normalized.get("ingress_mode") == "platform"
            and "ingress_mode" not in self.locked_keys
        ):
            normalized["ingress_mode"] = "bundled_traefik"
        route = str(normalized.get("ingress_mode", "bundled_traefik"))
        allowed = INGRESS_TLS_MODES.get(route, ())
        if (
            allowed
            and normalized.get("tls_mode") not in allowed
            and "tls_mode" not in self.locked_keys
        ):
            normalized["tls_mode"] = allowed[0]
        if "public_port" not in explicit_keys:
            normalized["public_port"] = (
                443
                if normalized.get("tls_mode") not in {"local", "self_signed", "provided"}
                else 8099
            )
        if normalized.get("tls_mode") == "acme":
            normalized.setdefault("acme_directory_url", _ACME_DIRECTORY)
            if "redirect_http_port" not in explicit_keys:
                normalized["redirect_http_port"] = 80
        if normalized.get("environment") == "production":
            normalized["development_tools"] = ()
        return normalized

    def resolve_field(self, field: FieldSpec, values: dict[str, Any]) -> FieldSpec:
        updates: dict[str, Any] = {}
        if field.key == "deployment_profile" and values.get("environment") == "development":
            updates["choices"] = tuple(
                choice for choice in field.choices if choice.value == "scalable"
            )
        if field.key == "ingress_mode" and values.get("deployment_profile") == "scalable":
            updates["choices"] = tuple(
                choice for choice in field.choices if choice.value != "platform"
            )
        if field.key == "tls_mode":
            allowed = INGRESS_TLS_MODES.get(str(values.get("ingress_mode", "bundled_traefik")), ())
            updates["choices"] = tuple(
                choice
                for choice in field.choices
                if choice.value in allowed and (self.advanced or choice.value != "self_signed")
            )
            updates["visible_if"] = {
                "ingress_mode": {"values": ["bundled_traefik", "external_traefik"]}
            }
        if field.key in {"certificate_path", "private_key_path"}:
            updates["visible_if"] = {"tls_mode": {"values": ["provided", "external_certificate"]}}
            updates["advanced"] = False
        if field.key in {"traefik_network", "traefik_cert_resolver", "proxy_network"}:
            updates["advanced"] = False
            updates["required"] = True
        if field.key == "acme_contact":
            updates["required"] = True
        return field.model_copy(update=updates)


def installation_error_message(exc: Exception) -> str:
    """Present validation issues without echoing input dictionaries or Pydantic internals."""
    from pydantic import ValidationError

    if isinstance(exc, ValidationError):
        return "; ".join(
            f"{'.'.join(map(str, error['loc'])) or 'Installation'}: {str(error['msg']).removeprefix('Value error, ')}"
            for error in exc.errors(include_input=False, include_url=False)
        )
    return str(exc)


def require_source_opt_in(values: dict[str, Any], *, from_source: bool) -> None:
    from core.application.installer.errors import InstallerFailure

    if values.get("environment") == "development" and not from_source:
        raise InstallerFailure(
            code="source_install_requires_flag",
            cause="Development requires an explicit source installation.",
            remediation="Rerun ./install.sh --from-source --environment development.",
            exit_code=3,
            retryable=False,
            step="configuration",
        )
