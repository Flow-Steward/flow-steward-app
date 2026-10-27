"""Private-network LLM provider endpoints are an administrator's decision.

``FS_ALLOW_PRIVATE_REMOTE_URLS`` off (the default): only public HTTPS, and a
private address is refused with a message naming the setting. On: loopback and
RFC1918/ULA addresses may use http or https, public addresses still need https,
and link-local / cloud-metadata addresses stay refused either way. The decision
reaches an in-process provider as ``runtime_context.network_policy``.
"""

from __future__ import annotations

import pytest

from core.application.extensions import url_safety


@pytest.fixture
def dns(monkeypatch):
    answers: dict[str, set[str]] = {
        "ollama.example.com": {"93.184.216.34"},
        "ollama.lan": {"192.168.1.20"},
        "host.docker.internal": {"fdc4:f303:9324::254"},
        "split.example.com": {"93.184.216.34", "10.0.0.5"},
        "metadata.example.com": {"169.254.169.254"},
    }

    def resolve(host, *, port):
        return set(answers[host])

    monkeypatch.setattr(url_safety._descriptor_io, "_resolve_hostname_ips", resolve)
    return answers


def _check(url: str, *, allow: bool) -> str:
    return url_safety.assert_safe_provider_url(
        url, purpose="Base URL", allow_private_addresses=allow
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11434",
        "http://127.0.0.1:11434",
        "http://192.168.1.20:11434",
        "http://ollama.lan:11434",
        "https://ollama.lan",
        "http://host.docker.internal:11434",
    ],
)
def test_private_addresses_are_refused_by_default_and_name_the_setting(
    monkeypatch, dns, url
) -> None:
    monkeypatch.delenv("FS_ALLOW_PRIVATE_REMOTE_URLS", raising=False)

    with pytest.raises(ValueError, match="FS_ALLOW_PRIVATE_REMOTE_URLS=1"):
        _check(url, allow=False)


def test_public_https_is_accepted_by_default(dns) -> None:
    assert _check("https://ollama.example.com", allow=False) == "https://ollama.example.com"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:11434",
        "http://[::1]:11434",
        "http://10.1.2.3:11434",
        "http://ollama.lan:11434",
        "https://ollama.lan",
        "http://host.docker.internal:11434",
        "https://ollama.example.com",
    ],
)
def test_with_the_setting_private_http_and_public_https_are_accepted(dns, url) -> None:
    assert _check(url, allow=True) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://ollama.example.com",  # public needs TLS even with the setting on
        "http://split.example.com",  # one public answer is enough to need TLS
    ],
)
def test_with_the_setting_public_http_is_still_refused(dns, url) -> None:
    with pytest.raises(ValueError, match="must use https:// for a public address"):
        _check(url, allow=True)


@pytest.mark.parametrize("allow", [False, True])
@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest",
        "https://169.254.169.254",
        "http://[fe80::1]:11434",
        "https://metadata.example.com",
        "http://100.64.0.1",  # CGNAT
        "http://0.0.0.0:11434",
    ],
)
def test_link_local_and_metadata_addresses_are_refused_either_way(dns, url, allow) -> None:
    with pytest.raises(ValueError):
        _check(url, allow=allow)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://192.168.1.20",
        "http://user:pass@192.168.1.20",  # pragma: allowlist secret - URL-credential test input
        "http://",
    ],
)
def test_with_the_setting_the_basic_url_rules_still_hold(dns, url) -> None:
    with pytest.raises(ValueError):
        _check(url, allow=True)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1", True), ("true", True), ("on", True), ("0", False), ("", False)],
)
def test_the_switch_is_the_existing_private_urls_setting(monkeypatch, raw, expected) -> None:
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", raw)

    assert url_safety.allow_private_provider_addresses() is expected


@pytest.mark.parametrize("allow", [False, True])
def test_the_subprocess_receives_the_hosts_network_decision(monkeypatch, allow) -> None:
    from core.application.settings import llm_provider_extension_service as svc

    captured: dict = {}
    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.extension_runtime_helpers."
        "resolve_extension_runtime_record_by_install_id",
        lambda install_id, **_k: ({"install_id": install_id}, None),
    )
    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.extension_runtime_entrypoints.run_extension_query",
        lambda _extension, **kwargs: (
            captured.update(kwargs)
            or {"status": "succeeded", "response": {"ok": True, "result": {}}}
        ),
    )
    monkeypatch.setattr(url_safety, "allow_private_provider_addresses", lambda: allow)

    svc.invoke_llm_provider_operation(
        {"install_id": "pi_acme", "runtime_mode": "subprocess"},
        op_kind="query",
        op_id="llm.list_models",
        payload={},
        account_id="acc-1",
    )

    assert captured["runtime_context"]["network_policy"] == {"allow_private_addresses": allow}


def test_saving_a_private_base_url_without_the_setting_explains_how_to_allow_it(
    monkeypatch, dns
) -> None:
    from core.application.settings.llm_provider_extension_use_cases import (
        LlmProviderExtensionError,
        LlmProviderExtensionUseCases,
    )

    monkeypatch.setattr(url_safety, "allow_private_provider_addresses", lambda: False)

    with pytest.raises(LlmProviderExtensionError) as caught:
        LlmProviderExtensionUseCases._safe_upstream_config(
            {"upstream_base_url": "http://192.168.1.20:11434"}
        )

    assert caught.value.status_code == 422
    assert "FS_ALLOW_PRIVATE_REMOTE_URLS=1" in caught.value.detail


def test_saving_a_private_base_url_with_the_setting_keeps_it(monkeypatch, dns) -> None:
    from core.application.settings.llm_provider_extension_use_cases import (
        LlmProviderExtensionUseCases,
    )

    monkeypatch.setattr(url_safety, "allow_private_provider_addresses", lambda: True)

    assert LlmProviderExtensionUseCases._safe_upstream_config(
        {"upstream_base_url": "http://host.docker.internal:11434"}
    ) == {"upstream_base_url": "http://host.docker.internal:11434"}


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://ollama.example.com", "Base URL must use https:// for a public address"),
        ("http://169.254.169.254", "a link-local, metadata or reserved address"),
    ],
)
def test_saving_a_refused_base_url_says_why(monkeypatch, dns, url, reason) -> None:
    from core.application.settings.llm_provider_extension_use_cases import (
        LlmProviderExtensionError,
        LlmProviderExtensionUseCases,
    )

    monkeypatch.setattr(url_safety, "allow_private_provider_addresses", lambda: True)

    with pytest.raises(LlmProviderExtensionError) as caught:
        LlmProviderExtensionUseCases._safe_upstream_config({"upstream_base_url": url})

    assert caught.value.status_code == 422
    assert reason in caught.value.detail


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("ftp://ollama.example.com", "Base URL must start with https://"),
        ("https://", "Base URL needs a host name"),
        (
            "https://no-such-host.invalid",
            "Base URL: the host 'no-such-host.invalid' can't be found",
        ),
    ],
)
def test_refusals_name_the_field_and_a_plain_cause(monkeypatch, url, message) -> None:
    def resolve(host, *, port):
        raise OSError("[Errno -2] Name or service not known")

    monkeypatch.setattr(url_safety._descriptor_io, "_resolve_hostname_ips", resolve)

    with pytest.raises(ValueError) as caught:
        _check(url, allow=False)

    assert str(caught.value) == message
    assert "Errno" not in str(caught.value)
    assert "upstream_base_url" not in str(caught.value)
