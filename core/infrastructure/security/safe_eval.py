"""
AST-whitelisted condition evaluator — replaces eval() for gate conditions.

Supports only:
- Literals: numbers, strings, booleans, None
- Names (looked up in a provided namespace)
- Boolean operators: and, or, not
- Comparisons: ==, !=, <, <=, >, >=, in, not in, is, is not
- Unary operators: not, +, -
- Subscript / index access: d["key"], d[0]
- Attribute access: obj.attr (only through namespace DictAccessor)
- Calls to whitelisted builtins: len(), bool(), str(), int(), float(), isinstance()

Rejects:
- Arbitrary function calls
- Comprehensions, generators
- Lambda, class, import, exec, eval
- Object introspection (__class__, __bases__, __subclasses__, etc.)
"""

from __future__ import annotations

import ast
import logging
import operator
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

_ALLOWED_BUILTINS = {"len", "bool", "str", "int", "float", "isinstance"}
# Type names allowed in isinstance() and similar
_SAFE_NAMES = {
    "int": int,
    "float": float,
    "str": str,
    "bool": bool,
    "list": list,
    "dict": dict,
    "tuple": tuple,
    "None": None,
    "True": True,
    "False": False,
}

_CMP_OPS: dict[type[ast.cmpop], Callable[[Any, Any], Any]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
}

_BOOL_OPS = {
    ast.And: all,
    ast.Or: any,
}

_UNARY_OPS = {
    ast.Not: operator.not_,
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

_BIN_OPS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}


class _UnsafeNode(Exception):
    """Raised when the AST contains a disallowed node type."""


class DictAccessor:
    """Wraps a dict to support dotted attribute access in conditions.

    e.g. ``review_report.critical_count`` resolves to
    ``review_report['critical_count']``.
    """

    def __init__(self, d: dict):
        self._d = d

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        val = self._d.get(name)
        if isinstance(val, dict):
            return DictAccessor(val)
        return val

    def __eq__(self, other: object) -> bool:
        if isinstance(other, DictAccessor):
            return self._d == other._d
        return self._d == other

    def __ne__(self, other: object) -> bool:
        return not self.__eq__(other)

    def _unsupported_ordering(self, other: object) -> bool:
        del other
        return False

    __ge__ = _unsupported_ordering
    __gt__ = _unsupported_ordering
    __le__ = _unsupported_ordering
    __lt__ = _unsupported_ordering

    def __len__(self) -> int:
        return len(self._d)

    def __contains__(self, item: object) -> bool:
        return item in self._d

    def __getitem__(self, key: Any) -> Any:
        return self._d[key]

    def __repr__(self) -> str:
        return f"DictAccessor({self._d!r})"


def _eval_node(node: ast.AST, ns: dict[str, Any]) -> Any:
    """Recursively evaluate a single AST node against a namespace."""

    if isinstance(node, ast.Expression):
        return _eval_node(node.body, ns)

    # Literals
    if isinstance(node, ast.Constant):
        return node.value

    # Names — lookup in namespace, then safe builtins
    if isinstance(node, ast.Name):
        if node.id.startswith("__"):
            raise _UnsafeNode(f"dunder access forbidden: {node.id}")
        if node.id in ns:
            return ns[node.id]
        if node.id in _SAFE_NAMES:
            return _SAFE_NAMES[node.id]
        raise _UnsafeNode(f"undefined name: {node.id}")

    # Attribute access — only through DictAccessor, no dunders
    if isinstance(node, ast.Attribute):
        if node.attr.startswith("__"):
            raise _UnsafeNode(f"dunder attribute access forbidden: {node.attr}")
        val = _eval_node(node.value, ns)
        if not isinstance(val, DictAccessor):
            raise _UnsafeNode("attribute access is only allowed on DictAccessor values")
        try:
            return getattr(val, node.attr)
        except AttributeError as exc:
            raise _UnsafeNode(f"unknown attribute: {node.attr}") from exc

    # Subscript: d["key"] or d[0]
    if isinstance(node, ast.Subscript):
        val = _eval_node(node.value, ns)
        idx = _eval_node(node.slice, ns)
        return val[idx]

    # Boolean ops: and, or
    if isinstance(node, ast.BoolOp):
        bool_op_fn = _BOOL_OPS.get(type(node.op))
        if not bool_op_fn:
            raise _UnsafeNode(f"unsupported bool op: {type(node.op).__name__}")
        # Short-circuit: evaluate lazily
        if isinstance(node.op, ast.And):
            result: Any = True
            for v in node.values:
                result = _eval_node(v, ns)
                if not result:
                    return result
            return result
        # Or
        result = False
        for v in node.values:
            result = _eval_node(v, ns)
            if result:
                return result
        return result

    # Unary ops: not, +, -
    if isinstance(node, ast.UnaryOp):
        unary_op_fn = _UNARY_OPS.get(type(node.op))
        if not unary_op_fn:
            raise _UnsafeNode(f"unsupported unary op: {type(node.op).__name__}")
        return unary_op_fn(_eval_node(node.operand, ns))

    # Binary ops: +, -, *, //, %
    if isinstance(node, ast.BinOp):
        bin_op_fn = _BIN_OPS.get(type(node.op))
        if not bin_op_fn:
            raise _UnsafeNode(f"unsupported binary op: {type(node.op).__name__}")
        return bin_op_fn(_eval_node(node.left, ns), _eval_node(node.right, ns))

    # Comparisons: ==, !=, <, <=, >, >=, in, not in, is, is not
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, ns)
        for op, comparator_node in zip(node.ops, node.comparators, strict=False):
            right = _eval_node(comparator_node, ns)
            if isinstance(op, ast.In):
                if left not in right:
                    return False
            elif isinstance(op, ast.NotIn):
                if left in right:
                    return False
            else:
                cmp_op_fn = _CMP_OPS.get(type(op))
                if not cmp_op_fn:
                    raise _UnsafeNode(f"unsupported comparison: {type(op).__name__}")
                if not cmp_op_fn(left, right):
                    return False
            left = right
        return True

    # Whitelisted function calls: len(), bool(), str(), int(), float(), isinstance()
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id in _ALLOWED_BUILTINS:
            fn = (
                __builtins__[node.func.id]
                if isinstance(__builtins__, dict)
                else getattr(__builtins__, node.func.id)
            )
            args = [_eval_node(a, ns) for a in node.args]
            return fn(*args)
        raise _UnsafeNode(f"function call not allowed: {ast.dump(node.func)}")

    # IfExp (ternary): x if cond else y
    if isinstance(node, ast.IfExp):
        return (
            _eval_node(node.body, ns) if _eval_node(node.test, ns) else _eval_node(node.orelse, ns)
        )

    # Tuple/List literals used in "x in (a, b, c)"
    if isinstance(node, ast.Tuple):
        return tuple(_eval_node(e, ns) for e in node.elts)
    if isinstance(node, ast.List):
        return [_eval_node(e, ns) for e in node.elts]

    raise _UnsafeNode(f"disallowed AST node: {type(node).__name__}")


def safe_eval(condition: str, namespace: dict[str, Any]) -> Any:
    """Evaluate a condition string safely using AST whitelisting.

    Args:
        condition: A simple Python expression string.
        namespace: Variable bindings available to the expression.

    Returns:
        The result of evaluating the expression.

    Raises:
        ValueError: If the expression contains disallowed constructs.
        SyntaxError: If the expression is not valid Python.
    """
    tree = ast.parse(condition, mode="eval")
    try:
        return _eval_node(tree, namespace)
    except _UnsafeNode as e:
        raise ValueError(str(e)) from e
