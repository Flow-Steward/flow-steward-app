"""Unit tests for security/scanners/base.py.

The Scanner Protocol defines the interface that all scanner engines must satisfy.
Tests verify:
- A conforming concrete class satisfies the Protocol contract (structural typing).
- Protocol has the expected method names and signatures.
- Unimplemented methods cause AttributeError at call time (not at class definition).
"""

from __future__ import annotations

import inspect

import pytest

from core.infrastructure.security.models import ScanFinding
from core.infrastructure.security.policy import ScannerConfig
from core.infrastructure.security.scanners.base import Scanner

# ---------------------------------------------------------------------------
# Protocol shape checks
# ---------------------------------------------------------------------------


class TestScannerProtocolShape:
    def test_scanner_protocol_has_scan_method(self):
        assert hasattr(Scanner, "scan")

    def test_scanner_protocol_has_redact_method(self):
        assert hasattr(Scanner, "redact")

    def test_scan_method_accepts_text_surface_config(self):
        sig = inspect.signature(Scanner.scan)
        params = list(sig.parameters.keys())
        assert "text" in params
        assert "surface" in params
        assert "config" in params

    def test_redact_method_accepts_text_and_findings(self):
        sig = inspect.signature(Scanner.redact)
        params = list(sig.parameters.keys())
        assert "text" in params
        assert "findings" in params


# ---------------------------------------------------------------------------
# Conforming implementation
# ---------------------------------------------------------------------------


class _ConcreteScanner:
    """Minimal Scanner implementation for protocol compliance testing."""

    def scan(
        self,
        text: str,
        surface: str,
        config: ScannerConfig,
        **kwargs,
    ) -> list[ScanFinding]:
        return []

    def redact(self, text: str, findings: list[ScanFinding]) -> str:
        return text


class TestConformingImplementation:
    def test_conforming_class_instantiates(self):
        scanner = _ConcreteScanner()
        assert scanner is not None

    def test_scan_returns_empty_list_by_default(self):
        scanner = _ConcreteScanner()
        config = ScannerConfig()
        result = scanner.scan("clean text", "inbound", config)
        assert result == []

    def test_redact_returns_original_text(self):
        scanner = _ConcreteScanner()
        result = scanner.redact("original", [])
        assert result == "original"

    def test_scan_signature_matches_protocol(self):
        concrete_sig = inspect.signature(_ConcreteScanner.scan)
        protocol_sig = inspect.signature(Scanner.scan)
        concrete_params = set(concrete_sig.parameters.keys())
        protocol_params = set(protocol_sig.parameters.keys())
        # All non-kwargs protocol params must be in concrete
        required = {p for p in protocol_params if p != "kwargs"}
        assert required.issubset(concrete_params | {"kwargs"})


# ---------------------------------------------------------------------------
# Non-conforming implementation
# ---------------------------------------------------------------------------


class _NonConformingScanner:
    """Does not implement Scanner protocol."""

    def only_scan(self, text: str) -> list:
        return []


class TestNonConformingUsage:
    def test_missing_redact_raises_attribute_error(self):
        scanner = _NonConformingScanner()
        with pytest.raises(AttributeError):
            scanner.redact("text", [])  # type: ignore[attr-defined]

    def test_wrong_method_not_usable_as_scan(self):
        scanner = _NonConformingScanner()
        _raises_input_112_1 = ScannerConfig()
        with pytest.raises(AttributeError):
            scanner.scan("text", "inbound", _raises_input_112_1)  # type: ignore[attr-defined]
