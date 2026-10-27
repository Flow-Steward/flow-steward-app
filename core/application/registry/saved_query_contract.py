"""Application-owned validation contract for persisted dataset queries."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_TOKEN_QUERY_DEFINITION_JSONB_SQL = "query_definition_jsonb.sql"

_PARAMETER = re.compile(r"(?<!:):([A-Za-z_](?a:\w)*)")
# A FROM clause is read token by token so that every table it reaches is seen:
# quoted names, comma-separated lists, entries after a join condition, tables
# inside a parenthesized join (`from (a join b on ...), c`, MySQL `join (c)`),
# `LATERAL`/`ONLY` prefixes, MySQL `STRAIGHT_JOIN`, SQL Server `APPLY` and
# PostgreSQL `TABLE name`. A subquery's own FROM clause is read on its own.
_SQL_TOKEN = re.compile(
    r'(?P<quoted>"(?:[^"]|"")*"|`(?:[^`]|``)*`|\[[^\[\]()]*\])'
    r"|(?P<word>[A-Za-z_][A-Za-z0-9_$]*)"
    r"|(?P<punct>[(),.])"
    r"|(?P<other>\S)"
)
# Words after which the next name is a table: FROM, every JOIN spelling, SQL
# Server APPLY and PostgreSQL `TABLE name` (shorthand for `SELECT * FROM name`).
_TABLE_CLAUSE_STARTS = frozenset({"from", "table"})
_JOIN_WORDS = frozenset({"join", "straight_join"})
# Words that may stand between FROM/JOIN and the table they qualify.
_TABLE_PREFIXES = frozenset({"lateral", "only"})
# A parenthesized entry starting with one of these is a derived table.
_DERIVED_TABLE_HEADS = frozenset({"select", "table", "values", "with"})
# Words that end a FROM clause.
_FROM_CLAUSE_END = frozenset(
    {
        "except",
        "fetch",
        "for",
        "group",
        "having",
        "intersect",
        "limit",
        "offset",
        "order",
        "qualify",
        "returning",
        "union",
        "where",
        "window",
    }
)


@dataclass(frozen=True)
class _SqlToken:
    kind: str
    text: str

    @property
    def keyword(self) -> str:
        return self.text.lower() if self.kind == "word" else ""

    @property
    def name(self) -> str:
        """The identifier this token spells, unquoted and lower-cased."""
        if self.kind == "quoted":
            closing = self.text[-1]
            return self.text[1:-1].replace(closing * 2, closing).lower()
        return self.text.lower()


def _sql_tokens(sql: str) -> list[_SqlToken]:
    tokens: list[_SqlToken] = []
    for match in _SQL_TOKEN.finditer(sql):
        kind = match.lastgroup or "other"
        tokens.append(_SqlToken(kind, match.group(0)))
    return tokens


def _skip_parentheses(tokens: list[_SqlToken], index: int) -> int:
    """Return the index after the parenthesis that closes the one at ``index``."""
    depth = 0
    while index < len(tokens):
        if tokens[index].text == "(":
            depth += 1
        elif tokens[index].text == ")":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return index


def _joins_a_table(tokens: list[_SqlToken], index: int) -> bool:
    """Whether the token at ``index`` is JOIN, STRAIGHT_JOIN or CROSS/OUTER APPLY."""
    keyword = tokens[index].keyword
    if keyword in _JOIN_WORDS:
        return True
    return keyword == "apply" and index > 0 and tokens[index - 1].keyword in {"cross", "outer"}


def _qualified_name(tokens: list[_SqlToken], index: int) -> tuple[str, int]:
    """Read ``name(.name)*`` at ``index``; return it normalized and the next index."""
    parts = [tokens[index].name]
    index += 1
    while index < len(tokens) and tokens[index].text == ".":
        while index < len(tokens) and tokens[index].text == ".":
            index += 1
        if index < len(tokens) and tokens[index].kind in {"word", "quoted"}:
            parts.append(tokens[index].name)
            index += 1
    return ".".join(part for part in parts if part), index


def _clause_resources(tokens: list[_SqlToken], index: int) -> set[str]:
    """Collect the tables of the FROM clause whose first entry starts at ``index``."""
    resources: set[str] = set()
    expect_table = True
    group_depth = 0
    while index < len(tokens):
        token = tokens[index]
        if expect_table:
            if token.text == "(":
                following = tokens[index + 1] if index + 1 < len(tokens) else None
                if following is not None and following.keyword in _DERIVED_TABLE_HEADS:
                    # A derived table: its own FROM clause is read separately.
                    index = _skip_parentheses(tokens, index)
                    expect_table = False
                else:
                    group_depth += 1
                    index += 1
                continue
            if token.keyword in _TABLE_PREFIXES:
                index += 1
                continue
            expect_table = False
            if token.kind in {"word", "quoted"}:
                name, index = _qualified_name(tokens, index)
                if name:
                    resources.add(name)
                if index < len(tokens) and tokens[index].text == "(":
                    index = _skip_parentheses(tokens, index)
            continue
        if token.text == ",":
            expect_table = True
        elif token.text == "(":
            # A join condition, a USING list, function arguments or a column alias
            # list: any subquery inside has its own FROM clause.
            index = _skip_parentheses(tokens, index)
            continue
        elif token.text == ")":
            if group_depth == 0:
                break
            group_depth -= 1
        elif _joins_a_table(tokens, index):
            expect_table = True
        elif token.keyword in _FROM_CLAUSE_END:
            break
        index += 1
    return resources


def _referenced_resources(sql: str) -> set[str]:
    """Collect every table a FROM/JOIN clause reaches, including comma-separated ones."""
    tokens = _sql_tokens(sql)
    resources: set[str] = set()
    for index, token in enumerate(tokens):
        if token.keyword in _TABLE_CLAUSE_STARTS or _joins_a_table(tokens, index):
            resources |= _clause_resources(tokens, index + 1)
    return resources


_FORBIDDEN_SQL_WORDS = frozenset(
    {
        "alter",
        "call",
        "copy",
        "create",
        "delete",
        "do",
        "drop",
        "dumpfile",
        "execute",
        "grant",
        "insert",
        # SELECT ... INTO creates a table (PostgreSQL, SQL Server) or writes a
        # file on the database server (MySQL INTO OUTFILE / DUMPFILE).
        "into",
        "merge",
        "outfile",
        "revoke",
        "truncate",
        "update",
    }
)
# Row locks taken by a "read": FOR UPDATE / FOR SHARE / FOR NO KEY UPDATE /
# FOR KEY SHARE (PostgreSQL), LOCK IN SHARE MODE (MySQL).
_SQL_FOR_LOCK = re.compile(
    r"\bfor\s+(?:no\s+key\s+update|key\s+share|update|share)\b", re.IGNORECASE
)
_SQL_SHARE_MODE_LOCK = re.compile(r"\block\s+in\s+share\s+mode\b", re.IGNORECASE)
# Functions a SELECT can call that change state or reach outside the database.
_SQL_SIDE_EFFECT_CALL = re.compile(
    r"\b(?:nextval|setval|set_config|pg_\w+|lo_\w+|dblink\w*|load_file|sleep|benchmark"
    r"|xp_\w+|sp_\w+)\s*\(",
    re.IGNORECASE,
)
_JSON_SCHEMA_TYPES = frozenset(
    {"array", "boolean", "integer", "null", "number", "object", "string"}
)


@dataclass
class SavedQueryContractError(ValueError):
    """A safe, transport-independent saved-query validation failure."""

    code: str
    path: str
    message: str

    def __str__(self) -> str:
        return self.message

    def as_detail(self) -> dict[str, str]:
        return {
            "code": self.code,
            "field": self.path.split(".", 1)[0],
            "path": self.path,
            "message": self.message,
        }


def _invalid(path: str, message: str) -> SavedQueryContractError:
    return SavedQueryContractError(
        code="saved_query_definition_invalid",
        path=path,
        message=message,
    )


def _sql_without_comments_or_literals(sql: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(sql):
        if sql.startswith("--", index):
            newline = sql.find("\n", index + 2)
            index = len(sql) if newline < 0 else newline + 1
            result.append(" ")
            continue
        if sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            if end < 0:
                raise _invalid(
                    _TOKEN_QUERY_DEFINITION_JSONB_SQL, "SQL block comment is unterminated"
                )
            index = end + 2
            result.append(" ")
            continue
        if sql[index] == "'":
            index += 1
            while index < len(sql):
                if sql[index] == "'":
                    if index + 1 < len(sql) and sql[index + 1] == "'":
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            else:
                raise _invalid(
                    _TOKEN_QUERY_DEFINITION_JSONB_SQL, "SQL string literal is unterminated"
                )
            result.append(" ")
            continue
        result.append(sql[index])
        index += 1
    return "".join(result)


def _named_parameters(sql: str) -> set[str]:
    """Return single-colon parameters outside PostgreSQL quoted identifiers."""

    parameters: set[str] = set()
    index = 0
    while index < len(sql):
        if sql[index] == '"':
            index += 1
            while index < len(sql):
                if sql[index] == '"':
                    if index + 1 < len(sql) and sql[index + 1] == '"':
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            continue
        match = _PARAMETER.match(sql, index)
        if match is not None:
            parameters.add(match.group(1))
            index = match.end()
            continue
        index += 1
    return parameters


def _validate_params_schema(schema: Any) -> set[str]:
    path = "params_schema_jsonb"
    if not isinstance(schema, dict):
        raise _invalid(path, "Parameter schema must be an object")
    if not schema:
        return set()
    unknown = sorted(set(schema) - {"type", "properties", "required", "additionalProperties"})
    if unknown:
        raise _invalid(f"{path}.{unknown[0]}", "Unsupported parameter schema field")
    if schema.get("type", "object") != "object":
        raise _invalid(f"{path}.type", "Parameter schema type must be object")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        raise _invalid(f"{path}.properties", "Parameter schema properties must be an object")
    for name, definition in properties.items():
        property_path = f"{path}.properties.{name}"
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_](?a:\w)*", name):
            raise _invalid(property_path, "Parameter names must be identifiers")
        if not isinstance(definition, dict):
            raise _invalid(property_path, "Parameter definition must be an object")
        parameter_type = definition.get("type")
        if parameter_type not in _JSON_SCHEMA_TYPES:
            raise _invalid(f"{property_path}.type", "Parameter type is missing or unsupported")
    required = schema.get("required", [])
    if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
        raise _invalid(f"{path}.required", "Required parameters must be a list of names")
    undeclared_required = sorted(set(required) - set(properties))
    if undeclared_required:
        raise _invalid(
            f"{path}.required",
            f"Required parameter '{undeclared_required[0]}' is not declared",
        )
    additional = schema.get("additionalProperties", True)
    if not isinstance(additional, bool):
        raise _invalid(
            f"{path}.additionalProperties",
            "additionalProperties must be a boolean",
        )
    return set(properties)


def _allowed_resources(dataset: dict[str, Any]) -> set[str]:
    scope = dataset.get("resource_scope_jsonb") or dataset.get("resource_scope") or {}
    if not scope:
        metadata = dataset.get("metadata_jsonb") or dataset.get("metadata") or {}
        if isinstance(metadata, dict):
            scope = metadata.get("resource_scope_jsonb") or metadata.get("resource_scope") or {}
    if not isinstance(scope, dict):
        return set()
    resources = scope.get("resources")
    if not isinstance(resources, list):
        return set()
    return {
        str(item.get("resource_id") or "").strip().lower()
        for item in resources
        if isinstance(item, dict) and str(item.get("resource_id") or "").strip()
    }


def _is_in_scope(reference: str, allowed_resources: set[str]) -> bool:
    """A reference names a declared table, written in full or with fewer qualifiers.

    `orders` and `public.orders` both name a declared `public.orders`, but
    `audit.orders` names another schema's table and is outside the scope.
    """
    return any(
        reference == allowed or allowed.endswith(f".{reference}") for allowed in allowed_resources
    )


def validate_saved_query_contract(
    payload: dict[str, Any],
    *,
    dataset: dict[str, Any],
) -> None:
    """Reject a saved query that cannot safely execute against its dataset."""

    language = str(payload.get("query_language") or "sql").strip().lower()
    if language != "sql":
        raise _invalid("query_language", "Only the sql query language is supported")

    if "query_definition" in payload:
        raise _invalid(
            "query_definition",
            "Legacy query_definition is unsupported; use query_definition_jsonb",
        )
    definition = payload.get("query_definition_jsonb") or {}
    if not isinstance(definition, dict):
        raise _invalid("query_definition_jsonb", "Query definition must be an object")
    unknown_definition_fields = sorted(set(definition) - {"sql"})
    if unknown_definition_fields:
        name = unknown_definition_fields[0]
        raise _invalid(
            f"query_definition_jsonb.{name}",
            f"Unsupported query definition field '{name}'; use 'sql'",
        )
    sql = str(definition.get("sql") or "").strip()
    if not sql:
        raise _invalid(_TOKEN_QUERY_DEFINITION_JSONB_SQL, "SQL is required")

    declared_params = _validate_params_schema(
        payload.get("params_schema_jsonb") or payload.get("param_schema_jsonb") or {}
    )
    sanitized = _sql_without_comments_or_literals(sql).strip()
    without_trailing_semicolon = sanitized[:-1].rstrip() if sanitized.endswith(";") else sanitized
    if ";" in without_trailing_semicolon:
        raise _invalid(_TOKEN_QUERY_DEFINITION_JSONB_SQL, "Only one SQL statement is allowed")
    words = {word.lower() for word in re.findall(r"\b[A-Za-z_](?a:\w)*\b", sanitized)}
    if (
        not re.match(r"(?is)^\s*select\b", sanitized)
        or words & _FORBIDDEN_SQL_WORDS
        or _SQL_FOR_LOCK.search(sanitized)
        or _SQL_SHARE_MODE_LOCK.search(sanitized)
        or _SQL_SIDE_EFFECT_CALL.search(sanitized)
    ):
        raise _invalid(
            _TOKEN_QUERY_DEFINITION_JSONB_SQL, "Only read-only SELECT statements are allowed"
        )
    if "where" not in words and "limit" not in words:
        raise _invalid(
            _TOKEN_QUERY_DEFINITION_JSONB_SQL, "Query must include a WHERE clause or LIMIT"
        )

    allowed_resources = _allowed_resources(dataset)
    if not allowed_resources:
        raise _invalid("dataset.resource_scope_jsonb", "Dataset resource scope is empty")
    referenced_resources = _referenced_resources(sanitized)
    outside_scope = sorted(
        resource
        for resource in referenced_resources
        if not _is_in_scope(resource, allowed_resources)
    )
    if outside_scope:
        raise _invalid(
            _TOKEN_QUERY_DEFINITION_JSONB_SQL,
            f"Query resource '{outside_scope[0]}' is outside the dataset scope",
        )

    placeholders = _named_parameters(sanitized)
    undeclared = sorted(placeholders - declared_params)
    if undeclared:
        raise _invalid(
            f"params_schema_jsonb.properties.{undeclared[0]}",
            f"SQL parameter '{undeclared[0]}' is not declared",
        )
