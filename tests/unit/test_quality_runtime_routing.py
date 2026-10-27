"""Quality and testing commands must run where their tools live.

Remediation epic 2026-07-29 — `AUDIT-089` and `AUDIT-083`.

`quality release-gate` exited immediately because `pytest` is not installed in
the shipped CLI image. It lives in `requirements-audit.txt`, which only the
`audit` build target installs.

Owner decision: route quality to `fs-audit`, rather than adding test tooling to
the public CLI image.

The property that matters most here is the one the audit actually complained
about: a gate that cannot run must not look like a gate that passed.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from core.application.tooling.quality import runtime_routing as rr


class TestServiceTargets:
    def test_quality_routes_to_the_audit_service(self):
        assert rr.PYTEST_IN_AUDIT.module == "pytest"
        assert rr.PYTEST_IN_AUDIT.service == "fs-audit"

    def test_the_audit_service_exists_in_the_compose_file(self):
        from pathlib import Path

        compose = Path(rr.COMPOSE_FILE).read_text(encoding="utf-8")

        assert f"\n  {rr.AUDIT_SERVICE}:" in compose


class TestRunningLocallyWhenTheToolIsPresent:
    def test_the_command_runs_in_place_and_returns_its_code(self):
        requirement = rr.RuntimeRequirement(module="pytest", service="fs-audit")
        with patch.object(rr.subprocess, "run") as run:
            run.return_value.returncode = 0
            code, diagnostic = rr.run_in_required_runtime(requirement, ["pytest", "-q"])

        assert (code, diagnostic) == (0, "")
        assert run.call_args[0][0] == ["pytest", "-q"], "the command was delegated unnecessarily"

    def test_a_failing_command_reports_its_own_exit_code_without_a_diagnostic(self):
        """A real test failure must not be dressed up as an environment problem."""
        requirement = rr.RuntimeRequirement(module="pytest", service="fs-audit")
        with patch.object(rr.subprocess, "run") as run:
            run.return_value.returncode = 1
            code, diagnostic = rr.run_in_required_runtime(requirement, ["pytest", "-q"])

        assert code == 1
        assert diagnostic == "", "a failing check is not a routing problem"


class TestDelegationWhenTheToolIsAbsent:
    ABSENT = rr.RuntimeRequirement(module="a_module_that_does_not_exist", service="fs-audit")

    def test_the_command_is_delegated_to_the_service(self):
        with (
            patch.object(rr, "can_delegate", return_value=True),
            patch.object(rr.subprocess, "run") as run,
        ):
            run.return_value.returncode = 0
            code, diagnostic = rr.run_in_required_runtime(self.ABSENT, ["pytest", "-q"])

        assert (code, diagnostic) == (0, "")
        argv = run.call_args[0][0]
        assert argv[:3] == ["docker", "compose", "-f"]
        assert "fs-audit" in argv
        assert argv[-2:] == ["pytest", "-q"]

    def test_the_delegated_run_is_flagged_so_it_cannot_recurse(self):
        argv = rr.delegation_command("fs-audit", ["pytest"])

        assert f"{rr.NESTED_ENV_FLAG}=1" in argv

    def test_delegation_reuses_exact_project_without_build_or_pull(self, monkeypatch):
        monkeypatch.setitem(
            rr._runtime_env,
            "FS_COMPOSE_PROJECT_NAME",
            "task-owned-project",
        )
        monkeypatch.setitem(rr._runtime_env, "FS_ENV_FILE", ".env")
        monkeypatch.setitem(
            rr._runtime_env,
            "FS_DOCKER_HOST_PROJECT_ROOT",
            "/srv/task-owned-install",
        )

        argv = rr.delegation_command("fs-audit", ["pytest"])

        assert argv[argv.index("-p") + 1] == "task-owned-project"
        assert argv[argv.index("-f") + 1] == (
            "/srv/task-owned-install/infra/docker/docker-compose.yaml"
        )
        assert "--project-directory" not in argv
        assert argv[argv.index("--env-file") + 1] == "/srv/task-owned-install/.env"
        assert argv[argv.index("--pull") + 1] == "never"
        assert "--build" not in argv
        assert argv[-2:] == ["fs-audit", "pytest"]

    def test_a_delegated_child_does_not_delegate_again(self, monkeypatch):
        monkeypatch.setenv(rr.NESTED_ENV_FLAG, "1")

        assert rr.already_delegated() is True
        assert rr.can_delegate() is False

    @pytest.mark.parametrize("value", ["", "0", "false"])
    def test_an_unset_or_falsy_flag_is_not_treated_as_delegated(self, monkeypatch, value):
        monkeypatch.setenv(rr.NESTED_ENV_FLAG, value)

        assert rr.already_delegated() is False


class TestUnrunnableIsNeverReportedAsPassed:
    """The AUDIT-089 property: exiting early must not look like success."""

    ABSENT = rr.RuntimeRequirement(module="a_module_that_does_not_exist", service="fs-audit")

    def test_a_command_that_cannot_run_returns_nonzero(self):
        with patch.object(rr, "can_delegate", return_value=False):
            code, diagnostic = rr.run_in_required_runtime(self.ABSENT, ["pytest"])

        assert code != 0, "an un-runnable gate reported success"
        assert diagnostic

    def test_the_diagnostic_names_the_tool_the_service_and_how_to_run_it(self):
        with patch.object(rr, "can_delegate", return_value=False):
            _code, diagnostic = rr.run_in_required_runtime(self.ABSENT, ["pytest"])

        assert "a_module_that_does_not_exist" in diagnostic
        assert "fs-audit" in diagnostic
        assert "docker compose" in diagnostic
        assert rr.COMPOSE_FILE in diagnostic


class TestReleaseGateOwnsOnlyReleaseContracts:
    def test_the_gate_does_not_repeat_tier_three_pytest(self):
        from pathlib import Path

        source = Path("core/application/tooling/quality/release_gate.py").read_text(
            encoding="utf-8"
        )

        assert "pytest" not in source
        assert "load_check_registry" in source
