"""One parse per source text per process, shared by the source-scanning guards.

Architecture and guardrail tests each walk the whole ``core/`` tree with
``ast.parse``. Parsing is the same work every time, so this module parses each
distinct source text once per process and hands every caller the same result.

The cache is keyed by the source text itself, never by path or modification
time. Every call reads the file again (reading is cheap next to parsing), so:

- a file that changes on disk is parsed again, and the cache cannot go stale;
- a test that writes a temporary module scans that temporary module, not a
  cached tree of some other file;
- the same module reached through two roots (the checkout and the workspace
  copy ``run-core-only-tests.sh`` makes) shares one tree.

Shared trees are read-only. A caller must not change a node, add attributes to
one, or run a ``NodeTransformer`` over it; parse a private copy with
``ast.parse`` when a test needs to modify a tree.

The cache is per process (an xdist worker builds its own) and lives for the
whole session. Holding every ``core/`` tree costs a few hundred megabytes, the
price of not parsing that tree again in each guard.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from itertools import compress, repeat
from pathlib import Path
from typing import Any, TypeVar

__all__ = ["ParsedSource", "parse_file", "parse_source", "whole_words_in"]


_Node = TypeVar("_Node", bound=ast.AST)


class ParsedSource:
    """A parsed module plus node views computed at most once."""

    __slots__ = ("_by_type", "_nodes", "_subtrees", "text", "tree")

    def __init__(self, text: str, tree: ast.Module) -> None:
        self.text = text
        self.tree = tree
        self._nodes: tuple[ast.AST, ...] | None = None
        self._by_type: dict[tuple[type[ast.AST], ...], tuple[Any, ...]] = {}
        self._subtrees: dict[ast.AST, tuple[ast.AST, ...]] = {}

    @property
    def nodes(self) -> tuple[ast.AST, ...]:
        """Every node of the module, in ``ast.walk`` order."""
        if self._nodes is None:
            self._nodes = tuple(ast.walk(self.tree))
        return self._nodes

    def nodes_of(self, *types: type[_Node]) -> tuple[_Node, ...]:
        """The nodes that are instances of ``types``, in ``ast.walk`` order."""
        found = self._by_type.get(types)
        if found is None:
            # compress/map keep the filter in C: under a line tracer (coverage)
            # a Python-level loop over every node is the expensive part.
            nodes = self.nodes
            found = tuple(compress(nodes, map(isinstance, nodes, repeat(types))))
            self._by_type[types] = found
        return found

    def subtree(self, node: ast.AST) -> tuple[ast.AST, ...]:
        """``tuple(ast.walk(node))`` for a node of this module, computed once."""
        found = self._subtrees.get(node)
        if found is None:
            found = tuple(ast.walk(node))
            self._subtrees[node] = found
        return found


_PARSED: dict[str, ParsedSource] = {}


def parse_source(text: str, *, filename: str = "<unknown>") -> ParsedSource:
    """Return the shared parse of ``text``; ``filename`` only labels syntax errors."""
    parsed = _PARSED.get(text)
    if parsed is None:
        parsed = ParsedSource(text, ast.parse(text, filename=filename))
        _PARSED[text] = parsed
    return parsed


def parse_file(
    path: Path, *, encoding: str | None = "utf-8", errors: str = "strict"
) -> ParsedSource:
    """Read ``path`` now and return the shared parse of its current text."""
    return parse_source(path.read_text(encoding=encoding, errors=errors), filename=str(path))


_WORD = re.compile(r"\w+")


def whole_words_in(text: str, candidates: Iterable[str]) -> set[str]:
    """The candidates that occur in ``text`` as whole words.

    A candidate counts when ``re.search(rf"\\b{re.escape(candidate)}\\b", text)``
    finds it. A candidate made only of word characters matches that pattern
    exactly when it is one of the maximal word runs of ``text``, so one
    tokenisation answers all of them instead of one regex scan of the whole
    text per candidate. Any other candidate is checked with the pattern itself.
    """
    words = set(_WORD.findall(text))
    return {
        candidate
        for candidate in candidates
        if (
            candidate in words
            if _WORD.fullmatch(candidate)
            else re.search(rf"\b{re.escape(candidate)}\b", text)
        )
    }
