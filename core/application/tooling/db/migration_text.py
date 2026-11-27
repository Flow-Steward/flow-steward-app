"""Pure text helpers for post-1.0 SQL migration files.

Nothing here touches the filesystem or a database. This module owns:

- migration filenames (``YYYYMMDDHHMMSS_slug.sql``) and the new-file template
- the ``-- transaction: true|false`` marker
- a PostgreSQL-compatible scanner that separates comments, literals and quoted
  identifiers from code, used to detect empty files and explicit transaction
  control, and by the platform-module migration guard

:mod:`core.application.tooling.db.migration_runner` re-exports the public names.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

# YYYYMMDDHHMMSS_slug.sql where slug is lowercase alphanumeric words joined by
# single underscores.
_FILENAME_RE = re.compile(r"^(?P<timestamp>\d{14})_(?P<slug>[a-z0-9]+(?:_[a-z0-9]+)*)\.sql$")

# A standalone transaction-control marker line: "-- transaction: true|false".
# Documentation lines that embed the marker inside another comment (for example
# "--   -- transaction: false  use for ...") deliberately do not match because of
# the extra prefix/trailing text.
_TXN_MARKER_RE = re.compile(
    r"^[ \t]*--[ \t]*transaction:[ \t]*(true|false)[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)

# Lexical classes of PostgreSQL's scanner (scan.l). Bytes >= 0x80 count as
# identifier characters there, so every non-ASCII character does here.
_NON_ASCII = "\u0080-\U0010ffff"
_IDENT_START_RE = re.compile(rf"[A-Za-z_{_NON_ASCII}]")
_IDENT_CONT_RE = re.compile(rf"[A-Za-z0-9_${_NON_ASCII}]*")
# Dollar-quote opener: $$ or $tag$. A tag never contains ``$``.
_DOLLAR_TAG_RE = re.compile(rf"\$(?:[A-Za-z_{_NON_ASCII}][A-Za-z0-9_{_NON_ASCII}]*)?\$")
# PostgreSQL ends a ``--`` comment at either newline character.
_LINE_END = "\r\n"
# A plain identifier, the only quoted identifier the code view unquotes.
_PLAIN_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
# What may follow a closed escape string and still continue it: PostgreSQL glues
# 'a'<newline>'b' into one literal and keeps the first part's escape mode.
_STRING_CONTINUATION_RE = re.compile(r"(?:\s|--[^\r\n]*)*'")

# A top-level statement that opens/closes/marks a transaction. Migration files
# must not contain explicit transaction control — the runner owns transactions
# so the body and the platform_schema_migrations insert stay atomic. This is
# only matched against top-level statement starts (dollar-quoted PL/pgSQL bodies
# and CASE...END remain one statement and are not flagged).
_TXN_CONTROL_START_RE = re.compile(
    r"^(begin|start\s+transaction|commit|end(\s+transaction)?|rollback|abort"
    r"|savepoint|release(\s+savepoint)?|prepare\s+transaction)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Filename generation and validation
# ---------------------------------------------------------------------------


def slugify_migration_name(name: str) -> str:
    """Normalize a free-text migration name into a filename slug."""
    slug = re.sub(r"[^a-z0-9]+", "_", str(name or "").strip().lower()).strip("_")
    if not slug:
        raise ValueError("Migration name must contain at least one alphanumeric character")
    return slug


def migration_timestamp(now: datetime | None = None) -> str:
    moment = now or datetime.now(UTC)
    return moment.strftime("%Y%m%d%H%M%S")


def generate_migration_filename(name: str, *, now: datetime | None = None) -> str:
    """Build a ``YYYYMMDDHHMMSS_slug.sql`` filename for ``name``."""
    return f"{migration_timestamp(now)}_{slugify_migration_name(name)}.sql"


def is_valid_migration_filename(filename: str) -> bool:
    return bool(_FILENAME_RE.match(str(filename or "")))


def render_migration_template(name: str) -> str:
    """Render the SQL-only template body for a new migration file.

    The template is SQL-only, carries a clear header, defaults to
    ``-- transaction: true`` and documents ``-- transaction: false``. It never
    includes ``BEGIN``/``COMMIT`` — the runner owns transaction control.
    """
    slug = slugify_migration_name(name)
    return (
        f"-- Migration: {slug}\n"
        "-- Flow Steward post-1.0 SQL migration.\n"
        "--\n"
        "-- Write SQL statements only. Do NOT add explicit transaction-control\n"
        "-- statements; the runner manages transactions and records this migration\n"
        "-- in public.platform_schema_migrations after it succeeds.\n"
        "--\n"
        "-- Transaction control:\n"
        "--   -- transaction: true   (default) the runner wraps this file in a\n"
        "--                          single transaction together with the tracking\n"
        "--                          insert; any failure rolls the whole migration back.\n"
        "--   -- transaction: false  use for statements that cannot run inside a\n"
        "--                          transaction block, e.g. CREATE INDEX CONCURRENTLY.\n"
        "--                          The runner records durable in-progress state first;\n"
        "--                          interrupted outcomes require manual reconciliation.\n"
        "-- transaction: true\n"
        "\n"
        "-- Replace this comment with your migration SQL.\n"
    )


# ---------------------------------------------------------------------------
# Content inspection: transaction marker, executable-SQL detection
# ---------------------------------------------------------------------------


def parse_transactional(sql: str) -> bool:
    """Return whether a migration should run inside a transaction.

    Defaults to ``True`` when no marker is present. ``-- transaction: false``
    opts out (for operations such as ``CREATE INDEX CONCURRENTLY``). If multiple
    markers appear, the last one wins.
    """
    matches = _TXN_MARKER_RE.findall(str(sql or ""))
    if not matches:
        return True
    return matches[-1].strip().lower() != "false"


@dataclass(frozen=True)
class _ScannedSql:
    """One pass of a PostgreSQL-compatible scanner over a migration file.

    ``text``  comments replaced by a space; literals and identifiers as written.
    ``code``  the same, with every string or dollar-quoted literal replaced by
              ``''`` and every quoted identifier replaced by its lowercased name
              (or ``_quoted_identifier_`` when the name is not a plain one), so
              ``;`` only ever separates statements and keywords are never hidden
              in a literal or behind quotes.
    ``unsupported`` constructs the scanner refuses rather than interprets.

    It follows PostgreSQL's lexer with ``standard_conforming_strings = on``: a
    plain ``'...'`` treats backslash literally, ``E'...'`` honours backslash
    escapes, block comments nest, a ``--`` comment ends at ``\\n`` or ``\\r``,
    identifiers may contain ``$`` (so ``a$$`` is a name, not a dollar quote), and
    a dollar quote opens only where a token starts.
    """

    text: str
    code: str
    unsupported: tuple[str, ...]


def _scan_quoted(sql: str, start: int, quote: str, *, backslash_escapes: bool) -> int:
    """Index just past the literal or identifier opened at ``start - 1``; -1 if open."""
    i = start
    n = len(sql)
    while i < n:
        ch = sql[i]
        if backslash_escapes and ch == "\\":
            i += 2
            continue
        if ch == quote:
            if i + 1 < n and sql[i + 1] == quote:
                i += 2
                continue
            return i + 1
        i += 1
    return -1


class _SqlScanner:
    """The state of one :func:`_scan_sql` pass; each ``_take_*`` returns the next index."""

    def __init__(self, sql: str) -> None:
        self.sql = sql
        self.n = len(sql)
        self.text: list[str] = []
        self.code: list[str] = []
        self.unsupported: list[str] = []

    def refuse(self, reason: str) -> None:
        if reason not in self.unsupported:
            self.unsupported.append(reason)

    def emit(self, text: str, code: str) -> None:
        self.text.append(text)
        self.code.append(code)

    def closing(self, start: int, quote: str, *, backslash_escapes: bool, kind: str) -> int:
        end = _scan_quoted(self.sql, start, quote, backslash_escapes=backslash_escapes)
        if end < 0:
            self.refuse(f"an unterminated {kind}")
            return self.n
        return end

    def take_line_comment(self, i: int) -> int:
        j = i + 2
        while j < self.n and self.sql[j] not in _LINE_END:
            j += 1
        self.emit(" ", " ")
        return j

    def take_block_comment(self, i: int) -> int:
        depth = 1
        j = i + 2
        while j < self.n and depth:
            if self.sql.startswith("/*", j):
                depth += 1
                j += 2
            elif self.sql.startswith("*/", j):
                depth -= 1
                j += 2
            else:
                j += 1
        if depth:
            self.refuse("an unterminated block comment")
        self.emit(" ", " ")
        return j

    def take_string(self, i: int, body_start: int, *, backslash_escapes: bool) -> int:
        end = self.closing(
            body_start, "'", backslash_escapes=backslash_escapes, kind="string literal"
        )
        if backslash_escapes and end < self.n and _STRING_CONTINUATION_RE.match(self.sql, end):
            self.refuse("an escape string continued in a second quoted part")
        self.emit(self.sql[i:end], " '' ")
        return end

    def take_identifier(self, i: int, body_start: int) -> int:
        end = self.closing(body_start, '"', backslash_escapes=False, kind="quoted identifier")
        name = self.sql[body_start : end - 1].replace('""', '"')
        if _PLAIN_IDENT_RE.fullmatch(name):
            self.emit(self.sql[i:end], f" {name.lower()} ")
        else:
            self.emit(self.sql[i:end], " _quoted_identifier_ ")
        return end

    def take_dollar(self, i: int) -> int:
        match = _DOLLAR_TAG_RE.match(self.sql, i)
        if not match:  # $1 and friends
            self.emit("$", "$")
            return i + 1
        tag = match.group(0)
        close = self.sql.find(tag, match.end())
        if close < 0:
            self.refuse("an unterminated dollar-quoted string")
            end = self.n
        else:
            end = close + len(tag)
        self.emit(self.sql[i:end], " '' ")
        return end

    def take_word(self, i: int) -> int:
        """An identifier or keyword, or the ``E'``/``U&`` prefix of a literal."""
        sql = self.sql
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < self.n else ""
        if ch in "eE" and nxt == "'":
            return self.take_string(i, i + 2, backslash_escapes=True)
        if ch in "uU" and nxt == "&" and i + 2 < self.n and sql[i + 2] in "'\"":
            self.refuse("a Unicode-escape string or identifier (U&'...' or U&\"...\")")
            if sql[i + 2] == "'":
                return self.take_string(i, i + 3, backslash_escapes=False)
            end = self.closing(i + 3, '"', backslash_escapes=False, kind="quoted identifier")
            self.emit(sql[i:end], " _quoted_identifier_ ")
            return end
        match = _IDENT_CONT_RE.match(sql, i + 1)
        end = match.end() if match else i + 1
        self.emit(sql[i:end], sql[i:end])
        return end

    def step(self, i: int) -> int:
        ch = self.sql[i]
        pair = self.sql[i : i + 2]
        if pair == "--":
            return self.take_line_comment(i)
        if pair == "/*":
            return self.take_block_comment(i)
        if ch == "'":
            return self.take_string(i, i + 1, backslash_escapes=False)
        if ch == '"':
            return self.take_identifier(i, i + 1)
        if ch == "$":
            return self.take_dollar(i)
        if _IDENT_START_RE.match(ch):
            return self.take_word(i)
        self.emit(ch, ch)
        return i + 1


def _scan_sql(sql: str) -> _ScannedSql:
    scanner = _SqlScanner(sql)
    i = 0
    while i < scanner.n:
        i = scanner.step(i)
    return _ScannedSql("".join(scanner.text), "".join(scanner.code), tuple(scanner.unsupported))


def _strip_sql_comments(sql: str) -> str:
    """Remove SQL line/block comments, preserving string and dollar-quoted text."""
    return _scan_sql(sql).text


def _code_statements(code: str) -> list[str]:
    """Top-level statements of a scanned ``code`` view, stripped, empty ones dropped."""
    return [part.strip() for part in code.split(";") if part.strip()]


def migration_has_executable_sql(sql: str) -> bool:
    """Return True if the migration contains SQL beyond comments/whitespace."""
    stripped = _strip_sql_comments(str(sql or ""))
    return bool(stripped.replace(";", "").strip())


def find_explicit_transaction_control(sql: str) -> list[str]:
    """Return offending top-level transaction-control statements, if any.

    The runner owns transactions; a migration that opens/commits its own
    transaction would break the atomicity of the body plus the tracking insert.
    Statements are split on the scanner's ``code`` view (:func:`_scan_sql`), where
    every literal is already blanked, so ``BEGIN``/``COMMIT`` inside a PL/pgSQL
    function body, a ``DO`` block or an ``E'...'`` string is not flagged, and one
    hidden behind a mis-read literal is.
    """
    offenders: list[str] = []
    for statement in _code_statements(_scan_sql(str(sql or "")).code):
        if _TXN_CONTROL_START_RE.match(statement):
            offenders.append(statement[:40])
    return offenders
