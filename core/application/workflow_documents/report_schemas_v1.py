"""The two JSON Schemas of workflow report schema v1 (draft 2020-12).

The text is the contract published in ``docs/architecture/workflow-report-schema-v1.md``
section 9; ``tests/unit/workflow_documents/test_report_schemas.py`` fails when the two
copies differ. The schemas live in a Python module, not in data files, so every
runtime that imports the validators also carries them.
"""

from __future__ import annotations

import json
from typing import Any

DEFINITION_SCHEMA_V1_JSON = """{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "urn:flow-steward:workflow-report:definition:1",
  "title": "Flow Steward ReportDefinition v1",
  "type": "object",
  "additionalProperties": false,
  "required": ["schema_version", "title", "locale", "time_zone", "components"],
  "properties": {
    "schema_version": {"const": 1},
    "title": {"$ref": "#/$defs/label"},
    "locale": {"$ref": "#/$defs/locale"},
    "time_zone": {"$ref": "#/$defs/time_zone"},
    "summary": {"$ref": "#/$defs/segments"},
    "datasets": {"type": "array", "maxItems": 10, "items": {"$ref": "#/$defs/dataset"}},
    "components": {"$ref": "#/$defs/components"}
  },
  "$defs": {
    "id": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,63}$"},
    "label": {"type": "string", "minLength": 1, "maxLength": 200},
    "note": {"type": "string", "minLength": 1, "maxLength": 1000},
    "locale": {"type": "string", "pattern": "^[a-z]{2,3}(-[A-Z][a-z]{3})?(-([A-Z]{2}|[0-9]{3}))?$"},
    "time_zone": {"type": "string", "minLength": 1, "maxLength": 64, "pattern": "^[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*$"},
    "field": {"type": "string", "minLength": 1, "maxLength": 128},
    "fraction_digits": {"type": "integer", "minimum": 0, "maximum": 6},
    "format_text": {"type": "object", "additionalProperties": false, "required": ["type"], "properties": {"type": {"const": "text"}}},
    "format_boolean": {"type": "object", "additionalProperties": false, "required": ["type"], "properties": {"type": {"const": "boolean"}}},
    "format_date": {"type": "object", "additionalProperties": false, "required": ["type"], "properties": {"type": {"const": "date"}}},
    "format_datetime": {"type": "object", "additionalProperties": false, "required": ["type"], "properties": {"type": {"const": "datetime"}}},
    "format_number": {
      "type": "object", "additionalProperties": false, "required": ["type"],
      "properties": {
        "type": {"const": "number"},
        "minimum_fraction_digits": {"$ref": "#/$defs/fraction_digits"},
        "maximum_fraction_digits": {"$ref": "#/$defs/fraction_digits"}
      }
    },
    "format_percent": {
      "type": "object", "additionalProperties": false, "required": ["type"],
      "properties": {
        "type": {"const": "percent"},
        "minimum_fraction_digits": {"$ref": "#/$defs/fraction_digits"},
        "maximum_fraction_digits": {"$ref": "#/$defs/fraction_digits"}
      }
    },
    "format_currency": {
      "type": "object", "additionalProperties": false, "required": ["type", "currency"],
      "properties": {
        "type": {"const": "currency"},
        "currency": {"type": "string", "pattern": "^[A-Z]{3}$"},
        "minimum_fraction_digits": {"$ref": "#/$defs/fraction_digits"},
        "maximum_fraction_digits": {"$ref": "#/$defs/fraction_digits"}
      }
    },
    "format": {
      "oneOf": [
        {"$ref": "#/$defs/format_text"}, {"$ref": "#/$defs/format_boolean"},
        {"$ref": "#/$defs/format_number"}, {"$ref": "#/$defs/format_currency"},
        {"$ref": "#/$defs/format_percent"}, {"$ref": "#/$defs/format_date"},
        {"$ref": "#/$defs/format_datetime"}
      ]
    },
    "numeric_format": {
      "oneOf": [
        {"$ref": "#/$defs/format_number"}, {"$ref": "#/$defs/format_currency"},
        {"$ref": "#/$defs/format_percent"}
      ]
    },
    "category_format": {
      "oneOf": [
        {"$ref": "#/$defs/format_text"}, {"$ref": "#/$defs/format_number"},
        {"$ref": "#/$defs/format_date"}, {"$ref": "#/$defs/format_datetime"}
      ]
    },
    "input_name": {"type": "string", "pattern": "^[A-Za-z_][A-Za-z0-9_]{0,127}$"},
    "relative_path": {"type": "string", "minLength": 1, "maxLength": 512},
    "value_binding": {
      "type": "object", "additionalProperties": false, "required": ["input"],
      "properties": {
        "input": {"$ref": "#/$defs/input_name"},
        "path": {"$ref": "#/$defs/relative_path"},
        "default": {"type": ["string", "number", "boolean", "null"], "maxLength": 10000}
      }
    },
    "dataset_binding": {
      "type": "object", "additionalProperties": false, "required": ["input"],
      "properties": {
        "input": {"$ref": "#/$defs/input_name"},
        "path": {"$ref": "#/$defs/relative_path"},
        "default": {"type": "array", "maxItems": 0}
      }
    },
    "segment": {
      "oneOf": [
        {
          "type": "object", "additionalProperties": false, "required": ["text"],
          "properties": {"text": {"type": "string", "minLength": 1, "maxLength": 10000}}
        },
        {
          "type": "object", "additionalProperties": false, "required": ["value", "format"],
          "properties": {"value": {"$ref": "#/$defs/value_binding"}, "format": {"$ref": "#/$defs/format"}}
        }
      ]
    },
    "segments": {"type": "array", "minItems": 1, "maxItems": 100, "items": {"$ref": "#/$defs/segment"}},
    "dataset": {
      "type": "object", "additionalProperties": false, "required": ["id", "source"],
      "properties": {"id": {"$ref": "#/$defs/id"}, "source": {"$ref": "#/$defs/dataset_binding"}}
    },
    "components": {"type": "array", "minItems": 1, "maxItems": 100, "items": {"$ref": "#/$defs/component"}},
    "component": {
      "oneOf": [
        {"$ref": "#/$defs/text_component"}, {"$ref": "#/$defs/metric_component"},
        {"$ref": "#/$defs/table_component"}, {"$ref": "#/$defs/chart_component"},
        {"$ref": "#/$defs/section_component"}
      ]
    },
    "text_component": {
      "type": "object", "additionalProperties": false, "required": ["id", "type", "style", "segments"],
      "properties": {
        "id": {"$ref": "#/$defs/id"},
        "type": {"const": "text"},
        "style": {"enum": ["paragraph", "heading"]},
        "segments": {"$ref": "#/$defs/segments"}
      }
    },
    "metric_component": {
      "type": "object", "additionalProperties": false, "required": ["id", "type", "label", "value", "format"],
      "properties": {
        "id": {"$ref": "#/$defs/id"},
        "type": {"const": "metric"},
        "label": {"$ref": "#/$defs/label"},
        "value": {"$ref": "#/$defs/value_binding"},
        "format": {"$ref": "#/$defs/numeric_format"},
        "comparison": {
          "type": "object", "additionalProperties": false, "required": ["label", "value", "format"],
          "properties": {
            "label": {"$ref": "#/$defs/label"},
            "value": {"$ref": "#/$defs/value_binding"},
            "format": {"$ref": "#/$defs/numeric_format"}
          }
        }
      }
    },
    "table_column": {
      "type": "object", "additionalProperties": false, "required": ["id", "field", "label", "format"],
      "properties": {
        "id": {"$ref": "#/$defs/id"},
        "field": {"$ref": "#/$defs/field"},
        "label": {"$ref": "#/$defs/label"},
        "format": {"$ref": "#/$defs/format"}
      }
    },
    "table_component": {
      "type": "object", "additionalProperties": false,
      "required": ["id", "type", "caption", "dataset", "columns", "empty_text"],
      "properties": {
        "id": {"$ref": "#/$defs/id"},
        "type": {"const": "table"},
        "caption": {"$ref": "#/$defs/label"},
        "dataset": {"$ref": "#/$defs/id"},
        "columns": {"type": "array", "minItems": 1, "maxItems": 30, "items": {"$ref": "#/$defs/table_column"}},
        "empty_text": {"$ref": "#/$defs/note"}
      }
    },
    "chart_category": {
      "type": "object", "additionalProperties": false, "required": ["field", "label", "format"],
      "properties": {
        "field": {"$ref": "#/$defs/field"},
        "label": {"$ref": "#/$defs/label"},
        "format": {"$ref": "#/$defs/category_format"}
      }
    },
    "chart_series": {
      "type": "object", "additionalProperties": false, "required": ["id", "field", "label", "format"],
      "properties": {
        "id": {"$ref": "#/$defs/id"},
        "field": {"$ref": "#/$defs/field"},
        "label": {"$ref": "#/$defs/label"},
        "format": {"$ref": "#/$defs/numeric_format"}
      }
    },
    "chart_component": {
      "type": "object", "additionalProperties": false,
      "required": ["id", "type", "chart_type", "title", "description", "dataset", "category", "series", "empty_text"],
      "properties": {
        "id": {"$ref": "#/$defs/id"},
        "type": {"const": "chart"},
        "chart_type": {"enum": ["line", "bar"]},
        "title": {"$ref": "#/$defs/label"},
        "description": {"$ref": "#/$defs/note"},
        "dataset": {"$ref": "#/$defs/id"},
        "category": {"$ref": "#/$defs/chart_category"},
        "series": {"type": "array", "minItems": 1, "maxItems": 10, "items": {"$ref": "#/$defs/chart_series"}},
        "empty_text": {"$ref": "#/$defs/note"}
      }
    },
    "section_component": {
      "type": "object", "additionalProperties": false, "required": ["id", "type", "title", "children"],
      "properties": {
        "id": {"$ref": "#/$defs/id"},
        "type": {"const": "section"},
        "title": {"$ref": "#/$defs/label"},
        "children": {"$ref": "#/$defs/components"}
      }
    }
  }
}"""

SNAPSHOT_SCHEMA_V1_JSON = """{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "urn:flow-steward:workflow-report:snapshot:1",
  "title": "Flow Steward ReportSnapshot v1",
  "type": "object",
  "additionalProperties": false,
  "required": ["kind", "schema_version", "title", "locale", "time_zone", "summary_text", "datasets", "components"],
  "properties": {
    "kind": {"const": "fs.report"},
    "schema_version": {"const": 1},
    "title": {"$ref": "#/$defs/label"},
    "locale": {"$ref": "#/$defs/locale"},
    "time_zone": {"$ref": "#/$defs/time_zone"},
    "summary_text": {"type": "string", "minLength": 1, "maxLength": 4000},
    "datasets": {"type": "array", "maxItems": 10, "items": {"$ref": "#/$defs/dataset"}},
    "components": {"$ref": "#/$defs/components"}
  },
  "$defs": {
    "id": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,63}$"},
    "label": {"type": "string", "minLength": 1, "maxLength": 200},
    "note": {"type": "string", "minLength": 1, "maxLength": 1000},
    "locale": {"type": "string", "pattern": "^[a-z]{2,3}(-[A-Z][a-z]{3})?(-([A-Z]{2}|[0-9]{3}))?$"},
    "time_zone": {"type": "string", "minLength": 1, "maxLength": 64, "pattern": "^[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*$"},
    "field": {"type": "string", "minLength": 1, "maxLength": 128},
    "scalar": {"type": ["string", "number", "boolean", "null"], "maxLength": 10000},
    "number_or_null": {"type": ["number", "null"]},
    "fraction_digits": {"type": "integer", "minimum": 0, "maximum": 6},
    "format_text": {"type": "object", "additionalProperties": false, "required": ["type"], "properties": {"type": {"const": "text"}}},
    "format_boolean": {"type": "object", "additionalProperties": false, "required": ["type"], "properties": {"type": {"const": "boolean"}}},
    "format_date": {"type": "object", "additionalProperties": false, "required": ["type"], "properties": {"type": {"const": "date"}}},
    "format_datetime": {"type": "object", "additionalProperties": false, "required": ["type"], "properties": {"type": {"const": "datetime"}}},
    "format_number": {
      "type": "object", "additionalProperties": false, "required": ["type"],
      "properties": {
        "type": {"const": "number"},
        "minimum_fraction_digits": {"$ref": "#/$defs/fraction_digits"},
        "maximum_fraction_digits": {"$ref": "#/$defs/fraction_digits"}
      }
    },
    "format_percent": {
      "type": "object", "additionalProperties": false, "required": ["type"],
      "properties": {
        "type": {"const": "percent"},
        "minimum_fraction_digits": {"$ref": "#/$defs/fraction_digits"},
        "maximum_fraction_digits": {"$ref": "#/$defs/fraction_digits"}
      }
    },
    "format_currency": {
      "type": "object", "additionalProperties": false, "required": ["type", "currency"],
      "properties": {
        "type": {"const": "currency"},
        "currency": {"type": "string", "pattern": "^[A-Z]{3}$"},
        "minimum_fraction_digits": {"$ref": "#/$defs/fraction_digits"},
        "maximum_fraction_digits": {"$ref": "#/$defs/fraction_digits"}
      }
    },
    "format": {
      "oneOf": [
        {"$ref": "#/$defs/format_text"}, {"$ref": "#/$defs/format_boolean"},
        {"$ref": "#/$defs/format_number"}, {"$ref": "#/$defs/format_currency"},
        {"$ref": "#/$defs/format_percent"}, {"$ref": "#/$defs/format_date"},
        {"$ref": "#/$defs/format_datetime"}
      ]
    },
    "numeric_format": {
      "oneOf": [
        {"$ref": "#/$defs/format_number"}, {"$ref": "#/$defs/format_currency"},
        {"$ref": "#/$defs/format_percent"}
      ]
    },
    "category_format": {
      "oneOf": [
        {"$ref": "#/$defs/format_text"}, {"$ref": "#/$defs/format_number"},
        {"$ref": "#/$defs/format_date"}, {"$ref": "#/$defs/format_datetime"}
      ]
    },
    "dataset": {
      "type": "object", "additionalProperties": false, "required": ["id", "fields", "rows"],
      "properties": {
        "id": {"$ref": "#/$defs/id"},
        "fields": {"type": "array", "minItems": 1, "maxItems": 30, "uniqueItems": true, "items": {"$ref": "#/$defs/field"}},
        "rows": {
          "type": "array", "maxItems": 1000,
          "items": {"type": "array", "minItems": 1, "maxItems": 30, "items": {"$ref": "#/$defs/scalar"}}
        }
      }
    },
    "segment": {
      "oneOf": [
        {
          "type": "object", "additionalProperties": false, "required": ["text"],
          "properties": {"text": {"type": "string", "minLength": 1, "maxLength": 10000}}
        },
        {
          "type": "object", "additionalProperties": false, "required": ["value", "format"],
          "properties": {"value": {"$ref": "#/$defs/scalar"}, "format": {"$ref": "#/$defs/format"}}
        }
      ]
    },
    "components": {"type": "array", "minItems": 1, "maxItems": 100, "items": {"$ref": "#/$defs/component"}},
    "component": {
      "oneOf": [
        {"$ref": "#/$defs/text_component"}, {"$ref": "#/$defs/metric_component"},
        {"$ref": "#/$defs/table_component"}, {"$ref": "#/$defs/chart_component"},
        {"$ref": "#/$defs/section_component"}
      ]
    },
    "text_component": {
      "type": "object", "additionalProperties": false, "required": ["id", "type", "style", "segments"],
      "properties": {
        "id": {"$ref": "#/$defs/id"},
        "type": {"const": "text"},
        "style": {"enum": ["paragraph", "heading"]},
        "segments": {"type": "array", "minItems": 1, "maxItems": 100, "items": {"$ref": "#/$defs/segment"}}
      }
    },
    "metric_component": {
      "type": "object", "additionalProperties": false, "required": ["id", "type", "label", "value", "format"],
      "properties": {
        "id": {"$ref": "#/$defs/id"},
        "type": {"const": "metric"},
        "label": {"$ref": "#/$defs/label"},
        "value": {"$ref": "#/$defs/number_or_null"},
        "format": {"$ref": "#/$defs/numeric_format"},
        "comparison": {
          "type": "object", "additionalProperties": false, "required": ["label", "value", "format"],
          "properties": {
            "label": {"$ref": "#/$defs/label"},
            "value": {"$ref": "#/$defs/number_or_null"},
            "format": {"$ref": "#/$defs/numeric_format"}
          }
        }
      }
    },
    "table_column": {
      "type": "object", "additionalProperties": false, "required": ["id", "field", "label", "format"],
      "properties": {
        "id": {"$ref": "#/$defs/id"},
        "field": {"$ref": "#/$defs/field"},
        "label": {"$ref": "#/$defs/label"},
        "format": {"$ref": "#/$defs/format"}
      }
    },
    "table_component": {
      "type": "object", "additionalProperties": false,
      "required": ["id", "type", "caption", "dataset", "columns", "empty_text"],
      "properties": {
        "id": {"$ref": "#/$defs/id"},
        "type": {"const": "table"},
        "caption": {"$ref": "#/$defs/label"},
        "dataset": {"$ref": "#/$defs/id"},
        "columns": {"type": "array", "minItems": 1, "maxItems": 30, "items": {"$ref": "#/$defs/table_column"}},
        "empty_text": {"$ref": "#/$defs/note"}
      }
    },
    "chart_category": {
      "type": "object", "additionalProperties": false, "required": ["field", "label", "format"],
      "properties": {
        "field": {"$ref": "#/$defs/field"},
        "label": {"$ref": "#/$defs/label"},
        "format": {"$ref": "#/$defs/category_format"}
      }
    },
    "chart_series": {
      "type": "object", "additionalProperties": false, "required": ["id", "field", "label", "format"],
      "properties": {
        "id": {"$ref": "#/$defs/id"},
        "field": {"$ref": "#/$defs/field"},
        "label": {"$ref": "#/$defs/label"},
        "format": {"$ref": "#/$defs/numeric_format"}
      }
    },
    "chart_component": {
      "type": "object", "additionalProperties": false,
      "required": ["id", "type", "chart_type", "title", "description", "dataset", "category", "series", "empty_text"],
      "properties": {
        "id": {"$ref": "#/$defs/id"},
        "type": {"const": "chart"},
        "chart_type": {"enum": ["line", "bar"]},
        "title": {"$ref": "#/$defs/label"},
        "description": {"$ref": "#/$defs/note"},
        "dataset": {"$ref": "#/$defs/id"},
        "category": {"$ref": "#/$defs/chart_category"},
        "series": {"type": "array", "minItems": 1, "maxItems": 10, "items": {"$ref": "#/$defs/chart_series"}},
        "empty_text": {"$ref": "#/$defs/note"}
      }
    },
    "section_component": {
      "type": "object", "additionalProperties": false, "required": ["id", "type", "title", "children"],
      "properties": {
        "id": {"$ref": "#/$defs/id"},
        "type": {"const": "section"},
        "title": {"$ref": "#/$defs/label"},
        "children": {"$ref": "#/$defs/components"}
      }
    }
  }
}"""


def _load(text: str) -> dict[str, Any]:
    loaded = json.loads(text)
    if not isinstance(loaded, dict):  # pragma: no cover - the constants are objects
        raise TypeError("report schema must be a JSON object")
    return loaded


#: ReportDefinition v1. Treat as read-only.
DEFINITION_SCHEMA_V1: dict[str, Any] = _load(DEFINITION_SCHEMA_V1_JSON)
#: ReportSnapshot v1. Treat as read-only.
SNAPSHOT_SCHEMA_V1: dict[str, Any] = _load(SNAPSHOT_SCHEMA_V1_JSON)

__all__ = [
    "DEFINITION_SCHEMA_V1",
    "DEFINITION_SCHEMA_V1_JSON",
    "SNAPSHOT_SCHEMA_V1",
    "SNAPSHOT_SCHEMA_V1_JSON",
]
