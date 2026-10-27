from __future__ import annotations


def test_runtime_env_preserves_mapping_and_getenv_semantics(monkeypatch) -> None:
    from core.infrastructure.config.runtime_env import runtime_env

    monkeypatch.setenv("RUNTIME_ENV_ADAPTER_TEST", "present")
    assert runtime_env.get("RUNTIME_ENV_ADAPTER_TEST") == "present"
    assert runtime_env.getenv("RUNTIME_ENV_ADAPTER_TEST") == "present"
    assert runtime_env["RUNTIME_ENV_ADAPTER_TEST"] == "present"
    assert runtime_env.get("RUNTIME_ENV_ADAPTER_MISSING", "fallback") == "fallback"
    copied = runtime_env.copy()
    assert isinstance(copied, dict)
    assert copied["RUNTIME_ENV_ADAPTER_TEST"] == "present"


def test_runtime_env_mutations_update_the_process_environment(monkeypatch) -> None:
    from core.infrastructure.config.runtime_env import runtime_env

    monkeypatch.delenv("RUNTIME_ENV_ADAPTER_MUTATION", raising=False)
    runtime_env["RUNTIME_ENV_ADAPTER_MUTATION"] = "updated"
    assert runtime_env.getenv("RUNTIME_ENV_ADAPTER_MUTATION") == "updated"
    del runtime_env["RUNTIME_ENV_ADAPTER_MUTATION"]
    assert runtime_env.getenv("RUNTIME_ENV_ADAPTER_MUTATION") is None
