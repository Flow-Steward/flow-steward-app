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
                "Production runs the application. Development is for contributors "
                "and requires restarting with --from-source."
            ),
        ),
        FieldSpec(
            key="deployment_profile",
            label="How to run Flow Steward",
            type=FieldType.SINGLE_SELECT,
            choices=(
                ChoiceItem(
                    value="compact",
                    label="Compact — app and background workers together; fewer resources",
                ),
                ChoiceItem(
                    value="scalable",
                    label="Scalable — separate services; scale workers independently",
                ),
            ),
            default="compact",
            option_name="--deployment-profile",
            description=(
                "Compact keeps the app and background workers together and uses fewer "
                "resources. Scalable separates services so workers can grow independently."
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
            label="Website access",
            type=FieldType.SINGLE_SELECT,
            choices=(
                ChoiceItem(value="bundled_traefik", label="Let Flow Steward publish the site"),
                ChoiceItem(value="external_traefik", label="Use my existing Traefik"),
                ChoiceItem(value="external_proxy", label="Use my existing Nginx or Apache"),
                ChoiceItem(value="platform", label="My hosting platform handles website access"),
            ),
            default="bundled_traefik",
            option_name="--ingress-mode",
            description="Choose how people will reach this website.",
        ),
        FieldSpec(
            key="worker_processes",
            label="Compact worker processes",
            type=FieldType.INTEGER,
            default=2,
            option_name="--worker-processes",
            visible_if={"deployment_profile": "compact"},
            description="Number of queue-worker processes inside the Compact runtime.",
            advanced=True,
        ),
        FieldSpec(
            key="file_storage",
            label="File storage",
            type=FieldType.SINGLE_SELECT,
            choices=(
                ChoiceItem(
                    value="bundled", label="Included SeaweedFS — managed with this installation"
                ),
                ChoiceItem(value="external_s3", label="My own S3-compatible storage"),
            ),
            default="bundled",
            option_name="--file-storage",
            description=(
                "SeaweedFS is included by default. Choose external storage only if you "
                "already have an S3-compatible service."
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
            advanced=True,
        ),
        FieldSpec(
            key="tls_mode",
            label="HTTPS certificate",
            type=FieldType.SINGLE_SELECT,
            choices=(
                ChoiceItem(
                    value="local",
                    label="Local/private trial — generated certificate, browser warning",
                ),
                ChoiceItem(
                    value="self_signed", label="Generated certificate for my hostname (advanced)"
                ),
                ChoiceItem(value="provided", label="Use my certificate files"),
                ChoiceItem(value="acme", label="Public HTTPS — free Let's Encrypt certificate"),
                ChoiceItem(value="reverse_proxy", label="Existing reverse-proxy TLS"),
                ChoiceItem(value="external_resolver", label="Traefik obtains the certificate"),
                ChoiceItem(value="external_certificate", label="Use my certificate in Traefik"),
                ChoiceItem(value="platform_managed", label="Hosting platform TLS"),
            ),
            default="local",
            option_name="--tls-mode",
            description="How the public endpoint receives TLS.",
        ),
        FieldSpec(
            key="public_host",
            label="Website domain or IP",
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
            advanced=True,
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
            key="operator_settings",
            label="Host settings",
            type=FieldType.TEXT,
            option_name="--setting",
            advanced=True,
            # A command-line option, not a question: the plain questionnaire never asks it.
            deferred_until_plan=True,
            examples=("FS_ALLOW_PRIVATE_REMOTE_URLS=1",),
            description=(
                "NAME=VALUE for a host setting an error message names, such as "
                "FS_ALLOW_PRIVATE_REMOTE_URLS=1; a comma followed by NAME= starts another "
                "setting, while other commas remain in the value. Leave "
                "VALUE empty to reset one to its default. On an existing installation it "
                "changes only those settings and restarts the runtime."
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
            advanced=True,
        ),
        FieldSpec(
            key="reverse_proxy_backend_port",
            label="Reverse-proxy backend port",
            type=FieldType.INTEGER,
            default=18099,
            option_name="--reverse-proxy-backend-port",
            visible_if={"tls_mode": "reverse_proxy"},
            description="Private HTTP port used only by the existing host reverse proxy.",
            advanced=True,
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
            label="Where does your web server run?",
            type=FieldType.SINGLE_SELECT,
            choices=(
                ChoiceItem(value="host", label="On this server"),
                ChoiceItem(value="docker", label="In a Docker container"),
            ),
            default="host",
            option_name="--proxy-placement",
            visible_if={"ingress_mode": "external_proxy"},
            description="Whether the selected existing proxy runs on the host or in Docker.",
        ),
        FieldSpec(
            key="proxy_kind",
            label="Which web server do you use?",
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
            label="Email for certificate renewal notices",
            type=FieldType.TEXT,
            option_name="--acme-contact",
            visible_if={"tls_mode": "acme"},
            description="Contact email used for bundled ACME registration.",
        ),
        FieldSpec(
            key="acme_directory_url",
            default="https://acme-v02.api.letsencrypt.org/directory",
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
