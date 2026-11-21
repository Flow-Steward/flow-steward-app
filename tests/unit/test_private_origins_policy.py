"""Exact private origins an operator allows for provider endpoints (ADR 0078, #31).

``FS_ALLOWED_PRIVATE_ORIGINS`` admits one ``scheme://host:port`` at a time. It never
widens to other hosts, other ports or the other scheme, and the address rules that
hold with ``FS_ALLOW_PRIVATE_REMOTE_URLS=1`` (link-local and metadata refused, http
only to private answers) still apply to an allowed origin.
"""

from __future__ import annotations

import pytest

from core.application.extensions import url_safety
from core.infrastructure.config import private_origins

ALLOWED = "http://host.docker.internal:11434"


@pytest.fixture
def dns(monkeypatch):
    answers: dict[str, set[str]] = {
        "host.docker.internal": {"192.168.65.254"},
        "localhost": {"127.0.0.1"},
        "ollama.lan": {"192.168.1.20"},
        "metadata.lan": {"169.254.169.254"},
        "rebind.lan": {"192.168.1.20", "169.254.169.254"},
        "public.lan": {"93.184.216.34"},
    }

    def resolve(host, *, port):
        return set(answers[host])

    monkeypatch.setattr(url_safety._descriptor_io, "_resolve_hostname_ips", resolve)
    monkeypatch.setattr(url_safety, "_allow_private_remote_urls", lambda: False)
    return answers


def _check(url: str, *allowed: str) -> str:
    return url_safety.assert_safe_provider_url(
        url, purpose="Base URL", allow_private_addresses=False, allowed_origins=allowed
    )


# --- the setting's value -------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ()),
        ("http://host.docker.internal:11434", (ALLOWED,)),
        ("HTTP://Host.Docker.Internal:11434/", (ALLOWED,)),
        (
            "http://host.docker.internal:11434, https://10.0.0.5:8443 http://[::1]:1234",
            (ALLOWED, "https://10.0.0.5:8443", "http://[::1]:1234"),
        ),
        ("http://ollama:11434,http://ollama:11434", ("http://ollama:11434",)),
        ("http://vllm_server:8000", ("http://vllm_server:8000",)),
        ("http://[fd00::5]:80", ("http://[fd00::5]:80",)),
    ],
)
def test_valid_values_are_canonical_origins(raw, expected) -> None:
    assert private_origins.parse_allowed_private_origins(raw) == expected


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("http://host.docker.internal", "needs its port"),
        ("http://host.docker.internal:0", "needs its port"),
        ("http://host.docker.internal:11434/v1", "without a path"),
        ("http://host.docker.internal:11434?x=1", "without a path"),
        ("http://host.docker.internal:11434#x", "without a path"),
        # pragma: allowlist nextline secret
        ("http://user:pw@host.docker.internal:11434", "user name or password"),
        ("ftp://host.docker.internal:21", "http:// or https://"),
        ("host.docker.internal:11434", "http:// or https://"),
        ("http://*.lan:11434", "wildcard"),
        ("http://93.184.216.34:80", "not a loopback or private address"),
        ("http://169.254.169.254:80", "not a loopback or private address"),
        ("http://[fe80::1]:80", "not a loopback or private address"),
        ("http://[::ffff:10.0.0.5]:80", "not a loopback or private address"),
        ("http://100.64.0.1:80", "not a loopback or private address"),
        ("http://0.0.0.0:11434", "not a loopback or private address"),
        ("http://127.1:11434", "neither an IP address nor a host name"),
        ("http://bad!host:1", "does not name a valid host"),
        ("http://host.docker.internal:99999", "not an origin"),
    ],
)
def test_invalid_values_are_refused_with_the_reason(raw, reason) -> None:
    with pytest.raises(private_origins.PrivateOriginError, match=reason):
        private_origins.parse_allowed_private_origins(raw)


def test_the_list_is_bounded() -> None:
    raw = ",".join(f"http://10.0.0.{index}:80" for index in range(1, 18))
    with pytest.raises(private_origins.PrivateOriginError, match="at most 16"):
        private_origins.parse_allowed_private_origins(raw)


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("http://host.docker.internal:11434/api/tags", ALLOWED),
        ("https://ollama.lan", "https://ollama.lan:443"),
        ("http://ollama.lan", "http://ollama.lan:80"),
        ("http://[::1]:11434/v1", "http://[::1]:11434"),
        ("ftp://ollama.lan", None),
        ("http://user@ollama.lan:1", None),
    ],
)
def test_a_url_origin_fills_in_the_schemes_port(url, origin) -> None:
    assert private_origins.url_origin(url) == origin


def test_the_runtime_reads_the_setting_and_ignores_an_invalid_value(monkeypatch) -> None:
    monkeypatch.setenv(private_origins.SETTING, f"{ALLOWED},http://10.0.0.5:8000")
    assert private_origins.allowed_private_origins() == (ALLOWED, "http://10.0.0.5:8000")

    # A hand-edited value that does not parse allows nothing (fail closed).
    monkeypatch.setenv(private_origins.SETTING, f"{ALLOWED},http://169.254.169.254:80")
    assert private_origins.allowed_private_origins() == ()

    monkeypatch.delenv(private_origins.SETTING)
    assert private_origins.allowed_private_origins() == ()


def test_config_set_refuses_an_invalid_value_and_accepts_a_valid_one() -> None:
    from core.application.lifecycle.operator_settings import OperatorSettingError
    from core.application.lifecycle.setting_names import check_operator_settings

    with pytest.raises(OperatorSettingError, match=r"FS_ALLOWED_PRIVATE_ORIGINS: .*needs its port"):
        check_operator_settings({"FS_ALLOWED_PRIVATE_ORIGINS": "http://host.docker.internal"})

    assert check_operator_settings({"FS_ALLOWED_PRIVATE_ORIGINS": ALLOWED}) == {
        "FS_ALLOWED_PRIVATE_ORIGINS": ALLOWED
    }
    # An empty value resets the setting to its default (none allowed).
    assert check_operator_settings({"FS_ALLOWED_PRIVATE_ORIGINS": ""}) == {
        "FS_ALLOWED_PRIVATE_ORIGINS": ""
    }


# --- the provider URL guard ------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        (ALLOWED, ALLOWED),
        (f"{ALLOWED}/", ALLOWED),
        ("http://localhost:11434", "http://localhost:11434"),
        ("https://ollama.lan", "https://ollama.lan:443"),
        ("http://192.168.1.20:11434", "http://192.168.1.20:11434"),
    ],
)
def test_the_exact_allowed_origin_is_accepted(dns, url, allowed) -> None:
    assert _check(url, allowed) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://host.docker.internal:11435",  # another port on the allowed host
        "http://host.docker.internal",  # the scheme's default port is another port
        "http://ollama.lan:11434",  # another private host
        "http://192.168.65.254:11434",  # the allowed host's address, by another name
        "http://localhost:11434",
        # Where a redirect from the allowed server could point: not covered either.
        "http://10.0.0.9:11434/api/tags",
    ],
)
def test_anything_but_the_exact_origin_stays_refused(dns, url) -> None:
    with pytest.raises(ValueError, match="points to a private network address") as refused:
        _check(url, ALLOWED)
    # The refusal offers to allow exactly the origin that was refused.
    origin = private_origins.url_origin(url)
    assert f"FS_ALLOWED_PRIVATE_ORIGINS={origin}" in str(refused.value)


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        # No downgrade: an https origin does not admit cleartext on the same port...
        ("http://ollama.lan:443", "https://ollama.lan:443"),
        # ...and an http origin does not admit https on the same port either.
        ("https://ollama.lan:11434", "http://ollama.lan:11434"),
    ],
)
def test_the_scheme_is_part_of_the_origin(dns, url, allowed) -> None:
    with pytest.raises(ValueError, match="points to a private network address"):
        _check(url, allowed)


@pytest.mark.parametrize(
    "url",
    [
        "http://metadata.lan:80",  # an allowed name that resolves to the metadata service
        "http://rebind.lan:11434",  # one private and one link-local answer
    ],
)
def test_an_allowed_name_never_reaches_link_local_or_metadata(dns, url) -> None:
    with pytest.raises(ValueError, match="never allowed"):
        _check(url, url)


def test_an_allowed_name_that_resolves_publicly_still_needs_tls(dns) -> None:
    with pytest.raises(ValueError, match="must use https:// for a public address"):
        _check("http://public.lan:8080", "http://public.lan:8080")


def test_the_policy_carries_the_allowlist_to_the_extension(monkeypatch) -> None:
    monkeypatch.setattr(url_safety, "_allow_private_remote_urls", lambda: False)
    monkeypatch.setenv(private_origins.SETTING, ALLOWED)

    assert url_safety.provider_network_policy() == {
        "allow_private_addresses": False,
        "allowed_private_origins": [ALLOWED],
    }


def test_saving_the_allowed_base_url_keeps_it_without_the_global_switch(monkeypatch, dns) -> None:
    from core.application.settings.llm_provider_extension_use_cases import (
        LlmProviderExtensionError,
        LlmProviderExtensionUseCases,
    )

    monkeypatch.setenv(private_origins.SETTING, ALLOWED)

    assert LlmProviderExtensionUseCases._safe_upstream_config({"upstream_base_url": ALLOWED}) == {
        "upstream_base_url": ALLOWED
    }
    with pytest.raises(LlmProviderExtensionError) as caught:
        LlmProviderExtensionUseCases._safe_upstream_config(
            {"upstream_base_url": "http://host.docker.internal:11435"}
        )
    assert caught.value.status_code == 422
    assert "FS_ALLOWED_PRIVATE_ORIGINS=http://host.docker.internal:11435" in caught.value.detail
