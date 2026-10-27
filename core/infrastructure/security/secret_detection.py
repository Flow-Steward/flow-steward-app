"""Canonical credential-pattern policy shared by scanners and public projections."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class SecretPattern:
    pattern_id: str
    regex: re.Pattern[str]
    severity: str
    description: str
    replacement: str = ""


_PRIVATE_KEY_BLOCK_LABEL = (
    r"(?:PRIVATE KEY|RSA PRIVATE KEY|EC PRIVATE KEY|DSA PRIVATE KEY|"
    r"OPENSSH PRIVATE KEY|ENCRYPTED PRIVATE KEY|PGP PRIVATE KEY BLOCK)"
)

_CANONICAL_PROJECTED_REGISTRY_PREFIX = (
    r"extension\."
    r"(?:pi_[0-9a-fA-F]{24}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\."
)


SECRET_PATTERNS: tuple[SecretPattern, ...] = (
    SecretPattern(
        "basic_auth",
        re.compile(r"(?i)\b(Basic\s+[A-Z0-9+/]{4,}={0,2})(?![A-Z0-9+/=])"),
        "high",
        "HTTP Basic authorization credential",
    ),
    SecretPattern(
        "aws_access_key",
        re.compile(r"(?<![A-Za-z0-9/+])(AKIA[0-9A-Z]{16})(?![A-Za-z0-9/+=])"),
        "critical",
        "AWS Access Key ID",
    ),
    SecretPattern(
        "aws_secret_key",
        re.compile(
            r"(?i)(?:aws_secret_access_key|aws_secret_key)\s*[:=]\s*[\"']?([a-z0-9/+=]{40})[\"']?"
        ),
        "critical",
        "AWS Secret Access Key",
    ),
    SecretPattern(
        "github_pat",
        re.compile(r"\b(ghp_[A-Za-z0-9]{36,})\b"),
        "critical",
        "GitHub Personal Access Token",
    ),
    SecretPattern(
        "github_fine_grained",
        re.compile(r"\b(github_pat_(?a:\w){22,})\b"),
        "critical",
        "GitHub Fine-Grained PAT",
    ),
    SecretPattern(
        "github_token",
        re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,})\b"),
        "critical",
        "GitHub access token",
    ),
    SecretPattern(
        "gitlab_pat",
        re.compile(r"\b(glpat-[A-Za-z0-9\-_]{20,})\b"),
        "critical",
        "GitLab Personal Access Token",
    ),
    SecretPattern(
        "slack_bot_token",
        re.compile(r"\b(xoxb-\d{10,}-\d{10,}-[A-Za-z0-9]{24,})\b"),
        "high",
        "Slack Bot Token",
    ),
    SecretPattern(
        "slack_token",
        re.compile(r"\b(xox[baprs]-[A-Za-z0-9-]{10,})\b"),
        "high",
        "Slack access token",
    ),
    SecretPattern(
        "slack_webhook",
        re.compile(r"(https://hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]+)"),
        "high",
        "Slack Webhook URL",
    ),
    SecretPattern(
        "stripe_secret",
        re.compile(r"\b(sk_live_[A-Za-z0-9]{24,})\b"),
        "critical",
        "Stripe Secret Key",
    ),
    SecretPattern(
        "stripe_publishable",
        re.compile(r"\b(pk_live_[A-Za-z0-9]{24,})\b"),
        "medium",
        "Stripe Publishable Key",
    ),
    SecretPattern(
        "sendgrid_key",
        re.compile(r"\b(SG\.(?a:[\w-]){22,}\.(?a:[\w-]){22,})\b"),
        "critical",
        "SendGrid API Key",
    ),
    SecretPattern("twilio_key", re.compile(r"\b(SK[0-9a-f]{32})\b"), "high", "Twilio API Key"),
    SecretPattern(
        "gcp_api_key", re.compile(r"\b(AIza(?a:[\w-]){35})\b"), "high", "Google Cloud API Key"
    ),
    SecretPattern(
        "azure_connection_string",
        re.compile(
            r"(DefaultEndpointsProtocol=https?;AccountName=[^;]+;AccountKey=[A-Za-z0-9+/=]+;?[^\s]*)"
        ),
        "critical",
        "Azure Storage Connection String",
    ),
    SecretPattern(
        "jwt_token",
        re.compile(r"\b(eyJ(?a:[\w-]){20,}\.eyJ(?a:[\w-]){20,}\.(?a:[\w-]){20,})\b"),
        "high",
        "JSON Web Token",
    ),
    SecretPattern(
        "pem_private_key",
        re.compile(
            rf"(-----BEGIN (?P<private_key_label>{_PRIVATE_KEY_BLOCK_LABEL})-----"
            r".*?(?:-----END (?P=private_key_label)-----|\Z))",
            re.DOTALL,
        ),
        "critical",
        "PEM Private Key Block",
    ),
    SecretPattern(
        "db_uri",
        re.compile(
            r"\b((?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^\s'\"<>]{10,})\b"
        ),
        "high",
        "Database Connection URI",
    ),
    SecretPattern(
        "url_userinfo",
        re.compile(r"(?i)\b(https?://[^:@/\s]+:[^@/\s]+@[^\s'\"<>]+)"),
        "critical",
        "HTTP URL containing user credentials",
    ),
    SecretPattern(
        "generic_api_key",
        re.compile(
            r"(?i)(?:(?:api|secret)[_-]?key|api[_-]?secret)\s*[:=]\s*[\"']?([a-z0-9_-]{20,})[\"']?"
        ),
        "medium",
        "Generic API Key Assignment",
    ),
    SecretPattern(
        "provider_token",
        re.compile(
            r"\b((?:(?:sk|rk)_(?:live|test)_)[A-Za-z0-9_-]{6,}|"
            r"(?:sk-(?:proj-|ant-)?|hf_|npm_|pypi-)[A-Za-z0-9_.\-]{16,})\b"
        ),
        "high",
        "Provider access token",
    ),
    SecretPattern(
        "dotted_provider_token",
        re.compile(
            rf"(?<![A-Za-z0-9_-])(?<!process\.)(?!{_CANONICAL_PROJECTED_REGISTRY_PREFIX})"
            r"([A-Za-z][A-Za-z0-9_-]{1,15}\.[A-Za-z0-9_-]{24,})"
            r"(?![A-Za-z0-9_-])"
        ),
        "high",
        "Dotted provider access token",
    ),
    SecretPattern(
        "bearer_auth",
        re.compile(r"(?i)\bBearer\s+[A-Z0-9._~+/=-]+"),
        "high",
        "HTTP Bearer authorization credential",
        "Bearer [REDACTED]",
    ),
    SecretPattern(
        "credential_assignment",
        re.compile(
            r"(?ix)(?P<key_quote>[\"']?)"
            r"(?P<key>api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|"
            r"token|secret|password|passwd|pwd|credential)"
            r"(?P=key_quote)\s*(?P<separator>[:=])\s*"
            r"(?:"
            r"(?P<value_quote>[\"'])(?:\\.|(?!(?P=value_quote)).)*(?P=value_quote)"
            r"|[^\r\n,;}]+"
            r")"
        ),
        "medium",
        "Assigned credential value",
        r"\g<key_quote>\g<key>\g<key_quote>\g<separator>"
        r"\g<value_quote>[REDACTED]\g<value_quote>",
    ),
)


def redact_detected_secrets(value: str) -> str:
    result = str(value or "")
    for pattern in reversed(SECRET_PATTERNS):
        replacement = pattern.replacement or f"[SECRET:{pattern.pattern_id}:REDACTED]"
        result = pattern.regex.sub(replacement, result)
    return result


def contains_detected_secret(value: str) -> bool:
    text = str(value or "")
    return any(pattern.regex.search(text) is not None for pattern in SECRET_PATTERNS)


__all__ = [
    "SECRET_PATTERNS",
    "SecretPattern",
    "contains_detected_secret",
    "redact_detected_secrets",
]
