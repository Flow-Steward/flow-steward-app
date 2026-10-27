"""System-owned step kind profiles for Builder and workflow knowledge indexing."""

from __future__ import annotations

import json
from typing import Any

from core.application.orchestration.dataset_cursor_contracts import MAX_DATASET_PAGE_SIZE
from core.application.registry.builder_metadata import normalize_builder_metadata
from core.application.workflows.workflow_definition_validation import WAIT_DURATION_MS_MAX
from core.application.workflows.workflow_step_kinds import (
    STEP_KIND_AGENT,
    STEP_KIND_APPROVAL,
    STEP_KIND_BATCH,
    STEP_KIND_BREAK,
    STEP_KIND_COLLECT,
    STEP_KIND_CONNECTOR,
    STEP_KIND_CONTINUE,
    STEP_KIND_DATASET_QUERY,
    STEP_KIND_FILE_FROM_BASE64,
    STEP_KIND_HTTP_REQUEST,
    STEP_KIND_IF,
    STEP_KIND_INVOKE_WORKFLOW,
    STEP_KIND_SET_VARIABLE,
    STEP_KIND_STOP_FAIL,
    STEP_KIND_STOP_SUCCESS,
    STEP_KIND_SWITCH,
    STEP_KIND_TABULAR_FEED,
    STEP_KIND_TEMPLATE,
    STEP_KIND_TRANSFORM,
    STEP_KIND_WAIT,
)

_BUILTIN_STEP_KIND_PROFILES: tuple[dict[str, Any], ...] = (
    {
        "step_kind_key": STEP_KIND_WAIT,
        "display_name": "Wait",
        "description": "Pause durably for a duration, then follow success; failures use the error policy. Allowed at root or in sequential groups, not parallel lanes.",
        "enabled": True,
        "builder_metadata_jsonb": {
            "purpose": "Wait for a timer without occupying a worker.",
            "composition_role": "control_flow",
            "determinism_level": "deterministic",
            "safe_for_auto_use": True,
        },
        "config_schema_jsonb": {
            "type": "object",
            "properties": {
                "wait": {
                    "type": "object",
                    "description": "Provide exactly one duration source: literal duration_ms or input_mapping.duration_ms. Mapped values must also be integer milliseconds in the allowed range.",
                    "properties": {
                        "duration_ms": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": WAIT_DURATION_MS_MAX,
                        },
                        "input_mapping": {
                            "type": "object",
                            "properties": {
                                "duration_ms": {
                                    "description": "Workflow selector, template or literal that resolves to integer milliseconds.",
                                }
                            },
                            "additionalProperties": False,
                        },
                    },
                    "oneOf": [
                        {"required": ["duration_ms"]},
                        {
                            "required": ["input_mapping"],
                            "properties": {"input_mapping": {"required": ["duration_ms"]}},
                        },
                    ],
                    "additionalProperties": False,
                },
            },
        },
    },
    {
        "step_kind_key": STEP_KIND_APPROVAL,
        "display_name": "Approval",
        "description": "Pause for a human decision and route approved or rejected explicitly; failures use the error policy. Allowed at root or in sequential groups, not parallel lanes.",
        "enabled": True,
        "builder_metadata_jsonb": {
            "purpose": "Require a human decision before continuing a workflow.",
            "composition_role": "control_flow",
            "determinism_level": "human",
            "safe_for_auto_use": False,
        },
        "config_schema_jsonb": {
            "type": "object",
            "properties": {
                "approval": {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string"},
                        "show_outputs": {"type": "array", "items": {"type": "string"}},
                        "output_mapping": {"type": "object"},
                        "response_mapping": {"type": "object"},
                    },
                    "additionalProperties": False,
                },
            },
        },
    },
    {
        "step_kind_key": STEP_KIND_AGENT,
        "display_name": "Agent Step",
        "description": "Use an assigned agent to reason over text, produce structured outputs, or coordinate multiple tools.",
        "enabled": True,
        "builder_metadata_jsonb": {
            "purpose": "Delegate open-ended reasoning or synthesis work to an agent.",
            "use_cases": [
                "classification",
                "content generation",
                "analysis",
                "multi-step reasoning",
            ],
            "anti_use_cases": [
                "simple deterministic transforms",
                "direct HTTP calls with fixed payloads",
            ],
            "composition_role": "reasoning_step",
            "determinism_level": "agentic",
            "safe_for_auto_use": False,
        },
        "config_schema_jsonb": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "skill_id": {"type": "string"},
            },
        },
    },
    {
        "step_kind_key": STEP_KIND_CONNECTOR,
        "display_name": "Connector Step",
        "description": "Invoke an installed connector or tool-backed action directly from the workflow canvas.",
        "enabled": True,
        "builder_metadata_jsonb": {
            "purpose": "Execute a deterministic connector or tool operation without an agent wrapper.",
            "use_cases": [
                "publish content",
                "call installed actions",
                "send data to external systems",
            ],
            "anti_use_cases": [
                "high-ambiguity reasoning",
                "free-form text generation without a deterministic operation",
            ],
            "composition_role": "external_action",
            "determinism_level": "deterministic",
            "safe_for_auto_use": True,
        },
        "config_schema_jsonb": {
            "type": "object",
            "properties": {
                "connector_id": {"type": "string"},
                "connector_install_id": {"type": "string"},
                "operation_id": {"type": "string"},
            },
        },
    },
    {
        "step_kind_key": STEP_KIND_INVOKE_WORKFLOW,
        "display_name": "Invoke Workflow",
        "description": "Call an existing workflow through its callable contract and continue with its outputs.",
        "enabled": True,
        "builder_metadata_jsonb": {
            "purpose": "Reuse an existing workflow as a child workflow instead of duplicating its logic.",
            "use_cases": [
                "shared subroutines",
                "workflow orchestration",
                "reusable publish or enrichment flows",
            ],
            "anti_use_cases": [
                "single inline deterministic transforms",
            ],
            "composition_role": "workflow_reuse",
            "determinism_level": "hybrid",
            "safe_for_auto_use": True,
        },
        "config_schema_jsonb": {
            "type": "object",
            "properties": {
                "workflow_id": {"type": "string"},
            },
            "required": ["workflow_id"],
        },
    },
    {
        "step_kind_key": STEP_KIND_IF,
        "display_name": "If Branch",
        "description": "Branch execution into true and false paths from a boolean condition.",
        "enabled": True,
        "builder_metadata_jsonb": {
            "purpose": "Branch workflow execution from a boolean decision.",
            "use_cases": ["boolean routing", "guard conditions", "approval checks"],
            "anti_use_cases": ["multi-value routing", "list iteration"],
            "composition_role": "branching",
            "determinism_level": "deterministic",
            "safe_for_auto_use": True,
        },
        "config_schema_jsonb": {
            "type": "object",
            "properties": {
                "if": {
                    "type": "object",
                    "properties": {
                        "condition": {"type": "object"},
                    },
                }
            },
        },
    },
    {
        "step_kind_key": STEP_KIND_SWITCH,
        "display_name": "Switch Branch",
        "description": "Route execution across multiple named cases from a selector value.",
        "enabled": True,
        "builder_metadata_jsonb": {
            "purpose": "Branch workflow execution across multiple explicit cases.",
            "use_cases": ["multi-case routing", "status routing", "channel routing"],
            "anti_use_cases": ["simple true/false checks", "list iteration"],
            "composition_role": "branching",
            "determinism_level": "deterministic",
            "safe_for_auto_use": True,
        },
        "config_schema_jsonb": {
            "type": "object",
            "properties": {
                "switch": {
                    "type": "object",
                    "properties": {
                        "selector": {"type": "string"},
                        "cases": {"type": "array"},
                    },
                }
            },
        },
    },
    {
        "step_kind_key": STEP_KIND_STOP_SUCCESS,
        "display_name": "Stop Success",
        "description": "Terminate the workflow successfully and surface final outputs.",
        "enabled": True,
        "builder_metadata_jsonb": {
            "purpose": "Finish the workflow with a success outcome.",
            "use_cases": ["successful completion", "explicit terminal node"],
            "anti_use_cases": ["intermediate processing", "failure handling"],
            "composition_role": "terminal",
            "determinism_level": "deterministic",
            "safe_for_auto_use": True,
        },
        "config_schema_jsonb": {"type": "object", "properties": {}},
    },
    {
        "step_kind_key": STEP_KIND_STOP_FAIL,
        "display_name": "Stop Fail",
        "description": "Terminate the workflow with a failure outcome and error message.",
        "enabled": True,
        "builder_metadata_jsonb": {
            "purpose": "Finish the workflow with a failure outcome.",
            "use_cases": ["intentional fail path", "guardrail stop"],
            "anti_use_cases": ["normal completion", "branching"],
            "composition_role": "terminal",
            "determinism_level": "deterministic",
            "safe_for_auto_use": True,
        },
        "config_schema_jsonb": {"type": "object", "properties": {}},
    },
    {
        "step_kind_key": STEP_KIND_SET_VARIABLE,
        "display_name": "Set Variable",
        "description": "Write deterministic values into workflow state for later steps.",
        "enabled": True,
        "builder_metadata_jsonb": {
            "purpose": "Prepare or reshape deterministic state values.",
            "use_cases": ["payload shaping", "state aliases", "default values"],
            "anti_use_cases": ["semantic analysis", "external side effects"],
            "composition_role": "deterministic_transform",
            "determinism_level": "deterministic",
            "safe_for_auto_use": True,
        },
        "config_schema_jsonb": {
            "type": "object",
            "properties": {
                "set_variable": {
                    "type": "object",
                    "properties": {
                        "target": {"type": "string"},
                        "value": {},
                    },
                    "required": ["target"],
                }
            },
        },
    },
    {
        "step_kind_key": STEP_KIND_TRANSFORM,
        "display_name": "Transform",
        "description": "Map and reshape JSON deterministically with typed transform-safe actions.",
        "enabled": True,
        "builder_metadata_jsonb": {
            "purpose": "Convert upstream JSON into a downstream payload shape without arbitrary code or I/O.",
            "use_cases": [
                "field mapping",
                "payload normalization",
                "literal business constants",
                "simple text and number transforms",
            ],
            "anti_use_cases": [
                "external side effects",
                "free-form formulas",
                "CSV or spreadsheet processing",
            ],
            "composition_role": "deterministic_transform",
            "determinism_level": "deterministic",
            "safe_for_auto_use": False,
        },
        "config_schema_jsonb": {
            "type": "object",
            "properties": {
                "transform": {
                    "type": "object",
                    "properties": {
                        "input": {"type": "object"},
                        "result_target": {"type": "string"},
                        "fields": {"type": "array"},
                    },
                    "required": ["input", "result_target", "fields"],
                }
            },
        },
    },
    {
        "step_kind_key": STEP_KIND_TABULAR_FEED,
        "display_name": "Tabular Feed",
        "description": (
            "Read, transform, and export tabular data using artifact and dataset handles. "
            "An uploaded artifact or workflow input name is not a registered SQL dataset_id. "
            "dataset_query requires a selected registry dataset and query template; "
            "tabular_feed reads a supplied artifact_handle and passes dataset_handle between operations."
        ),
        "enabled": True,
        "builder_metadata_jsonb": {
            "purpose": "Import CSV or other tabular artifacts into a dataset, normalize columns in bulk, and pass dataset handles between operations. Use read_rows with an explicit limit from 1 to 1000 for a bounded page of JSON rows. A page is not a promise to return or send the full feed; do not silently truncate a request for all rows.",
            "use_cases": [
                "supplier CSV import: read artifact_handle into dataset_handle",
                "bulk normalization: transform dataset_handle with text.trim or number.coerce actions",
                "dataset export: write dataset_handle into an artifact",
                "bounded row reads: read_rows returns rows separately from the dataset handle",
            ],
            "anti_use_cases": [
                "reshaping an existing small JSON object or array: use transform step",
                "per-row loops over full feeds or treating a rows array as a dataset_handle",
                "arbitrary code, formulas, or action objects using type/column/to instead of action/target/params",
            ],
            "composition_role": "bulk_data_step",
            "determinism_level": "deterministic",
            "safe_for_auto_use": False,
        },
        "config_schema_jsonb": {
            "type": "object",
            "properties": {
                "tabular_feed": {
                    "type": "object",
                    "description": (
                        "The following examples form three consecutive tabular_feed steps. "
                        "The caller supplies an existing artifact handle as inputs.artifact. "
                        "read returns a dataset_handle; transform returns the transformed dataset_handle; "
                        "read_rows accepts limit from 1 to 1000 and materializes a bounded page "
                        "under result_target.rows, not necessarily the full feed. "
                        "column_map maps source column names to destination names (string values). "
                        "Each action uses action and target, with optional params and when; "
                        "consult the canonical actions and action_fields in this description."
                    ),
                    "examples": [
                        {
                            "operation": "read",
                            "input": {"artifact_handle": "state.inputs.artifact"},
                            "column_map": {"Vendor SKU": "sku", "Price": "price"},
                            "result_target": "state.table",
                        },
                        {
                            "operation": "transform",
                            "input": {"dataset_handle": "state.table.dataset_handle"},
                            "actions": [
                                {"action": "text.trim", "target": "sku"},
                                {"action": "number.coerce", "target": "price"},
                            ],
                            "result_target": "state.normalized_table",
                        },
                        {
                            "operation": "read_rows",
                            "input": {"dataset_handle": "state.normalized_table.dataset_handle"},
                            "limit": 2,
                            "result_target": "state.preview",
                        },
                    ],
                    "properties": {
                        "operation": {"type": "string"},
                        "input": {"type": "object"},
                        "result_target": {"type": "string"},
                    },
                    "required": ["operation", "result_target"],
                }
            },
        },
    },
    {
        "step_kind_key": STEP_KIND_COLLECT,
        "display_name": "Collect",
        "description": (
            "Add one value to an existing variable inside a sequence For each: append to an "
            "array, or set one literal key of an object. The variable must exist before the "
            "For each (a Set Variable to [] or {}, or an input); Collect never creates or "
            "resets it. Valid only directly in a sequence For each body."
        ),
        "enabled": True,
        "builder_metadata_jsonb": {
            "purpose": "Accumulate results of a For each pass into an ordinary variable.",
            "use_cases": ["collect matching emails", "index rows by key"],
            "anti_use_cases": ["assigning a single value (use Set Variable)", "parallel lanes"],
            "composition_role": "iteration",
            "determinism_level": "deterministic",
            "safe_for_auto_use": True,
        },
        "config_schema_jsonb": {
            "type": "object",
            "properties": {
                "collect": {
                    "type": "object",
                    "description": (
                        "target and source are state paths. mode append adds source to the "
                        "target array; mode set_key writes source under key in the target "
                        "object. key is literal text or {source: state path}; its value is one "
                        "key as written (dots are not nesting)."
                    ),
                    "examples": [
                        {"target": "state.emails", "source": "state.order.email"},
                        {
                            "target": "state.by_email",
                            "source": "state.order",
                            "mode": "set_key",
                            "key": {"source": "state.order.email"},
                        },
                    ],
                    "properties": {
                        "target": {"type": "string"},
                        "source": {"type": "string"},
                        "mode": {"type": "string", "enum": ["append", "set_key"]},
                        "key": {"type": ["string", "object"]},
                    },
                    "required": ["target", "source"],
                }
            },
        },
    },
    {
        "step_kind_key": STEP_KIND_BREAK,
        "display_name": "Break",
        "description": (
            "Leave the nearest For each now, without its remaining items. It has no outgoing "
            "edge; the For each then leaves by its success edge. Valid only directly in a "
            "sequence For each body."
        ),
        "enabled": True,
        "builder_metadata_jsonb": {
            "purpose": "Stop iterating once the result is known.",
            "use_cases": ["first match found", "budget reached"],
            "anti_use_cases": ["ending the workflow (use Stop)"],
            "composition_role": "control",
            "determinism_level": "deterministic",
            "safe_for_auto_use": True,
        },
        "config_schema_jsonb": {"type": "object", "properties": {}},
    },
    {
        "step_kind_key": STEP_KIND_CONTINUE,
        "display_name": "Continue",
        "description": (
            "Skip the rest of the current item and go on with the next one. It has no outgoing "
            "edge. Valid only directly in a sequence For each body."
        ),
        "enabled": True,
        "builder_metadata_jsonb": {
            "purpose": "Skip items that need no work.",
            "use_cases": ["filter items with If"],
            "anti_use_cases": ["error handling (use an error policy)"],
            "composition_role": "control",
            "determinism_level": "deterministic",
            "safe_for_auto_use": True,
        },
        "config_schema_jsonb": {"type": "object", "properties": {}},
    },
    {
        "step_kind_key": STEP_KIND_BATCH,
        "display_name": "Batch",
        "description": "Split work into groups before downstream processing.",
        "enabled": True,
        "builder_metadata_jsonb": {
            "purpose": "Chunk larger collections into manageable groups.",
            "use_cases": ["batch processing", "rate-limit friendly grouping"],
            "anti_use_cases": ["single-object transforms", "branching decisions"],
            "composition_role": "iteration",
            "determinism_level": "deterministic",
            "safe_for_auto_use": True,
        },
        "config_schema_jsonb": {
            "type": "object",
            "properties": {
                "batch": {
                    "type": "object",
                    "properties": {
                        "source_path": {"type": "string"},
                        "batch_size": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": MAX_DATASET_PAGE_SIZE,
                        },
                    },
                    "required": ["source_path"],
                }
            },
        },
    },
    {
        "step_kind_key": STEP_KIND_FILE_FROM_BASE64,
        "display_name": "Save file from base64",
        "description": (
            "Decode a base64 value from workflow state (an image generation b64_json, an "
            "inline_data part, or a data: URL) into a downloadable file and write its "
            "artifact_handle to result_target."
        ),
        "enabled": True,
        "builder_metadata_jsonb": {
            "purpose": "Turn base64 file content returned by a model or API into a real file.",
            "use_cases": [
                "save a generated image from an image generation response",
                "store a base64-encoded document returned by an API",
                "hand the saved file to a later step as an artifact_handle",
            ],
            "anti_use_cases": [
                "plain text or JSON that is not base64 file content",
                "downloading a file from a URL: use http_request with response_mode artifact",
            ],
            "composition_role": "file_conversion",
            "determinism_level": "deterministic",
            "safe_for_auto_use": True,
        },
        "config_schema_jsonb": {
            "type": "object",
            "properties": {
                STEP_KIND_FILE_FROM_BASE64: {
                    "type": "object",
                    "description": (
                        "source is a state selector of the base64 value: a string, a "
                        "data: URL, or an object holding b64_json/data/base64 with an optional "
                        "mime_type. filename and content_type are optional templates; the "
                        "content type is otherwise recognised from the bytes."
                    ),
                    "examples": [
                        {
                            "source": "state.outputs.image_response.data[0].b64_json",
                            "filename": "generated.png",
                            "result_target": "state.outputs.generated_image",
                        }
                    ],
                    "properties": {
                        "source": {"type": "string"},
                        "result_target": {"type": "string"},
                        "filename": {"type": "string"},
                        "content_type": {"type": "string"},
                        "max_bytes": {"type": "integer", "minimum": 1},
                    },
                    "required": ["source", "result_target"],
                }
            },
        },
    },
    {
        "step_kind_key": STEP_KIND_TEMPLATE,
        "display_name": "Template",
        "description": (
            "Assemble one text from declared inputs with a Jinja template: a prompt, an "
            "email, a product description, a post or an HTML fragment. Writes the text "
            "to result_target; never calls a model."
        ),
        "enabled": True,
        "builder_metadata_jsonb": {
            "purpose": "Build a text with a fixed structure from values produced earlier.",
            "use_cases": [
                "compose an email or post from a header, body and footer",
                "build a prompt for an agent from several workflow values",
                "render an HTML table from a list prepared by a transform step",
            ],
            "anti_use_cases": [
                "reshaping or calculating data: use transform before the template",
                "writing new text with a model: use an agent step",
            ],
            "composition_role": "text_assembly",
            "determinism_level": "deterministic",
            "safe_for_auto_use": True,
        },
        "config_schema_jsonb": {
            "type": "object",
            "properties": {
                STEP_KIND_TEMPLATE: {
                    "type": "object",
                    "description": (
                        "text is standard Jinja that reads only the declared inputs by their "
                        "local names ({{ header }}, {% for item in items %}). Each input "
                        "names a state selector as its source. format is text, markdown or "
                        "html; html escapes inserted values unless marked | safe. A missing "
                        "value fails the step unless the template uses default(...) or "
                        "is defined."
                    ),
                    "examples": [
                        {
                            "text": "# {{ header }}\n\n{{ body }}\n\n{{ footer | default('') }}\n",
                            "format": "markdown",
                            "inputs": [
                                {
                                    "name": "header",
                                    "type": "text",
                                    "required": True,
                                    "source": "state.outputs.header",
                                },
                                {
                                    "name": "body",
                                    "type": "text",
                                    "required": True,
                                    "source": "state.outputs.body",
                                },
                                {
                                    "name": "footer",
                                    "type": "text",
                                    "required": False,
                                    "source": "state.outputs.footer",
                                },
                            ],
                            "result_target": "state.outputs.post_markdown",
                        }
                    ],
                    "properties": {
                        "text": {"type": "string"},
                        "format": {"type": "string", "enum": ["text", "markdown", "html"]},
                        "inputs": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "type": {
                                        "type": "string",
                                        "enum": [
                                            "text",
                                            "number",
                                            "boolean",
                                            "object",
                                            "array",
                                            "json",
                                        ],
                                    },
                                    "required": {"type": "boolean"},
                                    "source": {"type": "string"},
                                },
                                "required": ["name", "source"],
                            },
                        },
                        "result_target": {"type": "string"},
                    },
                    "required": ["text", "result_target"],
                }
            },
        },
    },
    {
        "step_kind_key": STEP_KIND_HTTP_REQUEST,
        "display_name": "HTTP Request",
        "description": "Call a concrete HTTP endpoint with deterministic request configuration.",
        "enabled": True,
        "builder_metadata_jsonb": {
            "purpose": "Call a concrete external HTTP or API endpoint directly.",
            "use_cases": ["REST API calls", "webhooks", "external system writes"],
            "anti_use_cases": ["semantic generation", "implicit connector discovery"],
            "composition_role": "external_action",
            "determinism_level": "deterministic",
            "safe_for_auto_use": False,
        },
        "config_schema_jsonb": {
            "type": "object",
            "properties": {
                "http_request": {
                    "type": "object",
                    "properties": {
                        "artifact_filename": {"type": "string"},
                        "auth_mode": {
                            "type": "string",
                            "enum": ["none", "bearer", "basic"],
                        },
                        "auth_ref": {"type": "string"},
                        "body_template": {},
                        "expected_statuses": {
                            "type": "array",
                            "items": {"type": "integer", "minimum": 100, "maximum": 599},
                            "minItems": 1,
                        },
                        "headers": {"type": "object"},
                        "max_response_bytes": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 200 * 1024 * 1024,
                        },
                        "method": {
                            "type": "string",
                            "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
                        },
                        "query_params": {"type": "object"},
                        "response_mode": {
                            "type": "string",
                            "enum": ["inline", "artifact"],
                        },
                        "response_target": {"type": "string", "minLength": 1},
                        "timeout_seconds": {"type": "number", "exclusiveMinimum": 0},
                        "url": {"type": "string", "minLength": 1},
                    },
                    "required": [
                        "method",
                        "url",
                        "response_target",
                        "timeout_seconds",
                        "expected_statuses",
                    ],
                }
            },
        },
    },
    {
        "step_kind_key": STEP_KIND_DATASET_QUERY,
        "display_name": "Dataset Query",
        "description": "Run a dataset query template and expose its rows or aggregates downstream.",
        "enabled": True,
        "builder_metadata_jsonb": {
            "purpose": "Read structured data through an approved dataset query template.",
            "use_cases": ["lookup data", "analytics fetches", "report inputs"],
            "anti_use_cases": ["arbitrary side effects", "free-form external writes"],
            "composition_role": "data_retrieval",
            "determinism_level": "deterministic",
            "safe_for_auto_use": True,
        },
        "config_schema_jsonb": {
            "type": "object",
            "properties": {
                "dataset_query": {
                    "type": "object",
                    "properties": {
                        "dataset_id": {"type": "string"},
                        "query_template_id": {"type": "string"},
                    },
                }
            },
        },
    },
)


def builtin_step_kind_profiles() -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for row in _BUILTIN_STEP_KIND_PROFILES:
        normalized = dict(row)
        normalized["builder_metadata_jsonb"] = normalize_builder_metadata(
            row.get("builder_metadata_jsonb")
        )
        payload.append(normalized)
    return payload


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return dict(value) if isinstance(value, dict) else {}


def _profile_matches(existing: dict[str, Any] | None, desired: dict[str, Any]) -> bool:
    if not existing:
        return False
    desired_key = str(desired.get("step_kind_key") or "").strip()
    return (
        str(existing.get("step_kind_key") or "").strip() == desired_key
        and str(existing.get("display_name") or desired_key).strip()
        == str(desired.get("display_name") or desired_key).strip()
        and str(existing.get("description") or "").strip()
        == str(desired.get("description") or "").strip()
        and bool(existing.get("enabled", True)) == bool(desired.get("enabled", True))
        and normalize_builder_metadata(_json_object(existing.get("builder_metadata_jsonb")))
        == normalize_builder_metadata(desired.get("builder_metadata_jsonb"))
        and _json_object(existing.get("config_schema_jsonb"))
        == _json_object(desired.get("config_schema_jsonb"))
    )


def sync_builtin_step_kind_profiles(db_store: Any) -> list[str]:
    changed: list[str] = []
    for profile in builtin_step_kind_profiles():
        step_kind_key = str(profile.get("step_kind_key") or "").strip()
        if not step_kind_key:
            continue
        existing = db_store.get_step_kind_profile(step_kind_key)
        if _profile_matches(existing, profile):
            continue
        db_store.upsert_step_kind_profile(profile, author="system")
        changed.append(step_kind_key)
    return changed
