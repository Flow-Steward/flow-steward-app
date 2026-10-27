"""Tests for core.safe_eval — AST-whitelisted condition evaluator."""

import pytest

from core.infrastructure.security.safe_eval import DictAccessor, safe_eval

# ── Real workflow/supervisor conditions (must all evaluate correctly) ──────


class TestRealGateConditions:
    """Test with actual condition strings used in Flow Steward workflow YAML files."""

    def test_confidence_score_comparison(self):
        assert safe_eval("confidence_score >= 0.8", {"confidence_score": 0.9}) is True
        assert safe_eval("confidence_score >= 0.8", {"confidence_score": 0.5}) is False

    def test_review_result_equality(self):
        assert safe_eval("review_result == 'PASS'", {"review_result": "PASS"}) is True
        assert safe_eval("review_result == 'PASS'", {"review_result": "FAIL"}) is False

    def test_dotted_dict_access(self):
        ns = {
            "review_report": DictAccessor(
                {"critical_count": 0, "overall_recommendation": "APPROVE"}
            )
        }
        assert safe_eval("review_report.critical_count == 0", ns) is True
        assert safe_eval("review_report.overall_recommendation == 'APPROVE'", ns) is True

    def test_code_review_verdict(self):
        assert safe_eval("approval_verdict == 'approved'", {"approval_verdict": "approved"}) is True

    def test_user_approved(self):
        assert safe_eval("user_approved", {"user_approved": True}) is True
        assert safe_eval("user_approved", {"user_approved": False}) is False

    def test_len_with_or_default(self):
        assert safe_eval("len(blocking_issues or []) == 0", {"blocking_issues": []}) is True
        assert safe_eval("len(blocking_issues or []) == 0", {"blocking_issues": None}) is True
        assert (
            safe_eval("len(blocking_issues or []) == 0", {"blocking_issues": ["issue1"]}) is False
        )

    def test_boolean_and_or(self):
        ns = {"review_verdict": "pass", "confidence_score": 0.9}
        assert safe_eval("review_verdict == 'pass' and confidence_score >= 0.8", ns) is True
        assert safe_eval("review_verdict == 'fail' or confidence_score >= 0.8", ns) is True
        assert safe_eval("review_verdict == 'fail' and confidence_score < 0.5", ns) is False

    def test_iteration_count(self):
        assert safe_eval("iteration_count < 3", {"iteration_count": 2}) is True
        assert safe_eval("iteration_count < 3", {"iteration_count": 5}) is False


# ── Rejection tests (must raise ValueError for unsafe constructs) ──────


class TestSafetyRejections:
    """Verify that unsafe constructs are rejected."""

    def test_reject_dunder_access(self):
        with pytest.raises(ValueError, match="dunder"):
            safe_eval("().__class__", {"x": 1})

    def test_reject_dunder_attribute(self):
        _raises_input_69_1 = {"x": object()}
        with pytest.raises(ValueError, match="dunder"):
            safe_eval("x.__class__.__bases__", _raises_input_69_1)

    def test_reject_plain_object_attribute_access(self):
        class PlainObject:
            public_value = "secret"

        _raises_input_76_1 = {"x": PlainObject()}
        with pytest.raises(ValueError, match="DictAccessor"):
            safe_eval("x.public_value", _raises_input_76_1)

    def test_reject_arbitrary_function_call(self):
        with pytest.raises(ValueError, match="function call not allowed"):
            safe_eval("exec('import os')", {"exec": exec})

    def test_reject_import_via_eval(self):
        with pytest.raises(ValueError, match="function call not allowed"):
            safe_eval("__import__('os')", {"__import__": __import__})

    def test_reject_getattr_call(self):
        with pytest.raises(ValueError, match="function call not allowed"):
            safe_eval("getattr(x, 'y')", {"x": {}, "getattr": getattr})

    def test_reject_lambda(self):
        with pytest.raises((ValueError, SyntaxError)):
            safe_eval("lambda: 1", {})

    def test_reject_comprehension(self):
        with pytest.raises(ValueError, match="disallowed AST node"):
            safe_eval("[x for x in range(10)]", {"range": range})

    def test_reject_subclass_attack_via_dunder(self):
        """Dunder attribute access must be blocked."""
        with pytest.raises(ValueError, match="dunder"):
            safe_eval("x.__class__", {"x": ()})

    def test_reject_subclass_chain(self):
        """Multi-step introspection chain must fail."""
        with pytest.raises(ValueError, match="dunder"):
            safe_eval("x.__class__.__bases__", {"x": ()})


# ── Allowed builtins ──────


class TestAllowedBuiltins:
    """Verify whitelisted builtins work."""

    def test_len(self):
        assert safe_eval("len(items)", {"items": [1, 2, 3]}) == 3

    def test_bool(self):
        assert safe_eval("bool(x)", {"x": 1}) is True
        assert safe_eval("bool(x)", {"x": 0}) is False

    def test_str(self):
        assert safe_eval("str(x)", {"x": 42}) == "42"

    def test_int(self):
        assert safe_eval("int(x)", {"x": "42"}) == 42

    def test_isinstance(self):
        assert safe_eval("isinstance(x, int)", {"x": 42}) is True


# ── Edge cases ──────


class TestEdgeCases:
    def test_in_operator(self):
        assert safe_eval("'a' in items", {"items": ["a", "b"]}) is True
        assert safe_eval("'c' in items", {"items": ["a", "b"]}) is False

    def test_not_in_operator(self):
        assert safe_eval("'c' not in items", {"items": ["a", "b"]}) is True

    def test_is_none(self):
        assert safe_eval("x is None", {"x": None}) is True
        assert safe_eval("x is not None", {"x": 42}) is True

    def test_ternary(self):
        assert safe_eval("'yes' if x else 'no'", {"x": True}) == "yes"
        assert safe_eval("'yes' if x else 'no'", {"x": False}) == "no"

    def test_subscript(self):
        assert safe_eval("items[0]", {"items": [10, 20, 30]}) == 10
        assert safe_eval("d['key']", {"d": {"key": "value"}}) == "value"

    def test_tuple_literal_in_comparison(self):
        assert safe_eval("x in ('a', 'b', 'c')", {"x": "b"}) is True

    def test_undefined_name_raises(self):
        with pytest.raises(ValueError, match="undefined name"):
            safe_eval("nonexistent_var", {})

    def test_syntax_error(self):
        with pytest.raises(SyntaxError):
            safe_eval("if True:", {})


# ── DictAccessor ──────


class TestDictAccessor:
    def test_nested_access(self):
        d = DictAccessor({"a": {"b": {"c": 42}}})
        assert d.a.b.c == 42

    def test_equality(self):
        d = DictAccessor({"x": 1})
        assert d == {"x": 1}

    def test_len(self):
        d = DictAccessor({"a": 1, "b": 2})
        assert len(d) == 2

    def test_contains(self):
        d = DictAccessor({"key": "val"})
        assert "key" in d

    def test_getitem(self):
        d = DictAccessor({"key": "val"})
        assert d["key"] == "val"

    def test_dunder_attr_blocked_via_getattr(self):
        """DictAccessor.__getattr__ blocks dunder names."""
        d = DictAccessor({"__secret": "bad"})
        with pytest.raises(AttributeError):
            getattr(d, "__secret")

    def test_dunder_blocked_in_safe_eval(self):
        """safe_eval blocks dunder attribute access at the AST level."""
        _raises_input_200_1 = {"d": DictAccessor({})}
        with pytest.raises(ValueError, match="dunder"):
            safe_eval("d.__class__", _raises_input_200_1)
