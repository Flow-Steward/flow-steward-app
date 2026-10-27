"""Regression tests for extension-UI XSS / action-allowlist guards.

Covers two security findings in the vanilla-JS platform renderer:

* FS-DEEP-CAND-035 — grid link cells must reject javascript:/data:/vbscript:
  and other non-allowlisted href schemes (only http/https/mailto/tel + relative).
* FS-DEEP-CAND-066 — extension query-response detail payloads must have their
  executable (action-wiring) components dropped BEFORE instantiation, since they
  bypass the compiled-manifest static action allowlist.

Two layers of assertion:
  1. Source-text assertions that the guards are present and wired into the sinks
     (run in every pytest/CI environment, no node required).
  2. A behavioural check that loads the real asset modules in a node subprocess
     and exercises the guards. Skipped gracefully when node is unavailable.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GRID_JS = ROOT / "assets" / "platform-kit" / "components" / "platform-grid.js"
ACTION_BAR_JS = ROOT / "assets" / "platform-kit" / "components" / "platform-action-bar.js"
RENDERER_JS = ROOT / "assets" / "platform-renderer.js"
HARNESS = Path(__file__).resolve().parent / "platform_xss_guard_harness.mjs"


def test_grid_link_cell_uses_safe_href_guard() -> None:
    """CAND-035: link cell href is passed through the scheme allowlist guard."""
    src = GRID_JS.read_text(encoding="utf-8")
    # Allowlist constant + guard helpers exist.
    assert "SAFE_LINK_SCHEMES" in src
    assert "function isUnsafeLinkHref" in src
    assert "function safeLinkHref" in src
    # The link-cell sink neutralizes the href before it reaches the anchor.
    assert "const safeHref = safeLinkHref(href);" in src
    assert 'href="${this.esc(safeHref)}"' in src
    # Raw (unsanitized) href must no longer be emitted directly.
    assert 'href="${this.esc(href)}"' not in src


def test_detail_payload_executable_allowlist_runs_before_instantiation() -> None:
    """CAND-066: executable components are filtered before _renderComponent."""
    src = RENDERER_JS.read_text(encoding="utf-8")
    assert "EXECUTABLE_COMPONENT_TYPES" in src
    assert "function filterDisplaySafeComponents" in src
    # The detail-drawer sink filters BEFORE rendering/wiring components.
    drawer = src[src.index("_renderDetailIntoDrawer") :]
    filter_pos = drawer.index("filterDisplaySafeComponents(detail.components)")
    render_pos = drawer.index("_renderComponent(comp)")
    assert filter_pos < render_pos, "allowlist filter must run before component instantiation"
    # Executable types that wire onAction are the ones being dropped.
    for executable in ("action_bar", "filter_bar", "form", "confirm_action", "drawer_detail"):
        assert executable in src


def test_action_bar_variant_size_and_align_are_allowlisted() -> None:
    """Action-bar class tokens must be allowlisted before entering class attributes."""
    src = ACTION_BAR_JS.read_text(encoding="utf-8")
    assert "ACTION_BAR_ALIGNMENTS" in src
    assert "ACTION_BAR_VARIANTS" in src
    assert "ACTION_BAR_SIZES" in src
    assert "safeActionBarClassToken(a.variant, ACTION_BAR_VARIANTS)" in src
    assert "safeActionBarClassToken(a.size, ACTION_BAR_SIZES)" in src
    assert "safeActionBarClassToken(c.align, ACTION_BAR_ALIGNMENTS)" in src
    assert 'class="${this.esc(cls)}"' in src
    assert "const variant = a.variant || '';" not in src
    assert "const size = a.size || '';" not in src


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_guards_behave_correctly_in_node_runtime() -> None:
    """Execute the real asset modules in node and assert guard behaviour."""
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [shutil.which("node"), str(HARNESS)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(ROOT),
    )
    assert result.returncode == 0, (
        f"node harness failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert result.stdout.strip().endswith("OK"), result.stdout
