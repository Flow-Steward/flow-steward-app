"""Public installed-configuration command specifications."""

from __future__ import annotations

from core.application.lifecycle.configuration_service import (
    ConfigApplyCommandResult,
    ConfigApplyRequest,
    ConfigSetRequest,
    ConfigSetResult,
    ConfigShowRequest,
    ConfigShowResult,
    ConfigValidateRequest,
)
from core.application.lifecycle.contracts import ConfigurationValidationResult
from core.application.management.command_specs.contracts import (
    ChoiceItem,
    CommandSpec,
    CompactSupport,
    FieldSpec,
    FieldType,
    InteractivePolicy,
    OperationType,
)

_TOKEN_CONFIG_SHOW = "config.show"
_TOKEN_CONFIG_VALIDATE = "config.validate"
_TOKEN_CONFIG_SET = "config.set"
_TOKEN_CONFIG_APPLY = "config.apply"

_TLS_CHOICES = tuple(
    ChoiceItem(value=value, label=label)
    for value, label in (
        ("local", "Local TLS"),
        ("self_signed", "Self-signed TLS"),
        ("provided", "Provided certificate"),
        ("acme", "Bundled ACME certificate"),
        ("reverse_proxy", "Reverse proxy"),
        ("external_resolver", "External Traefik resolver"),
        ("external_certificate", "External Traefik certificate"),
        ("platform_managed", "Platform-managed TLS"),
    )
)


CONFIG_COMMAND_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec(
        command_id=_TOKEN_CONFIG_SHOW,
        compact_support=CompactSupport.RETAINED_CLI,
        path=("config", "show"),
        summary="Show active and pending installed configuration.",
        description=(
            "Show the active deployment configuration, pending declarative diff, and only "
            "configured/missing state for durable secrets."
        ),
        examples=("flow-steward config show", "flow-steward config show --json"),
        operation_type=OperationType.READ,
        interactive_policy=InteractivePolicy.IMMEDIATE,
        fields=(),
        input_model=ConfigShowRequest,
        result_model=ConfigShowResult,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_CONFIG_SHOW,
        result_renderer_ref=_TOKEN_CONFIG_SHOW,
    ),
    CommandSpec(
        command_id=_TOKEN_CONFIG_VALIDATE,
        compact_support=CompactSupport.RETAINED_CLI,
        path=("config", "validate"),
        summary="Validate installed and pending configuration without mutation.",
        description=(
            "Validate paths, ports, TLS, storage, and Compose rendering without writing "
            "configuration or changing containers."
        ),
        examples=("flow-steward config validate", "flow-steward config validate --json"),
        operation_type=OperationType.READ,
        interactive_policy=InteractivePolicy.IMMEDIATE,
        fields=(),
        input_model=ConfigValidateRequest,
        result_model=ConfigurationValidationResult,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_CONFIG_VALIDATE,
        result_renderer_ref=_TOKEN_CONFIG_VALIDATE,
    ),
    CommandSpec(
        command_id=_TOKEN_CONFIG_SET,
        compact_support=CompactSupport.RETAINED_CLI,
        path=("config", "set"),
        summary="Stage an approved declarative configuration patch.",
        description=(
            "Stage one typed non-secret patch. This command never changes the active runtime; "
            "use config apply after review."
        ),
        examples=(
            "flow-steward config set --public-host flow.example.test --public-port 443",
            "flow-steward config set --tls-mode provided --certificate-path .ssl/cert.pem "
            "--private-key-path .ssl/key.pem",
        ),
        operation_type=OperationType.CHANGE,
        interactive_policy=InteractivePolicy.WIZARD_WHEN_INCOMPLETE,
        fields=(
            FieldSpec(
                key="deployment_profile",
                label="Deployment profile",
                type=FieldType.SINGLE_SELECT,
                choices=(
                    ChoiceItem(value="compact", label="Compact"),
                    ChoiceItem(value="scalable", label="Scalable"),
                ),
                option_name="--deployment-profile",
                examples=("compact",),
                description="Production runtime topology.",
            ),
            FieldSpec(
                key="runtime_http_port",
                label="Compact runtime HTTP port",
                type=FieldType.INTEGER,
                option_name="--runtime-http-port",
                examples=("8080",),
                description="Single HTTP backend port exposed by Compact.",
            ),
            FieldSpec(
                key="worker_processes",
                label="Compact worker processes",
                type=FieldType.INTEGER,
                option_name="--worker-processes",
                examples=("2",),
                description=(
                    "Compact only: queue-worker processes inside the one runtime container, "
                    "applied on recreate. Scalable scales with --worker-replicas."
                ),
            ),
            FieldSpec(
                key="worker_replicas",
                label="Scalable worker containers",
                type=FieldType.INTEGER,
                option_name="--worker-replicas",
                examples=("3",),
                description=(
                    "Scalable only: fs-worker container replicas. Compact scales with "
                    "--worker-processes."
                ),
            ),
            FieldSpec(
                key="file_storage",
                label="File storage",
                type=FieldType.SINGLE_SELECT,
                choices=(
                    ChoiceItem(value="local_fs", label="Local persistent filesystem"),
                    ChoiceItem(value="bundled", label="Bundled object storage"),
                    ChoiceItem(value="external_s3", label="External S3"),
                ),
                option_name="--file-storage",
                examples=("local_fs",),
                description="Artifact storage backend; profile changes preserve this value.",
            ),
            FieldSpec(
                key="ingress_mode",
                label="Ingress owner",
                type=FieldType.SINGLE_SELECT,
                choices=tuple(
                    ChoiceItem(value=value, label=label)
                    for value, label in (
                        ("bundled_traefik", "Bundled Traefik"),
                        ("external_traefik", "Existing Traefik"),
                        ("external_proxy", "Existing reverse proxy"),
                        ("platform", "Hosting platform"),
                    )
                ),
                option_name="--ingress-mode",
                examples=("external_traefik",),
                description="Component that owns public routing and TLS.",
            ),
            FieldSpec(
                key="public_host",
                label="Public host",
                type=FieldType.TEXT,
                option_name="--public-host",
                description=(
                    "Public DNS name or IP address used to reach Flow Steward. Together with "
                    "Public port, this generates FS_PUBLIC_BASE_URL; apply with config apply."
                ),
                examples=("flow.example.test",),
            ),
            FieldSpec(
                key="public_port",
                label="Public port",
                type=FieldType.INTEGER,
                option_name="--public-port",
                description=(
                    "Public TCP and UDP port exposed by the selected TLS mode. HTTPS port 443 "
                    "is omitted from the generated public URL; stack restart does not apply changes."
                ),
                examples=("443",),
            ),
            FieldSpec(
                key="tls_mode",
                label="TLS mode",
                type=FieldType.SINGLE_SELECT,
                choices=_TLS_CHOICES,
                option_name="--tls-mode",
                description="TLS termination mode for the public Flow Steward endpoint.",
                examples=("provided",),
            ),
            FieldSpec(
                key="reverse_proxy_backend_host",
                label="Reverse-proxy backend host",
                type=FieldType.TEXT,
                option_name="--reverse-proxy-backend-host",
                advanced=True,
                description="Backend host reached by the configured reverse proxy.",
                examples=("127.0.0.1",),
            ),
            FieldSpec(
                key="reverse_proxy_backend_port",
                label="Reverse-proxy backend port",
                type=FieldType.INTEGER,
                option_name="--reverse-proxy-backend-port",
                advanced=True,
                description="Backend TCP port reached by the configured reverse proxy.",
                examples=("8099",),
            ),
            FieldSpec(
                key="reverse_proxy_mcp_port",
                label="Reverse-proxy MCP port",
                type=FieldType.INTEGER,
                option_name="--reverse-proxy-mcp-port",
                advanced=True,
                examples=("18100",),
                description="Scalable MCP backend port for a host reverse proxy.",
            ),
            FieldSpec(
                key="proxy_placement",
                label="Proxy placement",
                type=FieldType.SINGLE_SELECT,
                choices=(
                    ChoiceItem(value="host", label="Host"),
                    ChoiceItem(value="docker", label="Docker network"),
                ),
                option_name="--proxy-placement",
                examples=("host",),
                description="Location of an existing Nginx or Apache proxy.",
            ),
            FieldSpec(
                key="proxy_kind",
                label="Proxy kind",
                type=FieldType.SINGLE_SELECT,
                choices=tuple(
                    ChoiceItem(value=value, label=label)
                    for value, label in (
                        ("nginx", "Nginx"),
                        ("apache", "Apache"),
                    )
                ),
                option_name="--proxy-kind",
                examples=("nginx",),
                description="Existing reverse-proxy implementation.",
            ),
            FieldSpec(
                key="proxy_trusted_ips",
                label="Trusted proxy CIDRs",
                type=FieldType.TEXT,
                option_name="--proxy-trusted-ips",
                advanced=True,
                examples=("127.0.0.1/32,::1/128",),
                description="Comma-separated immediate proxy CIDRs; wildcard networks are rejected.",
            ),
            FieldSpec(
                key="traefik_network",
                label="Traefik network",
                type=FieldType.TEXT,
                option_name="--traefik-network",
                examples=("traefik",),
                description="External Docker network used by an existing Traefik.",
            ),
            FieldSpec(
                key="traefik_https_entrypoint",
                label="Traefik HTTPS entrypoint",
                type=FieldType.TEXT,
                option_name="--traefik-https-entrypoint",
                advanced=True,
                examples=("websecure",),
                description="Existing Traefik HTTPS entrypoint name.",
            ),
            FieldSpec(
                key="traefik_http_entrypoint",
                label="Traefik HTTP entrypoint",
                type=FieldType.TEXT,
                option_name="--traefik-http-entrypoint",
                advanced=True,
                examples=("web",),
                description="Existing Traefik HTTP entrypoint name.",
            ),
            FieldSpec(
                key="traefik_cert_resolver",
                label="Traefik certificate resolver",
                type=FieldType.TEXT,
                option_name="--traefik-cert-resolver",
                examples=("myresolver",),
                description="Existing resolver used only by external_resolver TLS.",
            ),
            FieldSpec(
                key="proxy_network",
                label="Proxy network",
                type=FieldType.TEXT,
                option_name="--proxy-network",
                examples=("proxy",),
                description="External Docker network used by a container reverse proxy.",
            ),
            FieldSpec(
                key="acme_contact",
                label="ACME contact",
                type=FieldType.TEXT,
                option_name="--acme-contact",
                examples=("ops@example.com",),
                description="Contact email for bundled ACME registration.",
            ),
            FieldSpec(
                key="acme_directory_url",
                label="ACME directory URL",
                type=FieldType.TEXT,
                option_name="--acme-directory-url",
                advanced=True,
                examples=("https://acme-v02.api.letsencrypt.org/directory",),
                description="Explicit ACME CA directory URL.",
            ),
            FieldSpec(
                key="extension_installer_origin",
                label="Extension installer origin",
                type=FieldType.TEXT,
                option_name="--extension-installer-origin",
                advanced=True,
                examples=("http://fs-extension-installer:8099",),
                description="Validated internal HTTP origin for the installer role.",
            ),
            FieldSpec(
                key="certificate_path",
                label="Certificate path",
                type=FieldType.PATH,
                option_name="--certificate-path",
                description="Path to the operator-provided TLS certificate chain.",
                examples=(".ssl/cert.pem",),
            ),
            FieldSpec(
                key="private_key_path",
                label="Private-key path",
                type=FieldType.PATH,
                option_name="--private-key-path",
                description="Path to the operator-provided TLS certificate private key.",
                examples=(".ssl/key.pem",),
            ),
        ),
        input_model=ConfigSetRequest,
        result_model=ConfigSetResult,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_CONFIG_SET,
        result_renderer_ref=_TOKEN_CONFIG_SET,
    ),
    CommandSpec(
        command_id=_TOKEN_CONFIG_APPLY,
        compact_support=CompactSupport.RETAINED_CLI,
        path=("config", "apply"),
        summary="Apply the pending configuration safely.",
        description=(
            "Validate, snapshot, render, change runtime, verify preservation and health, and "
            "restore the exact previous configuration/runtime on failure."
        ),
        examples=("flow-steward config apply",),
        operation_type=OperationType.CHANGE,
        interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
        fields=(),
        input_model=ConfigApplyRequest,
        result_model=ConfigApplyCommandResult,
        plan_builder_ref="config.apply.preview",
        confirmation_policy_ref=None,
        service_ref=_TOKEN_CONFIG_APPLY,
        result_renderer_ref=_TOKEN_CONFIG_APPLY,
        lifecycle_interrupts=True,
    ),
)
