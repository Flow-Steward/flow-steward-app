"""Validate public hosts and supplied TLS certificate chains."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from ipaddress import ip_address
from itertools import pairwise
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization


def _valid_public_host(value: str) -> bool:
    if (
        "%" in value
        or value != value.strip()
        or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in value
        )
    ):
        return False
    try:
        ip_address(value)
        return True
    except ValueError:
        pass
    if len(value) > 253 or value.endswith("."):
        return False
    return all(
        re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
        for label in value.split(".")
    )


def _provided_tls_issues(
    public_host: str,
    *,
    cert_path: Path,
    key_path: Path,
) -> tuple[str, ...]:
    try:
        certificates = x509.load_pem_x509_certificates(cert_path.read_bytes())
        if not certificates:
            raise ValueError("certificate chain is empty")
        certificate = certificates[0]
        private_key = serialization.load_pem_private_key(
            key_path.read_bytes(),
            password=None,
        )
    except (OSError, TypeError, ValueError):
        return ("provided TLS certificate or private key is not valid unencrypted PEM",)
    certificate_public = certificate.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    private_public = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    issues: list[str] = []
    if certificate_public != private_public:
        issues.append("provided TLS certificate does not match the private key")
    now = datetime.now(UTC)
    if not (certificate.not_valid_before_utc <= now < certificate.not_valid_after_utc):
        issues.append("provided TLS certificate is not valid at the current time")
    for child, issuer in pairwise(certificates):
        if not (issuer.not_valid_before_utc <= now < issuer.not_valid_after_utc):
            issues.append("provided TLS certificate chain contains an expired issuer")
            break
        try:
            child.verify_directly_issued_by(issuer)
        except ValueError:
            issues.append("provided TLS certificate chain is not ordered or valid")
            break
    try:
        san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        issues.append("provided TLS certificate has no subject alternative name")
    else:
        try:
            expected_ip = ip_address(public_host)
        except ValueError:
            dns_names = {name.casefold() for name in san.get_values_for_type(x509.DNSName)}
            if public_host.casefold() not in dns_names:
                issues.append("provided TLS certificate SAN does not include the public host")
        else:
            if expected_ip not in set(san.get_values_for_type(x509.IPAddress)):
                issues.append("provided TLS certificate SAN does not include the public IP")
    return tuple(issues)
