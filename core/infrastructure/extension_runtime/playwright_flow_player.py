"""
Playwright flow player — executes YAML-defined UI flows synchronously.

Uses playwright.sync_api so it integrates cleanly with LangGraph's
synchronous node architecture (no asyncio required).

Usage:
  player = FlowPlayer(
      base_url="http://localhost:3000",
      screenshot_dir="/tmp/flow-steward-screenshots/task-001",
      headless=True,
      timeout_ms=30000,
  )
  result = player.run_flow("/app/flows/registry/login-flow.yaml")
  if result.passed:
      print(f"Passed in {result.duration_ms:.0f}ms")
  else:
      print(f"Failed at step {result.failed_step_index}: {result.error}")

Supported YAML actions: navigate, click, fill, screenshot, wait_for,
  assert_visible, assert_text, assert_url, select_option, hover,
  press_key, wait_network_idle
"""

import logging
import os
import time
from dataclasses import dataclass, field

import yaml

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class StepResult:
    """Result of executing a single flow step."""

    action: str
    selector: str | None
    passed: bool
    error: str | None = None
    screenshot_path: str | None = None
    duration_ms: float = 0.0


@dataclass
class FlowResult:
    """Complete result of running a YAML flow."""

    flow_name: str
    flow_path: str
    passed: bool
    steps_run: int
    failed_step_index: int | None  # None if all steps passed
    failed_step_action: str | None  # action name of the failed step
    error: str | None  # full error message on failure
    screenshots: list[str] = field(default_factory=list)  # absolute paths
    step_results: list[StepResult] = field(default_factory=list)
    duration_ms: float = 0.0


# ---------------------------------------------------------------------------
# FlowPlayer
# ---------------------------------------------------------------------------


class FlowPlayer:
    """
    Executes Playwright YAML flows synchronously.

    Each call to run_flow() creates its own sync_playwright() context
    (fresh browser per flow) — safe to call multiple times on one instance.

    Thread safety: NOT thread-safe. Create one per invocation.
    """

    def __init__(
        self,
        base_url: str,
        screenshot_dir: str,
        headless: bool = True,
        timeout_ms: int = 30000,
    ):
        self.base_url = base_url.rstrip("/")
        self.screenshot_dir = screenshot_dir
        self.headless = headless
        self.default_timeout_ms = timeout_ms
        os.makedirs(screenshot_dir, exist_ok=True)

    def run_flow(self, flow_yaml_path: str) -> FlowResult:
        """
        Load and execute a YAML flow file.
        Manages Playwright browser lifecycle internally.
        Never raises — all errors are captured in FlowResult.
        """
        # --- Load YAML ---
        try:
            with open(flow_yaml_path) as f:
                flow_def = yaml.safe_load(f)
        except Exception as e:
            return FlowResult(
                flow_name=os.path.basename(flow_yaml_path),
                flow_path=flow_yaml_path,
                passed=False,
                steps_run=0,
                failed_step_index=None,
                failed_step_action=None,
                error=f"Failed to load flow YAML: {e}",
            )

        flow_name = flow_def.get("name", os.path.basename(flow_yaml_path))
        steps = flow_def.get("steps", [])
        # Flow-level base_url overrides the player's base_url
        base_url = flow_def.get("base_url", self.base_url) or self.base_url
        flow_timeout_ms = flow_def.get("timeout_ms", self.default_timeout_ms)

        logger.info(
            "FlowPlayer: running '%s' (%d steps) against %s",
            flow_name,
            len(steps),
            base_url,
        )

        step_results: list[StepResult] = []
        screenshots: list[str] = []
        start_time = time.monotonic()

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            return FlowResult(
                flow_name=flow_name,
                flow_path=flow_yaml_path,
                passed=False,
                steps_run=0,
                failed_step_index=None,
                failed_step_action=None,
                error=f"Playwright not installed: {e}. Run: playwright install chromium",
            )

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=self.headless)
                context = browser.new_context(
                    viewport={"width": 1280, "height": 720},
                    ignore_https_errors=True,
                )
                page = context.new_page()
                page.set_default_timeout(flow_timeout_ms)

                for i, step in enumerate(steps):
                    step_start = time.monotonic()
                    step_timeout = step.get("timeout_ms", flow_timeout_ms)

                    try:
                        screenshot_path = self._execute_step(
                            page=page,
                            step=step,
                            base_url=base_url,
                            step_index=i,
                            step_timeout_ms=step_timeout,
                        )
                        step_duration = (time.monotonic() - step_start) * 1000
                        sr = StepResult(
                            action=step.get("action", "unknown"),
                            selector=step.get("selector"),
                            passed=True,
                            screenshot_path=screenshot_path,
                            duration_ms=step_duration,
                        )
                        step_results.append(sr)
                        if screenshot_path:
                            screenshots.append(screenshot_path)

                    except Exception as e:
                        step_duration = (time.monotonic() - step_start) * 1000

                        # Auto-capture failure screenshot
                        fail_shot = self._capture_screenshot(
                            page,
                            f"FAILURE-step{i}-{step.get('action', 'unknown')}",
                        )
                        if fail_shot:
                            screenshots.append(fail_shot)

                        error_msg = (
                            f"Step {i} ({step.get('action')}"
                            + (f" {step.get('selector')!r}" if step.get("selector") else "")
                            + f"): {type(e).__name__}: {e}"
                        )
                        logger.warning("FlowPlayer: %s", error_msg)

                        step_results.append(
                            StepResult(
                                action=step.get("action", "unknown"),
                                selector=step.get("selector"),
                                passed=False,
                                error=error_msg,
                                screenshot_path=fail_shot,
                                duration_ms=step_duration,
                            )
                        )

                        total_ms = (time.monotonic() - start_time) * 1000
                        browser.close()
                        return FlowResult(
                            flow_name=flow_name,
                            flow_path=flow_yaml_path,
                            passed=False,
                            steps_run=i + 1,
                            failed_step_index=i,
                            failed_step_action=step.get("action"),
                            error=error_msg,
                            screenshots=screenshots,
                            step_results=step_results,
                            duration_ms=total_ms,
                        )

                browser.close()

        except Exception as e:
            # Browser-level failure (e.g. Chromium binary missing)
            total_ms = (time.monotonic() - start_time) * 1000
            return FlowResult(
                flow_name=flow_name,
                flow_path=flow_yaml_path,
                passed=False,
                steps_run=len(step_results),
                failed_step_index=None,
                failed_step_action=None,
                error=f"Browser-level failure: {type(e).__name__}: {e}",
                screenshots=screenshots,
                step_results=step_results,
                duration_ms=total_ms,
            )

        total_ms = (time.monotonic() - start_time) * 1000
        logger.info(
            "FlowPlayer: '%s' PASSED in %.0fms (%d steps)",
            flow_name,
            total_ms,
            len(steps),
        )
        return FlowResult(
            flow_name=flow_name,
            flow_path=flow_yaml_path,
            passed=True,
            steps_run=len(steps),
            failed_step_index=None,
            failed_step_action=None,
            error=None,
            screenshots=screenshots,
            step_results=step_results,
            duration_ms=total_ms,
        )

    def _execute_step(
        self,
        page,
        step: dict,
        base_url: str,
        step_index: int,
        step_timeout_ms: int,
    ) -> str | None:
        """
        Dispatch a single step to its action handler.
        Returns screenshot path for 'screenshot' action, else None.
        Raises on failure (caller catches and records the error).
        """
        action = step.get("action", "").lower().strip()

        if action == "navigate":
            url = step["url"]
            if not url.startswith("http"):
                url = base_url + "/" + url.lstrip("/")
            page.goto(url, wait_until="domcontentloaded", timeout=step_timeout_ms)
            return None

        if action == "click":
            page.locator(step["selector"]).click(timeout=step_timeout_ms)
            return None

        if action == "fill":
            loc = page.locator(step["selector"])
            loc.clear()
            loc.fill(str(step["value"]))
            return None

        if action == "screenshot":
            name = step.get("name", f"step-{step_index}")
            return self._capture_screenshot(page, name)

        if action == "wait_for":
            page.locator(step["selector"]).wait_for(state="visible", timeout=step_timeout_ms)
            return None

        if action == "assert_visible":
            loc = page.locator(step["selector"])
            if not loc.is_visible():
                msg = step.get("message", f"Element not visible: {step['selector']!r}")
                raise AssertionError(msg)
            return None

        if action == "assert_text":
            loc = page.locator(step["selector"])
            actual = loc.text_content(timeout=step_timeout_ms) or ""
            expected = str(step["expected"])
            if expected not in actual:
                msg = step.get(
                    "message",
                    f"Text assertion failed: expected {expected!r} in {actual!r}",
                )
                raise AssertionError(msg)
            return None

        if action == "assert_url":
            current = page.url
            pattern = str(step["pattern"])
            if pattern not in current:
                msg = step.get(
                    "message",
                    f"URL assertion failed: expected {pattern!r} in {current!r}",
                )
                raise AssertionError(msg)
            return None

        if action == "select_option":
            page.locator(step["selector"]).select_option(str(step["value"]))
            return None

        if action == "hover":
            page.locator(step["selector"]).hover(timeout=step_timeout_ms)
            return None

        if action == "press_key":
            page.keyboard.press(str(step["key"]))
            return None

        if action == "wait_network_idle":
            page.wait_for_load_state("networkidle", timeout=step_timeout_ms)
            return None

        raise ValueError(f"Unknown action: {action!r}")

    def _capture_screenshot(self, page, name: str) -> str | None:
        """Save a screenshot to screenshot_dir/{safe_name}.png. Returns path or None."""
        try:
            # Sanitize to prevent path traversal
            safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
            path = os.path.join(self.screenshot_dir, f"{safe}.png")
            page.screenshot(path=path, full_page=False)
            logger.debug("FlowPlayer: screenshot → %s", path)
            return path
        except Exception as e:
            logger.warning("FlowPlayer: screenshot failed: %s", e)
            return None
