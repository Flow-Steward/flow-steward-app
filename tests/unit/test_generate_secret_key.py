from __future__ import annotations

import re
from pathlib import Path

from core.application.tooling.ops import generate_secret_key


def test_generate_secret_key_defaults_to_256_bit_hex() -> None:
    value = generate_secret_key.generate_secret_key()

    assert len(value) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", value)


def test_generate_secret_key_rejects_small_byte_count() -> None:
    try:
        generate_secret_key.generate_secret_key(byte_count=16)
    except ValueError as exc:
        assert "at least 32 random bytes" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("small key generation should fail")


def test_write_env_value_creates_or_fills_blank_value(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=sk-test\nENCRYPTION_KEY=\n", encoding="utf-8")

    status = generate_secret_key.write_env_value(env_file, "ENCRYPTION_KEY", "a" * 64)

    assert status == "updated"
    assert env_file.read_text(encoding="utf-8") == (
        "OPENAI_API_KEY=sk-test\nENCRYPTION_KEY=" + "a" * 64 + "\n"
    )


def test_write_env_value_preserves_existing_secret_without_force(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("ENCRYPTION_KEY=keep-me\n", encoding="utf-8")

    status = generate_secret_key.write_env_value(env_file, "ENCRYPTION_KEY", "b" * 64)

    assert status == "exists"
    assert env_file.read_text(encoding="utf-8") == "ENCRYPTION_KEY=keep-me\n"


def test_main_writes_env_file_without_printing_secret(tmp_path: Path, capsys) -> None:
    env_file = tmp_path / ".env"

    assert generate_secret_key.main(["--env-file", str(env_file)]) == 0

    output = capsys.readouterr().out
    text = env_file.read_text(encoding="utf-8")
    value = text.split("=", 1)[1].strip()
    assert output == f"Created ENCRYPTION_KEY in {env_file}.\n"
    assert value not in output
    assert re.fullmatch(r"[0-9a-f]{64}", value)


def test_direct_secret_generator_raw_path(capsys) -> None:
    result = generate_secret_key.main(["--raw"])

    assert result == 0
    assert re.fullmatch(r"[0-9a-f]{64}\n", capsys.readouterr().out)
