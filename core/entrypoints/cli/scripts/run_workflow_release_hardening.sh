#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "${SCRIPT_DIR}/../../../.." && pwd)"

RUN_UI_PYTEST="${ROOT_DIR}/core/entrypoints/cli/scripts/run_ui_pytest.sh"

if [[ ! -x "${RUN_UI_PYTEST}" ]]; then
  echo "Missing executable UI pytest runner at ${RUN_UI_PYTEST}" >&2
  exit 1
fi

timestamp="$(date +%Y%m%d_%H%M%S)"
summary_dir="${ROOT_DIR}/logs/ui"
summary_file="${summary_dir}/workflow_release_hardening_${timestamp}.summary.txt"
mkdir -p "${summary_dir}"

iso_now() {
  date +"%Y-%m-%dT%H:%M:%S%z"
}

status=0

{
  echo "Workflow release hardening run"
  echo "Started: $(iso_now)"
  echo "Repo: ${ROOT_DIR}"
  echo
} > "${summary_file}"

run_check() {
  local name="$1"
  shift
  local start_ts end_ts
  start_ts="$(iso_now)"

  echo "=== ${name} ===" | tee -a "${summary_file}"
  echo "Started: ${start_ts}" | tee -a "${summary_file}"
  echo "Command: ${RUN_UI_PYTEST} $*" | tee -a "${summary_file}"

  if "${RUN_UI_PYTEST}" "$@" | tee -a "${summary_file}"; then
    end_ts="$(iso_now)"
    echo "Result: PASS" | tee -a "${summary_file}"
    echo "Finished: ${end_ts}" | tee -a "${summary_file}"
    echo | tee -a "${summary_file}"
    return 0
  fi

  end_ts="$(iso_now)"
  echo "Result: FAIL" | tee -a "${summary_file}"
  echo "Finished: ${end_ts}" | tee -a "${summary_file}"
  echo | tee -a "${summary_file}"
  return 1
}

if ! run_check \
  "workflow-builder-hardening" \
  tests/ui/test_workflow_builder_contracts.py \
  -q \
  -k \
  "step_test_run or workflow_test_run or build_with_ai or advanced"; then
  status=1
fi

if ! run_check \
  "workflow-registry-pages" \
  tests/ui/test_registry_pages_extended.py \
  -q \
  -k \
  "workflows_page_loads or workflows_page_has_grid_or_list or workflow_detail_has_phases or workflow_builder_renders_canvas"; then
  status=1
fi

if ! run_check \
  "workflow-registry-mutations" \
  tests/ui/test_registry_workflows.py \
  -q \
  -k \
  "test_create_workflow_with_valid_data_persists_and_validates or test_update_workflow_settings_persists_description_and_tags or test_delete_workflow_removes_created_workflow or test_add_and_delete_phase_persists or test_phase_put_persists_display_name_update or test_phase_reorder_persists_new_order or test_step_crud_and_reorder_persist or test_workflow_export_import_round_trip_is_stable"; then
  status=1
fi

if ! run_check \
  "workflow-portability-and-runtime-contracts" \
  tests/unit/test_registry_admin_api.py \
  tests/unit/test_workflow_portability.py \
  tests/unit/test_serve_endpoints.py \
  tests/unit/test_query_services.py \
  -q \
  -k \
  "step_test_run or workflow_export_yaml_endpoint or workflow_import or retry_step"; then
  status=1
fi

if ! run_check \
  "workflow-job-detail-retry-ui" \
  tests/ui/test_job_detail_page.py \
  -q \
  -k \
  "retry"; then
  status=1
fi

if ! run_check \
  "workflow-import-export-e2e" \
  tests/e2e/test_workflow_import_export.py \
  -q; then
  status=1
fi

if ! run_check \
  "workflow-retry-step-e2e" \
  tests/e2e/test_job_replay_retry.py \
  -q \
  -k \
  "retry_step"; then
  status=1
fi

{
  echo "Overall result: $([[ ${status} -eq 0 ]] && echo PASS || echo FAIL)"
  echo "Finished: $(iso_now)"
} | tee -a "${summary_file}"

echo "Saved workflow release hardening summary to ${summary_file}"
exit "${status}"
