"""Single source of installer fields for Click, Textual, and plain modes."""

from __future__ import annotations

from core.application.installer.contracts import InstallConfig, InstallRunResult
from core.application.management.command_specs.contracts import (
    ChoiceItem,
    CommandSpec,
    FieldSpec,
    FieldType,
    InteractivePolicy,
    OperationType,
)

_TOKEN_INSTALLER_RUN = "installer.run"

INSTALLER_SPEC = CommandSpec(
    command_id=_TOKEN_INSTALLER_RUN,
    path=("installer", "run"),
    summary="Install or safely resume Flow Steward.",
    description="Bring up core without requiring AI credentials.",
    examples=("./install.sh", "./install.sh --non-interactive --confirm"),
    operation_type=OperationType.CHANGE,
    interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
    fields=(
        FieldSpec(
            key="environment",
            label="Environment",
            type=FieldType.SINGLE_SELECT,
            choices=(
                ChoiceItem(value="production", label="Production"),
                ChoiceItem(value="development", label="Development"),
            ),
            default="production",
            option_name="--environment",
            description=(
                "Production installs the minimal runtime; Development installs the complete "
                "contributor preset. AC17-R92: the default profile is Compact, which is a "
                "production topology, so these two defaults have to agree -- otherwise the "
                "questionnaire refuses its own default answers."
            ),
        ),
        FieldSpec(
            key="deployment_profile",
            label="Deployment profile",
            type=FieldType.SINGLE_SELECT,
            choices=(
                ChoiceItem(value="compact", label="Compact single-runtime deployment"),
                ChoiceItem(value="scalable", label="Scalable multi-container runtime"),
            ),
            default="compact",
            option_name="--deployment-profile",
            description=(
                "Runtime topology used for the production deployment. Compact runs the "
                "whole platform as one runtime and is the default; the installer builds "
                "its image when you do not supply one. Scalable runs each service in its "
                "own container."
            ),
        ),
        FieldSpec(
            key="runtime_http_port",
            label="Compact runtime HTTP port",
            type=FieldType.INTEGER,
            default=8080,
            option_name="--runtime-http-port",
            visible_if={"deployment_profile": "compact"},
            advanced=True,
            description="Single HTTP port exposed by the Compact runtime.",
        ),
        FieldSpec(
            key="ingress_mode",
            label="Ingress owner",
            type=FieldType.SINGLE_SELECT,
            choices=(
                ChoiceItem(value="bundled_traefik", label="Bundled Traefik"),
                ChoiceItem(value="external_traefik", label="Existing Traefik"),
                ChoiceItem(value="external_proxy", label="Existing reverse proxy"),
                ChoiceItem(value="platform", label="Hosting platform ingress"),
            ),
            default="bundled_traefik",
            option_name="--ingress-mode",
            description="Component that owns public routing and TLS.",
        ),
        FieldSpec(
            key="worker_processes",
            label="Compact worker processes",
            type=FieldType.INTEGER,
            default=2,
            option_name="--worker-processes",
            visible_if={"deployment_profile": "compact"},
            description="Number of queue-worker processes inside the Compact runtime.",
        ),
        FieldSpec(
            key="file_storage",
            label="File storage",
            type=FieldType.SINGLE_SELECT,
            choices=(
                ChoiceItem(value="local_fs", label="Local persistent filesystem"),
                ChoiceItem(value="bundled", label="Bundled self-hosted storage"),
                ChoiceItem(value="external_s3", label="External S3-compatible storage"),
            ),
            default="bundled",
            option_name="--file-storage",
            description=(
                "Storage backend selected for new artifacts. Scalable defaults to bundled "
                "self-hosted storage; Compact uses the local persistent filesystem unless you "
                "choose external S3-compatible storage here or pass --file-storage bundled."
            ),
        ),
        FieldSpec(
            key="s3_endpoint",
            label="S3 endpoint",
            type=FieldType.TEXT,
            required=True,
            option_name="--s3-endpoint",
            visible_if={"file_storage": "external_s3"},
            description="HTTPS endpoint for the selected S3-compatible storage.",
        ),
        FieldSpec(
            key="s3_bucket",
            label="S3 bucket",
            type=FieldType.TEXT,
            default="flow-steward-artifacts",
            option_name="--s3-bucket",
            visible_if={"file_storage": "external_s3"},
            description="Bucket used for Flow Steward artifacts.",
        ),
        FieldSpec(
            key="s3_region",
            label="S3 region",
            type=FieldType.TEXT,
            default="us-east-1",
            option_name="--s3-region",
            visible_if={"file_storage": "external_s3"},
            description="Region required by the S3-compatible endpoint.",
        ),
        FieldSpec(
            key="s3_credentials_path",
            label="S3 credentials file",
            type=FieldType.PATH,
            required=True,
            option_name="--s3-credentials-path",
            visible_if={"file_storage": "external_s3"},
            description="Owner-only JSON file containing access_key and secret_key.",
        ),
        FieldSpec(
            key="allow_insecure_s3_http",
            label="Allow insecure S3 HTTP",
            type=FieldType.BOOLEAN,
            default=False,
            option_name="--allow-insecure-s3-http",
            visible_if={"file_storage": "external_s3"},
            description="Development only: explicitly allow an unencrypted S3 endpoint.",
        ),
        FieldSpec(
            key="tls_mode",
            label="Network and TLS",
            type=FieldType.SINGLE_SELECT,
            choices=(
                ChoiceItem(value="local", label="Local/private generated certificate"),
                ChoiceItem(value="self_signed", label="Hostname generated certificate"),
                ChoiceItem(value="provided", label="Operator-provided certificate"),
                ChoiceItem(value="acme", label="Bundled ACME certificate"),
                ChoiceItem(value="reverse_proxy", label="Existing reverse-proxy TLS"),
                ChoiceItem(value="external_resolver", label="Existing Traefik resolver"),
                ChoiceItem(value="external_certificate", label="Existing Traefik certificate"),
                ChoiceItem(value="platform_managed", label="Hosting platform TLS"),
            ),
            default="local",
            option_name="--tls-mode",
            description="How the public endpoint receives TLS.",
        ),
        FieldSpec(
            key="public_host",
            label="Public host (domain or IP)",
            type=FieldType.TEXT,
            default="localhost",
            option_name="--public-host",
            description=(
                "Public domain or IP included in the product URL and generated SANs. "
                "Keep localhost for local use without a domain."
            ),
        ),
        FieldSpec(
            key="public_port",
            label="Public port",
            type=FieldType.INTEGER,
            default=8099,
            option_name="--public-port",
            description="HTTPS/public endpoint port.",
        ),
        FieldSpec(
            key="redirect_http_port",
            label="Plain-HTTP redirect port",
            type=FieldType.INTEGER,
            default=8080,
            option_name="--redirect-http-port",
            visible_if={"ingress_mode": "bundled_traefik"},
            advanced=True,
            description=(
                "Host port the bundled reverse proxy binds for its HTTP-to-HTTPS "
                "redirect. AC17-R92: this was settable only through the HTTP_PORT "
                "environment variable, so an install on a host where 8080 is already "
                "taken -- any host already running this product -- could not be "
                "completed by answering the questionnaire alone."
            ),
        ),
        FieldSpec(
            key="reverse_proxy_backend_host",
            label="Reverse-proxy backend host",
            type=FieldType.TEXT,
            default="127.0.0.1",
            option_name="--reverse-proxy-backend-host",
            visible_if={"tls_mode": "reverse_proxy"},
            description="Loopback address exposed to the existing host reverse proxy.",
        ),
        FieldSpec(
            key="reverse_proxy_backend_port",
            label="Reverse-proxy backend port",
            type=FieldType.INTEGER,
            default=18099,
            option_name="--reverse-proxy-backend-port",
            visible_if={"tls_mode": "reverse_proxy"},
            description="Private HTTP port used only by the existing host reverse proxy.",
        ),
        FieldSpec(
            key="reverse_proxy_mcp_port",
            label="Reverse-proxy MCP port",
            type=FieldType.INTEGER,
            default=18100,
            option_name="--reverse-proxy-mcp-port",
            visible_if={"ingress_mode": "external_proxy"},
            advanced=True,
            description="Scalable MCP backend port for an existing host proxy.",
        ),
        FieldSpec(
            key="proxy_placement",
            label="Proxy placement",
            type=FieldType.SINGLE_SELECT,
            choices=(
                ChoiceItem(value="host", label="Host"),
                ChoiceItem(value="docker", label="Docker network"),
            ),
            default="host",
            option_name="--proxy-placement",
            visible_if={"ingress_mode": "external_proxy"},
            description="Whether the selected existing proxy runs on the host or in Docker.",
        ),
        FieldSpec(
            key="proxy_kind",
            label="Proxy kind",
            type=FieldType.SINGLE_SELECT,
            choices=(
                ChoiceItem(value="nginx", label="Nginx"),
                ChoiceItem(value="apache", label="Apache"),
            ),
            default="nginx",
            option_name="--proxy-kind",
            visible_if={"ingress_mode": "external_proxy"},
            description="Reverse-proxy syntax to render and validate.",
        ),
        FieldSpec(
            key="proxy_trusted_ips",
            label="Trusted proxy CIDRs",
            type=FieldType.TEXT,
            default="127.0.0.1/32,::1/128",
            option_name="--proxy-trusted-ips",
            advanced=True,
            description="Comma-separated immediate proxy CIDRs; wildcard networks are rejected.",
        ),
        FieldSpec(
            key="traefik_network",
            label="Existing Traefik network",
            type=FieldType.TEXT,
            option_name="--traefik-network",
            visible_if={"ingress_mode": "external_traefik"},
            description="External Docker network shared with an existing Traefik.",
        ),
        FieldSpec(
            key="traefik_https_entrypoint",
            label="Traefik HTTPS entrypoint",
            type=FieldType.TEXT,
            default="websecure",
            option_name="--traefik-https-entrypoint",
            visible_if={"ingress_mode": "external_traefik"},
            advanced=True,
            description="HTTPS entrypoint configured by the existing Traefik owner.",
        ),
        FieldSpec(
            key="traefik_http_entrypoint",
            label="Traefik HTTP entrypoint",
            type=FieldType.TEXT,
            default="web",
            option_name="--traefik-http-entrypoint",
            visible_if={"ingress_mode": "external_traefik"},
            advanced=True,
            description="HTTP entrypoint configured by the existing Traefik owner.",
        ),
        FieldSpec(
            key="traefik_cert_resolver",
            label="Traefik certificate resolver",
            type=FieldType.TEXT,
            option_name="--traefik-cert-resolver",
            visible_if={"tls_mode": "external_resolver"},
            description="Existing Traefik resolver selected for this route.",
        ),
        FieldSpec(
            key="proxy_network",
            label="Existing proxy network",
            type=FieldType.TEXT,
            option_name="--proxy-network",
            visible_if={"proxy_placement": "docker"},
            description="External Docker network shared with the existing proxy.",
        ),
        FieldSpec(
            key="acme_contact",
            label="ACME contact",
            type=FieldType.TEXT,
            option_name="--acme-contact",
            visible_if={"tls_mode": "acme"},
            description="Contact email used for bundled ACME registration.",
        ),
        FieldSpec(
            key="acme_directory_url",
            label="ACME directory URL",
            type=FieldType.TEXT,
            option_name="--acme-directory-url",
            visible_if={"tls_mode": "acme"},
            advanced=True,
            description="ACME CA directory, including staging endpoints used before release.",
        ),
        FieldSpec(
            key="extension_installer_origin",
            label="Extension installer origin",
            type=FieldType.TEXT,
            default="http://fs-extension-installer:8099",
            option_name="--extension-installer-origin",
            advanced=True,
            description="Validated internal HTTP origin used by the app proxy.",
        ),
        FieldSpec(
            key="certificate_path",
            label="Certificate path",
            type=FieldType.PATH,
            default=".ssl/cert.pem",
            option_name="--certificate-path",
            advanced=True,
            description="Host path for generated or provided certificate.",
        ),
        FieldSpec(
            key="private_key_path",
            label="Private key path",
            type=FieldType.PATH,
            default=".ssl/key.pem",
            option_name="--private-key-path",
            advanced=True,
            description="Host path for generated or provided private key.",
        ),
    ),
    input_model=InstallConfig,
    result_model=InstallRunResult,
    plan_builder_ref=None,
    confirmation_policy_ref=None,
    service_ref=_TOKEN_INSTALLER_RUN,
    result_renderer_ref=_TOKEN_INSTALLER_RUN,
)
