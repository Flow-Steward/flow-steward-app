from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.application.settings.ui_i18n import reset_ui_i18n_cache, translate_ui_text
from core.application.workflows.canvas_graph_service import build_workflow_canvas_graph

CASES = json.loads(r"""[
  {
    "kind": "set_variable",
    "cfg": {},
    "raw": "Not set up yet",
    "key": "react.workflows.step_not_set_up_summary",
    "params": {},
    "lt": "Dar nenustatyta"
  },
  {
    "kind": "set_variable",
    "cfg": {
      "value": true
    },
    "raw": "variable = true",
    "key": "react.workflows.set_variable_default_summary",
    "params": {
      "value": "true"
    },
    "lt": "kintamasis = true"
  },
  {
    "kind": "set_variable",
    "cfg": {
      "value": false
    },
    "raw": "variable = false",
    "key": "react.workflows.set_variable_default_summary",
    "params": {
      "value": "false"
    },
    "lt": "kintamasis = false"
  },
  {
    "kind": "set_variable",
    "cfg": {
      "value": 12.5
    },
    "raw": "variable = 12.5",
    "key": "react.workflows.set_variable_default_summary",
    "params": {
      "value": "12.5"
    },
    "lt": "kintamasis = 12.5"
  },
  {
    "kind": "set_variable",
    "cfg": {
      "value": "  owner value  "
    },
    "raw": "variable = owner value",
    "key": "react.workflows.set_variable_default_summary",
    "params": {
      "value": "owner value"
    },
    "lt": "kintamasis = owner value"
  },
  {
    "kind": "set_variable",
    "cfg": {
      "target": " ",
      "value": " "
    },
    "raw": "variable = \"\"",
    "key": "react.workflows.set_variable_default_summary",
    "params": {
      "value": "\"\""
    },
    "lt": "kintamasis = \"\""
  },
  {
    "kind": "set_variable",
    "cfg": {
      "value": [
        true,
        null,
        "owner",
        4
      ]
    },
    "raw": "variable = [true, null, owner, ...]",
    "key": "react.workflows.set_variable_default_summary",
    "params": {
      "value": "[true, null, owner, ...]"
    },
    "lt": "kintamasis = [true, null, owner, ...]"
  },
  {
    "kind": "set_variable",
    "cfg": {
      "value": {
        "owner": true,
        "selector": "state.source",
        "nested": [
          null,
          "owner"
        ]
      }
    },
    "raw": "variable = {owner: true, selector: state.source, nested: [null, owner]}",
    "key": "react.workflows.set_variable_default_summary",
    "params": {
      "value": "{owner: true, selector: state.source, nested: [null, owner]}"
    },
    "lt": "kintamasis = {owner: true, selector: state.source, nested: [null, owner]}"
  },
  {
    "kind": "set_variable",
    "cfg": {
      "target": " variable ",
      "value": true
    },
    "raw": "variable = true",
    "key": null,
    "params": null
  },
  {
    "kind": "set_variable",
    "cfg": {
      "target": " state.owner ",
      "value": {
        "source": "state.inputs.owner"
      }
    },
    "raw": "state.owner = {source: state.inputs.owner}",
    "key": null,
    "params": null
  },
  {
    "kind": "set_variable",
    "cfg": {
      "assignments": {
        "owner_first": true
      },
      "target": "ignored"
    },
    "raw": "owner_first = true",
    "key": null,
    "params": null
  },
  {
    "kind": "set_variable",
    "cfg": {
      "assignments": {
        "owner_first": true,
        "owner_second": null,
        "owner_third": [
          1,
          2,
          3,
          4
        ]
      }
    },
    "raw": "owner_first = true; owner_second = null; owner_third = [1, 2, 3, ...]",
    "key": null,
    "params": null
  },
  {
    "kind": "set_variable",
    "cfg": {
      "assignments": {
        "owner_first": true,
        "owner_second": null,
        "owner_third": {
          "source": "state.owner"
        },
        "owner_fourth": "hidden"
      }
    },
    "raw": "owner_first = true; owner_second = null; owner_third = {source: state.owner}; +1 more",
    "key": "react.workflows.set_variable_more_summary",
    "params": {
      "assignments": "owner_first = true; owner_second = null; owner_third = {source: state.owner}",
      "extra_count": 1
    },
    "lt": "owner_first = true; owner_second = null; owner_third = {source: state.owner}; +1 papildomų"
  },
  {
    "kind": "set_variable",
    "cfg": {
      "assignments": {
        "z_last": "first",
        "a_first": false,
        "variable": "more",
        "hidden1": 1,
        "hidden2": 2,
        "hidden3": 3
      }
    },
    "raw": "z_last = first; a_first = false; variable = more; +3 more",
    "key": "react.workflows.set_variable_more_summary",
    "params": {
      "assignments": "z_last = first; a_first = false; variable = more",
      "extra_count": 3
    },
    "lt": "z_last = first; a_first = false; variable = more; +3 papildomų"
  },
  {
    "kind": "set_variable",
    "cfg": {
      "assignments": {},
      "target": "state.owner",
      "value": 0
    },
    "raw": "state.owner = 0",
    "key": null,
    "params": null
  },
  {
    "kind": "set_variable",
    "cfg": {
      "assignments": "malformed",
      "value": "owner"
    },
    "raw": "variable = owner",
    "key": "react.workflows.set_variable_default_summary",
    "params": {
      "value": "owner"
    },
    "lt": "kintamasis = owner"
  },
  {
    "kind": "file_from_base64",
    "cfg": {
      "source": " state.owner ",
      "filename": " owner-report.pdf "
    },
    "raw": "state.owner -> file owner-report.pdf",
    "key": "react.workflows.base64_file_named_summary",
    "params": {
      "source": "state.owner",
      "filename": "owner-report.pdf"
    },
    "lt": "state.owner -> failas owner-report.pdf"
  },
  {
    "kind": "file_from_base64",
    "cfg": {
      "source": "state.owner"
    },
    "raw": "state.owner -> file",
    "key": "react.workflows.base64_file_summary",
    "params": {
      "source": "state.owner",
      "filename": ""
    },
    "lt": "state.owner -> failas"
  },
  {
    "kind": "file_from_base64",
    "cfg": {
      "filename": "file"
    },
    "raw": "base64 -> file file",
    "key": "react.workflows.base64_file_named_summary",
    "params": {
      "source": "base64",
      "filename": "file"
    },
    "lt": "base64 -> failas file"
  },
  {
    "kind": "file_from_base64",
    "cfg": {},
    "raw": "base64 -> file",
    "key": "react.workflows.base64_file_summary",
    "params": {
      "source": "base64",
      "filename": ""
    },
    "lt": "base64 -> failas"
  },
  {
    "kind": "file_from_base64",
    "cfg": {
      "source": "base64",
      "filename": "{{ owner.filename }}.txt"
    },
    "raw": "base64 -> file {{ owner.filename }}.txt",
    "key": "react.workflows.base64_file_named_summary",
    "params": {
      "source": "base64",
      "filename": "{{ owner.filename }}.txt"
    },
    "lt": "base64 -> failas {{ owner.filename }}.txt"
  },
  {
    "kind": "template",
    "cfg": {
      "format": "markdown",
      "result_target": " state.owner "
    },
    "raw": "markdown template -> state.owner",
    "key": "react.workflows.template_summary",
    "params": {
      "format": "markdown",
      "target": "state.owner"
    },
    "lt": "markdown šablonas -> state.owner"
  },
  {
    "kind": "template",
    "cfg": {
      "format": "html",
      "result_target": "text"
    },
    "raw": "html template -> text",
    "key": "react.workflows.template_summary",
    "params": {
      "format": "html",
      "target": "text"
    },
    "lt": "html šablonas -> text"
  },
  {
    "kind": "template",
    "cfg": {
      "result_target": "state.owner"
    },
    "raw": "text template -> state.owner",
    "key": "react.workflows.template_summary",
    "params": {
      "format": "text",
      "target": "state.owner"
    },
    "lt": "text šablonas -> state.owner"
  },
  {
    "kind": "template",
    "cfg": {},
    "raw": "text template -> text",
    "key": "react.workflows.template_default_target_summary",
    "params": {
      "format": "text",
      "target": ""
    },
    "lt": "text šablonas -> tekstas"
  },
  {
    "kind": "template",
    "cfg": {
      "format": " owner-format ",
      "result_target": " "
    },
    "raw": " owner-format  template -> text",
    "key": "react.workflows.template_default_target_summary",
    "params": {
      "format": " owner-format ",
      "target": ""
    },
    "lt": " owner-format  šablonas -> tekstas"
  },
  {
    "kind": "template",
    "cfg": {
      "format": "text",
      "result_target": "template"
    },
    "raw": "text template -> template",
    "key": "react.workflows.template_summary",
    "params": {
      "format": "text",
      "target": "template"
    },
    "lt": "text šablonas -> template"
  }
]""")


@pytest.mark.parametrize("case", CASES)
def test_value_file_template_summary_localizes_only_proven_platform_prose(case):
    step = {
        "id": "owner-step-id",
        "step_id": "owner-step-key",
        "display_name": "Owner value-file-template step",
        "step_kind": case["kind"],
        "override": {case["kind"]: case["cfg"]},
    }
    workflow = {
        "workflow_id": "owner-workflow",
        "enabled": False,
        "settings": {
            "execution_graph": {
                "schema_version": 1,
                "entry": {"kind": "step", "id": step["step_id"]},
                "edges": [],
            }
        },
        "steps": [step],
        "groups": [],
    }
    original = copy.deepcopy(workflow)
    db = SimpleNamespace(
        get_workflow=lambda _workflow_id: workflow,
        list_workflow_reactions=lambda *_args, **_kwargs: [],
    )
    with patch(
        "core.application.workflows.canvas_graph_service.validate_workflow_readiness",
        return_value={"valid": True, "errors": [], "warnings": []},
    ):
        row = build_workflow_canvas_graph(db, "owner-workflow")["steps"][0]
    assert row["config_summary"] == case["raw"]
    if case["key"] is None:
        assert "config_summary_key" not in row
        assert "config_summary_params" not in row
    else:
        assert row.get("config_summary_key") == case["key"]
        assert row.get("config_summary_params") == case["params"]
    assert row["id"] == "owner-step-key"
    assert row["step_id"] == "owner-step-key"
    assert row["display_name"] == "Owner value-file-template step"
    assert row["kind"] == case["kind"]
    assert workflow == original


@pytest.mark.parametrize("locale", ["en", "lt"])
@pytest.mark.parametrize("case", [case for case in CASES if case["key"] is not None])
def test_actual_compiled_value_file_template_catalog_preserves_machine_values(locale, case):
    reset_ui_i18n_cache()
    try:
        assert (
            translate_ui_text(case["key"], locale=locale, params=case["params"])
            == case["lt" if locale == "lt" else "raw"]
        )
    finally:
        reset_ui_i18n_cache()


@pytest.mark.parametrize(
    ("step_kind", "override"),
    [
        ("set_variable", {"set_variable": {"assignments": {}}}),
        ("collect", {"collect": {"target": "", "mode": "append", "source": ""}}),
    ],
)
def test_an_unconfigured_step_says_so_instead_of_a_sample_assignment(step_kind, override):
    # R2-20: a new Set variable read "variable = null" and a new Collect "value -> variable".
    from core.application.workflows.canvas_graph_common import (
        _step_config_summary,
        _step_config_summary_metadata,
    )

    step = {"step_id": "s", "step_kind": step_kind, "override": override}
    assert _step_config_summary(step, workflow_id="wf") == "Not set up yet"
    assert _step_config_summary_metadata(step, workflow_id="wf") == {
        "config_summary_key": "react.workflows.step_not_set_up_summary",
        "config_summary_params": {},
    }
