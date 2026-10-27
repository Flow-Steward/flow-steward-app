"""The platform executes what an extension declares, or says it will not.

RS5-014: a connection schema could declare conditional requirements with
allOf / if / then and the platform would accept a connection satisfying none of
them. The validator was a hand-written subset that skipped anything unfamiliar,
so the declaration was decoration and the author had no way to know.

Two layers are pinned here, in the order they matter:
1. A schema using a keyword the platform cannot execute is rejected at install
   time rather than accepted and ignored.
2. The keywords authors actually write - oneOf/anyOf for alternative types,
   allOf + if/then and dependentRequired for conditional requirements - are
   executed.
"""

from __future__ import annotations

import pytest

from core.application.extensions.json_schema_subset import (
    ANNOTATION_KEYWORDS,
    EXECUTED_KEYWORDS,
    unsupported_schema_keywords,
    validate_json_schema_value,
)


#: The two shapes the platform's validator has to execute, written here rather
#: than read from a bundle: what is under test is the validator. That an extension's
#: own schemas are executable is checked by `extensions validate` in its repository.
CONDITIONAL_SCHEMA = {
    "type": "object",
    "properties": {
        "prebuilt": {"type": "string", "enum": ["postgres", "sqlite"]},
        "host": {"type": "string"},
        "database": {"type": "string"},
        "user": {"type": "string"},
    },
    "required": ["prebuilt"],
    "allOf": [
        {
            "if": {"properties": {"prebuilt": {"const": "postgres"}}},
            "then": {"required": ["host", "database", "user"]},
        },
        {
            "if": {"properties": {"prebuilt": {"const": "sqlite"}}},
            "then": {"required": ["database"]},
        },
    ],
}
ALTERNATIVE_SCHEMA = {
    "type": "object",
    "properties": {
        "to": {
            "oneOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "string"}},
            ]
        }
    },
}


def _errors(schema: dict, value: object) -> list[str]:
    collected: list[str] = []
    validate_json_schema_value(schema=schema, value=value, path="config", errors=collected)
    return collected


def _missing_fields(errors: list[str]) -> set[str]:
    return {
        error.split(":", 1)[0].rsplit(".", 1)[-1]
        for error in errors
        if "required property is missing" in error
    }


class TestUnexecutableDeclarationsAreRefused:
    def test_a_keyword_the_platform_cannot_run_is_named(self) -> None:
        findings = unsupported_schema_keywords(
            {"type": "object", "patternProperties": {"^x": {"type": "string"}}}
        )

        assert findings
        assert "patternProperties" in findings[0]

    def test_the_refusal_reaches_nested_positions(self) -> None:
        findings = unsupported_schema_keywords(
            {
                "type": "object",
                "properties": {"a": {"type": "array", "minContains": 1}},
            }
        )

        assert findings
        assert "properties.a" in findings[0]

    def test_pattern_and_not_are_executed_assertions(self) -> None:
        schema = {
            "type": "object",
            "properties": {"fingerprint": {"type": "string", "pattern": "^SHA256:"}},
            "not": {"required": ["password", "private_key"]},
        }

        assert not unsupported_schema_keywords(schema)
        assert _errors(schema, {"fingerprint": "SHA256:abc"}) == []
        assert _errors(schema, {"fingerprint": "md5:abc"})
        assert _errors(
            schema,
            {"fingerprint": "SHA256:abc", "password": "p", "private_key": "k"},
        )

    def test_annotations_are_allowed_because_they_assert_nothing(self) -> None:
        assert not unsupported_schema_keywords(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "title": "t",
                "description": "d",
                "default": "x",
                "examples": ["x"],
                "format": "email",
                "type": "string",
            }
        )

    def test_the_two_keyword_sets_do_not_overlap(self) -> None:
        assert not (EXECUTED_KEYWORDS & ANNOTATION_KEYWORDS)


class TestConditionalRequirementsAreExecuted:
    def test_a_host_addressed_source_must_name_its_host(self) -> None:
        schema = CONDITIONAL_SCHEMA

        missing = _missing_fields(_errors(schema, {"prebuilt": "postgres"}))

        assert {"host", "database", "user"} <= missing

    def test_the_same_source_fully_specified_is_accepted(self) -> None:
        schema = CONDITIONAL_SCHEMA

        assert not _errors(
            schema, {"prebuilt": "postgres", "host": "db", "database": "app", "user": "svc"}
        )

    def test_a_file_backed_source_is_not_asked_for_a_host(self) -> None:
        schema = CONDITIONAL_SCHEMA

        missing = _missing_fields(_errors(schema, {"prebuilt": "sqlite"}))

        assert missing == {"database"}

    @pytest.mark.parametrize(
        ("config", "expected_missing"),
        [
            ({"prebuilt": "postgres"}, {"host", "database", "user"}),
            ({"prebuilt": "sqlite"}, {"database"}),
        ],
    )
    def test_each_shape_names_exactly_what_it_needs(
        self, config: dict, expected_missing: set[str]
    ) -> None:
        """Each branch must name its own requirements, not another branch's."""
        assert _missing_fields(_errors(CONDITIONAL_SCHEMA, config)) == expected_missing

    def test_optional_fields_stay_optional(self) -> None:
        """No password among them: Cloud SQL and AlloyDB authenticate via IAM."""
        schema = CONDITIONAL_SCHEMA

        complete = {"prebuilt": "postgres", "host": "db", "database": "app", "user": "svc"}

        assert not _errors(schema, complete)
        for optional in ("port", "query_params", "protocol", "ip_type", "schema", "role"):
            assert optional not in _missing_fields(_errors(schema, complete))

    def test_dependent_required_is_executed(self) -> None:
        schema = {"type": "object", "dependentRequired": {"user": ["password"]}}

        assert not _errors(schema, {})
        assert _missing_fields(_errors(schema, {"user": "svc"})) == {"password"}


class TestAlternativeTypesAreExecuted:
    def test_the_shipped_recipient_alternative_now_rejects_a_number(self) -> None:
        """This oneOf shipped unexecuted: `to: 42` was accepted."""
        schema = ALTERNATIVE_SCHEMA

        assert _errors(schema, {"to": 42})
        assert _errors(schema, {"to": [42]})

    @pytest.mark.parametrize("recipients", ["a@b.c", ["a@b.c", "d@e.f"]])
    def test_both_declared_shapes_are_accepted(self, recipients: object) -> None:
        schema = ALTERNATIVE_SCHEMA

        assert not _errors(schema, {"to": recipients})

    def test_the_reported_branch_matches_the_type_that_was_sent(self) -> None:
        """A list value must be told about the list branch, not the scalar one."""
        schema = {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]}

        errors = _errors(schema, [42])

        assert errors == ["config[0]: expected type string"]




class TestTheAuthorFacingToolAnswersTheSchemaQuestion:
    """`validate_extensions` had no notion of schemas at all.

    Narrow on purpose: the tool is not asked to validate schemas, only to answer
    the one question an author cannot answer alone - will the platform run what
    I declared? Full schema validation in this tool is a separate task.
    """

    def test_an_unexecutable_keyword_fails_the_run(self, tmp_path) -> None:
        from core.application.tooling.extensions.validate_extensions import (
            _unexecutable_schema_errors,
        )

        (tmp_path / "broken.schema.yaml").write_text(
            "type: object\npatternProperties:\n  '^x': {type: string}\n",
            encoding="utf-8",
        )

        findings = _unexecutable_schema_errors(tmp_path)

        assert findings
        assert "patternProperties" in findings[0]

    def test_an_inline_manifest_schema_is_checked_too(self, tmp_path) -> None:
        """The inline copy is what the platform serves, so it is what matters."""
        from core.application.tooling.extensions.validate_extensions import (
            _unexecutable_schema_errors,
        )

        (tmp_path / "extension.yaml").write_text(
            "runtime:\n"
            "  connectors:\n"
            "    - connection_schema:\n"
            "        type: object\n"
            "        patternProperties:\n"
            "          '^x': {type: string}\n",
            encoding="utf-8",
        )

        findings = _unexecutable_schema_errors(tmp_path)

        assert findings
        assert "extension.yaml" in findings[0]
