"""A base64 value from workflow state becomes a downloadable file."""

from __future__ import annotations

import base64
from typing import Any

import pytest

from core.application.extensions.artifact_plane_values import ArtifactHandle
from core.application.orchestration.file_from_base64_step_runtime import (
    FileFromBase64Error,
    decode_base64,
    execute_file_from_base64_step,
    sniff_content_type,
    validate_file_from_base64_config,
)
from core.application.orchestration.workflow_state_selector_contract import (
    discover_step_state_selectors,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
PDF = b"%PDF-1.7\n" + b"x" * 32


class _Plane:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create_artifact(self, **kwargs: Any) -> tuple[ArtifactHandle, dict[str, Any]]:
        self.calls.append(kwargs)
        return ArtifactHandle(artifact_id="art_1"), {
            "artifact_id": "art_1",
            "filename": kwargs["filename"],
            "sha256": "f" * 64,
        }


def _state(value: Any, plane: _Plane | None = None) -> dict[str, Any]:
    return {
        "account_id": "acc-1",
        "project_id": "proj-1",
        "job_id": "job-1",
        "workflow_id": "wf",
        "step_id": "save_image",
        "outputs": {"image": value},
        "_artifact_plane": plane or _Plane(),
    }


def _b64(body: bytes) -> str:
    return base64.b64encode(body).decode("ascii")


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (PNG, "image/png"),
        (JPEG, "image/jpeg"),
        (b"GIF89a" + b"\x00" * 10, "image/gif"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
        (PDF, "application/pdf"),
        (b"PK\x03\x04rest", "application/zip"),
        (b'<svg xmlns="http://www.w3.org/2000/svg"></svg>', "image/svg+xml"),
        (b"plain words", ""),
    ],
)
def test_the_file_type_is_recognised_from_its_bytes(body: bytes, expected: str) -> None:
    assert sniff_content_type(body) == expected


def test_standard_url_safe_and_unpadded_base64_all_decode() -> None:
    body = bytes(range(256))
    standard = _b64(body)
    url_safe = base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")

    assert decode_base64(standard, max_bytes=1024) == body
    assert decode_base64(url_safe, max_bytes=1024) == body


def test_invalid_base64_is_refused() -> None:
    with pytest.raises(FileFromBase64Error) as error:
        decode_base64("not base64 at all!", max_bytes=1024)
    assert error.value.reason == "invalid_base64"


def test_an_oversized_value_is_refused_before_it_is_decoded() -> None:
    with pytest.raises(FileFromBase64Error) as error:
        decode_base64("A" * 4000, max_bytes=100)
    assert error.value.reason == "file_too_large"


def test_an_image_generation_response_becomes_a_png_artifact() -> None:
    plane = _Plane()
    state = _state({"data": [{"b64_json": _b64(PNG)}]}, plane)

    result = execute_file_from_base64_step(
        {"source": "state.outputs.image.data[0].b64_json", "result_target": "state.outputs.f"},
        state,
    )

    assert result["ok"] is True
    assert result["artifact_handle"] == "artifact:art_1"
    assert result["content_type"] == "image/png"
    assert result["filename"] == "file.png"
    assert result["size_bytes"] == len(PNG)
    call = plane.calls[0]
    assert call["body"] == PNG
    assert call["owner_scope"]["project_id"] == "proj-1"
    assert call["owner_scope"]["job_id"] == "job-1"
    assert call["run_id"] == "job-1"
    assert call["retention_class"] == "run_window"


def test_a_data_url_declares_its_own_type_and_keeps_a_given_name() -> None:
    plane = _Plane()
    state = _state(f"data:image/jpeg;base64,{_b64(JPEG)}", plane)

    result = execute_file_from_base64_step(
        {"source": "state.outputs.image", "result_target": "x", "filename": "photo"}, state
    )

    assert result["content_type"] == "image/jpeg"
    assert result["filename"] == "photo.jpg"


def test_an_inline_data_object_carries_its_mime_type() -> None:
    state = _state({"mime_type": "application/pdf", "data": _b64(PDF)})

    result = execute_file_from_base64_step(
        {"source": "state.outputs.image", "result_target": "x", "filename": "report.pdf"}, state
    )

    assert result["ok"] is True
    assert result["content_type"] == "application/pdf"
    assert result["filename"] == "report.pdf"


def test_an_explicit_content_type_wins_and_unknown_bytes_fall_back_to_octet_stream() -> None:
    explicit = execute_file_from_base64_step(
        {"source": "state.outputs.image", "result_target": "x", "content_type": "text/csv"},
        _state(_b64(b"a,b\n1,2\n")),
    )
    unknown = execute_file_from_base64_step(
        {"source": "state.outputs.image", "result_target": "x"},
        _state(_b64(b"\x00\x01\x02 unknown")),
    )

    assert explicit["content_type"] == "text/csv"
    assert explicit["filename"] == "file.csv"
    assert unknown["content_type"] == "application/octet-stream"
    assert unknown["filename"] == "file"


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (None, "source_not_base64"),
        ({"other": "x"}, "source_not_base64"),
        ("data:text/plain,hello", "source_not_base64"),
        ("%%%", "invalid_base64"),
    ],
)
def test_a_source_that_is_not_base64_fails_the_step(value: Any, reason: str) -> None:
    plane = _Plane()
    result = execute_file_from_base64_step(
        {"source": "state.outputs.image", "result_target": "x"}, _state(value, plane)
    )

    assert result["ok"] is False
    assert result["failure_reason"] == reason
    assert plane.calls == []


def test_a_path_in_the_file_name_is_refused() -> None:
    result = execute_file_from_base64_step(
        {"source": "state.outputs.image", "result_target": "x", "filename": "../etc/passwd"},
        _state(_b64(PNG)),
    )

    assert result["failure_reason"] == "invalid_filename"


def test_the_size_limit_is_applied_to_the_decoded_file() -> None:
    result = execute_file_from_base64_step(
        {"source": "state.outputs.image", "result_target": "x", "max_bytes": 10},
        _state(_b64(PNG)),
    )

    assert result["failure_reason"] == "file_too_large"


def test_authoring_issues_name_the_missing_and_invalid_fields() -> None:
    issues = validate_file_from_base64_config({"max_bytes": 0}, path_prefix="step")
    assert {issue["path"] for issue in issues} == {
        "step.source",
        "step.result_target",
        "step.max_bytes",
    }
    ok = validate_file_from_base64_config(
        {"source": "state.a", "result_target": "state.b", "max_bytes": 10**12},
        path_prefix="step",
    )
    assert ok == []


def test_the_operator_limit_caps_every_step_and_a_step_can_only_lower_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FS_BASE64_FILE_MAX_BYTES", "2048")
    big = _b64(PNG + b"\x00" * 4096)

    capped = execute_file_from_base64_step(
        {"source": "state.outputs.image", "result_target": "x", "max_bytes": 10**9},
        _state(big),
    )
    within = execute_file_from_base64_step(
        {"source": "state.outputs.image", "result_target": "x"}, _state(_b64(PNG))
    )

    assert capped["failure_reason"] == "file_too_large"
    assert within["ok"] is True


def test_the_source_selector_is_checked_like_any_other_state_reference() -> None:
    references = discover_step_state_selectors(
        {
            "step_kind": "file_from_base64",
            "override": {
                "file_from_base64": {
                    "source": "state.outputs.image.b64_json",
                    "result_target": "state.outputs.file",
                }
            },
        }
    )

    assert [ref.path for ref in references] == ["step.override.file_from_base64.source"]


@pytest.mark.parametrize("valid", [True, False])
def test_file_builtin_summary_localization_preserves_filename_bytes_and_failure(
    valid: bool,
) -> None:
    from unittest.mock import patch

    from core.application.orchestration.graph.nodes import execute_step_node
    from core.application.reports.ui_projection_helpers import _build_message_steps
    from tests._step_run_state import step_run_state

    filename = "Saved Ąžuolas (owner).txt"
    body = "Ąžuolas / raw owner bytes".encode()
    plane = _Plane()
    state = step_run_state(
        {
            "step_id": "owned-file",
            "step_kind": "file_from_base64",
            "display_name": "Owner Ąžuolas",
            "override": {
                "file_from_base64": {
                    "source": "state.outputs.image",
                    "result_target": "state.outputs.saved_file",
                    "filename": filename,
                }
            },
        },
        **_state(_b64(body) if valid else "%%%", plane),
    )
    with patch(
        "core.application.orchestration.agents.common.publish_agent_message", return_value=True
    ):
        update = execute_step_node(state)
    message = update["agent_messages"][-1]
    projected = _build_message_steps([message])[0]
    assert projected["summary"] == message["summary"]
    result = message["artifacts"]["file_from_base64"]
    if valid:
        assert message["summary"] == f"Saved {filename} ({len(body)} bytes)"
        assert projected["summary_key"] == "react.jobs.step_file_from_base64_saved"
        assert projected["summary_params"] == {"filename": filename, "byte_count": len(body)}
        assert result["filename"] == filename
        assert result["size_bytes"] == len(body)
        assert plane.calls[0]["body"] == body
        assert update["outputs"]["saved_file"] == "artifact:art_1"
    else:
        assert message["status"] == "fail"
        assert message["summary"] == "file_from_base64 failed: invalid_base64"
        assert not projected.get("summary_key")
        assert projected["summary_params"] == {}
        assert "summary_key" not in message["artifacts"]
        assert "summary_params" not in message["artifacts"]
        assert result["failure_reason"] == "invalid_base64"
        assert plane.calls == []


@pytest.mark.parametrize("locale", ["en", "lt"])
def test_file_success_template_is_in_the_real_compiled_locale_catalog(locale: str) -> None:
    import gettext
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    with (root / "locales" / locale / "LC_MESSAGES/ui.mo").open("rb") as stream:
        catalog = gettext.GNUTranslations(stream)
    filename = "Saved Ąžuolas {owner}.txt"
    rendered = catalog.gettext("react.jobs.step_file_from_base64_saved").format(
        filename=filename, byte_count=56
    )
    expected = (
        f"Saved {filename} (56 bytes)" if locale == "en" else f"Išsaugota {filename} (baitų: 56)"
    )
    assert rendered == expected
