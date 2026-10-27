from __future__ import annotations

import json
from dataclasses import replace

import requests

from core.application.tooling.testing import fuzz_api, fuzz_mcp, fuzz_ui


def test_rpc_url_appends_mcp_when_base_is_host_root() -> None:
    assert fuzz_mcp._rpc_url("http://fs-mcp:8100") == "http://fs-mcp:8100/mcp"


def test_rpc_url_preserves_explicit_mcp_suffix() -> None:
    assert fuzz_mcp._rpc_url("http://fs-mcp:8100/mcp") == "http://fs-mcp:8100/mcp"
    assert fuzz_mcp._rpc_url("http://fs-mcp:8100/mcp/") == "http://fs-mcp:8100/mcp"


def test_fuzz_modules_do_not_expose_second_public_argparse_cli(capsys) -> None:
    for module, command_name in (
        (fuzz_api, "fuzz-api"),
        (fuzz_mcp, "fuzz-mcp"),
        (fuzz_ui, "fuzz-ui"),
    ):
        assert module.main(["--help"]) == 2
        output = capsys.readouterr().out
        assert f"flow-steward testing {command_name}" in output
        assert "--password" not in output
        assert "--bearer-token" not in output


def test_seed_changes_only_nonexistent_tool_name_and_preserves_auth(monkeypatch, tmp_path) -> None:
    requests_by_run: list[list[dict]] = []

    def send(_session, request, **_kwargs):
        raw_body = request.body.decode() if isinstance(request.body, bytes) else request.body
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            payload = raw_body
        requests_by_run[-1].append(
            {"url": request.url, "headers": dict(request.headers), "body": payload}
        )
        response = requests.Response()
        response.request = request
        response.status_code = 200
        result = {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}
        if not isinstance(payload, dict) or payload.get("method") != "tools/list":
            response.status_code = 400
            result = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Invalid request"},
            }
        response._content = json.dumps(result).encode()
        return response

    # Intercept the HTTP boundary; run the actual fuzz checks, request preparation
    # and report writer so the assertion covers the outgoing wire contract.
    monkeypatch.setattr(requests.Session, "send", send)
    options = fuzz_mcp.FuzzMcpOptions(
        app_base_url="https://app.example.test",
        mcp_base_url="https://mcp.example.test",
        seed=17,
        timeout=1,
        burst_size=1,
        report_path=str(tmp_path / "report.json"),
        role="admin",
        username="",
        password="",
        bearer_token="test-bearer",
        skip_auth=False,
        strict=True,
    )
    for seed in (17, 17, 23):
        requests_by_run.append([])
        assert fuzz_mcp.run_fuzz_mcp_options(replace(options, seed=seed)) == 0

    first, repeated, different_seed = requests_by_run
    assert first == repeated
    assert len(first) == len(different_seed) == 6
    differences = [
        index
        for index, (left, right) in enumerate(zip(first, different_seed, strict=True))
        if left != right
    ]
    assert len(differences) == 1
    changed_index = differences[0]
    for run in requests_by_run:
        for request in run:
            assert request["headers"]["Authorization"] == "Bearer test-bearer"
            assert request["url"] == "https://mcp.example.test/mcp"
        changed = run[changed_index]["body"]
        assert changed["method"] == "tools/call"
        assert changed["params"]["arguments"] == {}
        name = changed["params"]["name"]
        assert name.startswith("tool_does_not_exist_")
        assert name.removeprefix("tool_does_not_exist_").isdigit()
        changed["params"]["name"] = "<nonexistent-tool-name>"
    assert first == different_seed
