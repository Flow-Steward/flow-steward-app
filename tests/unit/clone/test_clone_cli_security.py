import io

import pytest

from core.application.management.command_specs.clone import CLONE_COMMAND_SPECS
from core.entrypoints.cli.platform.service_bindings.clone import _passphrase_source


def test_clone_cli_accepts_passphrases_only_from_prompt_or_dedicated_stdin() -> None:
    serialized = " ".join(
        item
        for spec in CLONE_COMMAND_SPECS
        for item in (
            *spec.examples,
            *(str(field.option_name or "") for field in spec.fields),
        )
    )

    assert "--passphrase-stdin" in serialized
    assert "--passphrase " not in serialized
    assert "passphrase_file" not in serialized
    assert all("passphrase" not in spec.input_model.model_fields for spec in CLONE_COMMAND_SPECS)


def test_clone_cli_has_no_inspect_verify_promote_or_switch_surface() -> None:
    assert {spec.path for spec in CLONE_COMMAND_SPECS} == {
        ("clone", "create"),
        ("clone", "restore"),
    }


def test_stdin_passphrase_supports_text_stream_without_serializing_secret(monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("private value\n"))

    source = _passphrase_source(True)

    assert source() == b"private value"
    assert source() == b"private value"


@pytest.mark.parametrize("binary", [False, True])
def test_stdin_passphrase_is_reserved_before_capture_drains_stream(monkeypatch, binary) -> None:
    stream = (
        io.TextIOWrapper(io.BytesIO(b"reserved private value\r\nremaining input\n"))
        if binary
        else io.StringIO("reserved private value\r\nremaining input\n")
    )
    monkeypatch.setattr("sys.stdin", stream)

    source = _passphrase_source(True)
    # Source capture may consume inherited stdin before archive assembly.
    getattr(stream, "buffer", stream).read()

    assert source() == b"reserved private value"
    assert source() == b"reserved private value"


def test_prompt_passphrase_remains_lazy_and_cached(monkeypatch) -> None:
    prompts = []

    def prompt(label):
        prompts.append(label)
        return b"prompt private value"

    monkeypatch.setattr(
        "core.entrypoints.cli.platform.service_bindings.clone.read_hidden_secret", prompt
    )
    source = _passphrase_source(False)
    assert prompts == []

    assert source() == b"prompt private value"
    assert source() == b"prompt private value"
    assert len(prompts) == 1
