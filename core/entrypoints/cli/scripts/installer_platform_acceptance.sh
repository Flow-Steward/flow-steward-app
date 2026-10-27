#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "$0")" && pwd -P)"
root_dir="$(cd -- "${script_dir}/../../../.." && pwd -P)"

require_linux=0
scenario="all"
output_dir=""

usage() {
  cat <<'EOF'
Usage: installer_platform_acceptance.sh [options]

Options:
  --require-linux          Reject non-Linux hosts before running acceptance.
  --scenario NAME         all, production, development, or official-release (default: all).
  --output-dir PATH       Evidence directory (default: logs/installer-platform/<run-id>).
  -h, --help              Show this help.
EOF
}

while (($#)); do
  case "$1" in
    --require-linux)
      require_linux=1
      shift
      ;;
    --scenario)
      if (($# < 2)); then
        echo "--scenario requires a value" >&2
        exit 2
      fi
      scenario="$2"
      shift 2
      ;;
    --output-dir)
      if (($# < 2)); then
        echo "--output-dir requires a value" >&2
        exit 2
      fi
      output_dir="$2"
      shift 2
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

case "$scenario" in
  all | production | development | official-release) ;;
  *)
    echo "Unsupported scenario: ${scenario}" >&2
    exit 2
    ;;
esac

if [[ -z "$output_dir" ]]; then
  run_id="$(date -u +%Y%m%dT%H%M%SZ)-$$"
  output_dir="${root_dir}/logs/installer-platform/${run_id}"
fi
mkdir -p -- "$output_dir"
output_dir="$(cd -- "$output_dir" && pwd -P)"
pytest_root=""
junit_xml=""
expected_node_ids=()

cleanup_pytest_root() {
  if [[ -n "$pytest_root" && "$pytest_root" == /tmp/flow-steward-installer-acceptance.* ]]; then
    rm -rf -- "$pytest_root"
  fi
}
trap cleanup_pytest_root EXIT

os_raw="$(uname -s 2>/dev/null || printf unknown)"
case "$os_raw" in
  Linux) platform_os="linux" ;;
  Darwin) platform_os="darwin" ;;
  *) platform_os="$(printf '%s' "$os_raw" | tr '[:upper:]' '[:lower:]')" ;;
esac

arch_raw="$(uname -m 2>/dev/null || printf unknown)"
case "$arch_raw" in
  x86_64 | amd64) platform_arch="amd64" ;;
  arm64 | aarch64) platform_arch="arm64" ;;
  *) platform_arch="$arch_raw" ;;
esac

revision="$(git -C "$root_dir" rev-parse 'HEAD^{commit}' 2>/dev/null || printf unknown)"
docker_engine="unavailable"
docker_compose="unavailable"
bounded_probe() {
  python3 - "$@" <<'PY'
import subprocess
import sys

try:
    completed = subprocess.run(
        sys.argv[1:],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
except (OSError, subprocess.TimeoutExpired):
    raise SystemExit(1)
sys.stdout.write(completed.stdout)
raise SystemExit(completed.returncode)
PY
}
if docker_value="$(bounded_probe docker version --format '{{.Client.Version}}/{{.Server.Version}}' 2>/dev/null)"; then
  docker_engine="$docker_value"
fi
if compose_value="$(bounded_probe docker compose version --short 2>/dev/null)"; then
  docker_compose="$compose_value"
fi

requested_status="passed"
requested_exit=0
blocked_reason=""

if ((require_linux)) && [[ "$platform_os" != "linux" ]]; then
  requested_status="blocked"
  requested_exit=4
  blocked_reason="--require-linux requires a native Linux host"
elif [[ ! "$revision" =~ ^[0-9a-f]{40}$ ]]; then
  requested_status="blocked"
  requested_exit=5
  blocked_reason="An exact Git revision is required for installer acceptance"
elif [[ "$docker_engine" == "unavailable" || "$docker_compose" == "unavailable" ]]; then
  requested_status="blocked"
  requested_exit=7
  blocked_reason="Docker Engine and Compose v2 must be reachable"
elif [[ "$scenario" == "official-release" ]] && {
  [[ -z "${FLOW_STEWARD_TEST_RELEASE_ACCEPTANCE_COMPACT_MANIFEST:-}" ]] ||
  [[ -z "${FLOW_STEWARD_TEST_RELEASE_ACCEPTANCE_SCALABLE_MANIFEST:-}" ]] ||
  [[ -z "${FLOW_STEWARD_TEST_RELEASE_ACCEPTANCE_TARGET_VERSION:-}" ]];
}; then
  requested_status="blocked"
  requested_exit=4
  blocked_reason="Official release acceptance requires compact/scalable disposable manifests and a target version"
else
  positive_test="tests/integration/test_installer_disposable.py::test_disposable_install_reaches_storage_round_trip"
  case "$scenario" in
    production)
      test_nodes=(
        "${positive_test}[production-textual-bundled]"
        "${positive_test}[production-textual-external_s3]"
        "${positive_test}[production-plain-bundled]"
        "${positive_test}[production-plain-external_s3]"
        "${positive_test}[production-non_interactive-bundled]"
        "${positive_test}[production-non_interactive-external_s3]"
      )
      expected_node_ids=("${test_nodes[@]}")
      ;;
    development)
      test_nodes=(
        "${positive_test}[development-textual-bundled]"
        "${positive_test}[development-textual-external_s3]"
        "${positive_test}[development-plain-bundled]"
        "${positive_test}[development-plain-external_s3]"
        "${positive_test}[development-non_interactive-bundled]"
        "${positive_test}[development-non_interactive-external_s3]"
      )
      expected_node_ids=("${test_nodes[@]}")
      ;;
    official-release)
      live_test="tests/integration/test_release_upgrade_disposable.py::test_live_official_release_upgrade_uses_public_cli_and_real_disposable_stack"
      test_nodes=("$live_test")
      expected_node_ids=(
        "${live_test}[compact]"
        "${live_test}[scalable]"
      )
      ;;
    all)
      test_nodes=(
        "$positive_test"
        "tests/integration/test_installer_disposable.py::test_interrupted_install_resumes_from_persisted_journal"
        "tests/integration/test_installer_disposable.py::test_repeat_after_completion_reports_already_installed"
        "tests/integration/test_installer_disposable.py::test_unreachable_external_s3_refuses_with_specific_redacted_error"
        "tests/integration/test_installer_disposable.py::test_occupied_public_port_refuses_before_compose_mutation"
        "tests/integration/test_installer_disposable.py::test_insufficient_disk_refuses_before_compose_mutation"
        "tests/integration/test_installer_disposable.py::test_tls_mode_reaches_healthy_public_route"
        "tests/integration/test_installer_disposable.py::test_ambiguous_owned_state_refuses_without_adoption"
      )
      expected_node_ids=(
        "${positive_test}[production-textual-bundled]"
        "${positive_test}[production-textual-external_s3]"
        "${positive_test}[production-plain-bundled]"
        "${positive_test}[production-plain-external_s3]"
        "${positive_test}[production-non_interactive-bundled]"
        "${positive_test}[production-non_interactive-external_s3]"
        "${positive_test}[development-textual-bundled]"
        "${positive_test}[development-textual-external_s3]"
        "${positive_test}[development-plain-bundled]"
        "${positive_test}[development-plain-external_s3]"
        "${positive_test}[development-non_interactive-bundled]"
        "${positive_test}[development-non_interactive-external_s3]"
        "tests/integration/test_installer_disposable.py::test_interrupted_install_resumes_from_persisted_journal"
        "tests/integration/test_installer_disposable.py::test_repeat_after_completion_reports_already_installed"
        "tests/integration/test_installer_disposable.py::test_unreachable_external_s3_refuses_with_specific_redacted_error"
        "tests/integration/test_installer_disposable.py::test_occupied_public_port_refuses_before_compose_mutation"
        "tests/integration/test_installer_disposable.py::test_insufficient_disk_refuses_before_compose_mutation"
        "tests/integration/test_installer_disposable.py::test_tls_mode_reaches_healthy_public_route[local]"
        "tests/integration/test_installer_disposable.py::test_tls_mode_reaches_healthy_public_route[provided]"
        "tests/integration/test_installer_disposable.py::test_tls_mode_reaches_healthy_public_route[reverse_proxy]"
        "tests/integration/test_installer_disposable.py::test_ambiguous_owned_state_refuses_without_adoption"
      )
      ;;
  esac

  pytest_root="$(mktemp -d /tmp/flow-steward-installer-acceptance.XXXXXX)"
  junit_xml="${pytest_root}/junit.xml"
  pytest_command=(python3 -m pytest)
  set +e
  (
    cd -- "$root_dir"
    "${pytest_command[@]}" -q -x -s --basetemp="${pytest_root}" \
      --junitxml="${junit_xml}" "${test_nodes[@]}"
  )
  requested_exit=$?
  set -e
  if ((requested_exit != 0)); then
    requested_status="failed"
  fi
fi

set +e
python3 - \
  "${output_dir}/evidence.json" \
  "$platform_os" \
  "$platform_arch" \
  "$docker_engine" \
  "$docker_compose" \
  "$revision" \
  "$scenario" \
  "$requested_status" \
  "$requested_exit" \
  "$blocked_reason" \
  "$pytest_root" \
  "$junit_xml" \
  ${expected_node_ids[@]+"${expected_node_ids[@]}"} <<'PY'
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree

evidence_path = Path(sys.argv[1])
platform_os = sys.argv[2]
platform_arch = sys.argv[3]
docker_engine = sys.argv[4]
docker_compose = sys.argv[5]
revision = sys.argv[6]
scenario = sys.argv[7]
requested_status = sys.argv[8]
requested_exit = int(sys.argv[9])
blocked_reason = sys.argv[10]
pytest_root_arg = sys.argv[11]
junit_xml_arg = sys.argv[12]
expected_nodes = sys.argv[13:]

scenario_results: list[dict[str, str]] = []
if junit_xml_arg:
    try:
        junit_root = ElementTree.parse(junit_xml_arg).getroot()
        for test_case in junit_root.iter("testcase"):
            class_name = test_case.attrib["classname"]
            test_name = test_case.attrib["name"]
            node_id = f"{class_name.replace('.', '/')}.py::{test_name}"
            if test_case.find("error") is not None:
                outcome = "error"
            elif test_case.find("failure") is not None:
                outcome = "failed"
            elif test_case.find("skipped") is not None:
                outcome = "skipped"
            else:
                outcome = "passed"
            scenario_results.append({"node_id": node_id, "outcome": outcome})
    except (ElementTree.ParseError, KeyError, OSError, TypeError, ValueError):
        scenario_results = []

scenario_results.sort(key=lambda item: item["node_id"])
observed_nodes = [item["node_id"] for item in scenario_results]
scenarios_verified = (
    bool(expected_nodes)
    and len(observed_nodes) == len(expected_nodes)
    and len(observed_nodes) == len(set(observed_nodes))
    and set(observed_nodes) == set(expected_nodes)
    and all(item["outcome"] == "passed" for item in scenario_results)
)

manifests = (
    sorted(Path(pytest_root_arg).rglob("disposable-installer-resources.json"))
    if pytest_root_arg
    else []
)
cleanup_verified = bool(expected_nodes) and len(manifests) == len(expected_nodes)
resource_kinds = ("containers", "images", "networks", "paths", "volumes")
cleanup_proofs: list[dict[str, object]] = []
for manifest_path in manifests:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        active = manifest["active_resources"]
        if not isinstance(active, dict) or any(
            not isinstance(active.get(kind), list) for kind in resource_kinds
        ):
            raise TypeError("active_resources must contain lists")
        counts = {kind: len(active[kind]) for kind in resource_kinds}
        source_revision = str(manifest["source_revision"])
        project_name = str(manifest["project_name"])
        cleanup_proofs.append(
            {
                "project_name": project_name,
                "source_revision": source_revision,
                "active_resource_counts": counts,
            }
        )
        cleanup_verified = (
            cleanup_verified
            and all(count == 0 for count in counts.values())
            and source_revision == revision
        )
    except (KeyError, OSError, TypeError, ValueError):
        cleanup_proofs.append(
            {
                "project_name": "unknown",
                "source_revision": "unknown",
                "active_resource_counts": {kind: -1 for kind in resource_kinds},
            }
        )
        cleanup_verified = False

status = requested_status
exit_code = requested_exit
if requested_status == "passed" and not (cleanup_verified and scenarios_verified):
    status = "failed"
    exit_code = 1

evidence = {
    "schema_version": 1,
    "generated_at": datetime.now(UTC).isoformat(),
    "platform": {
        "os": platform_os,
        "architecture": platform_arch,
    },
    "docker": {
        "engine": docker_engine,
        "compose": docker_compose,
    },
    "revision": revision,
    "scenario": scenario,
    "result": {
        "status": status,
        "exit_code": exit_code,
        "manifest_count": len(manifests),
        "cleanup_verified": cleanup_verified,
        "expected_scenario_count": len(expected_nodes),
        "scenario_count": len(scenario_results),
        "scenarios_verified": scenarios_verified,
    },
    "cleanup_proofs": cleanup_proofs,
    "scenario_results": scenario_results,
}
if blocked_reason:
    evidence["blocked_reason"] = blocked_reason

temporary = evidence_path.with_suffix(".json.tmp")
temporary.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
temporary.replace(evidence_path)
raise SystemExit(exit_code)
PY
final_exit=$?
set -e

printf 'Installer platform evidence: %s\n' "${output_dir}/evidence.json"
exit "$final_exit"
