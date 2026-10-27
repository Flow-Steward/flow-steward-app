"""Render one user-written Jinja template in a disposable, bounded child process.

The workflow Template step lets a user write standard Jinja. Jinja's own
sandbox (``ImmutableSandboxedEnvironment``) blocks unsafe attribute access and
mutation, but it cannot stop a template that runs for minutes or builds a huge
string. So every render happens in a fresh Python process that receives the
template and its JSON context on stdin, writes one JSON answer on stdout, and is
killed when it runs past ``RENDER_TIMEOUT_SECONDS``. The child also bounds its
own output while rendering and, on Linux, its address space.

Only JSON values reach the template: no runtime objects, no application
functions, no loader. ``include``, ``import`` and ``extends`` are refused when
the template is compiled.

Syntax is checked in-process by compiling (``check_template_syntax``); compiling
evaluates nothing.
"""

from __future__ import annotations

import json
import subprocess  # nosec B404 - runs this interpreter on a fixed bootstrap, no shell.
import sys
import traceback
from pathlib import Path
from typing import Any

#: Longest template source, in characters.
MAX_TEMPLATE_CHARS = 100_000
#: Largest JSON context (all declared inputs together), in UTF-8 bytes.
MAX_CONTEXT_BYTES = 2_000_000
#: Longest rendered result, in characters.
MAX_OUTPUT_CHARS = 1_000_000
#: Wall-clock limit of one render, including the child's start-up.
RENDER_TIMEOUT_SECONDS = 5.0
#: Address-space limit of the child process (Linux only).
CHILD_MEMORY_BYTES = 1024 * 1024 * 1024

TEMPLATE_FORMATS = ("text", "markdown", "html")
_TEMPLATE_NAME = "<template>"
#: The directory holding the ``core`` package this module was loaded from.
_PACKAGE_ROOT = str(Path(__file__).resolve().parents[3])
_MESSAGE_CHARS = 300

# The child imports this module by its package path, so it runs the same code
# the parent was loaded from rather than whatever the working directory holds.
_CHILD_BOOTSTRAP = (
    "import json, sys\n"
    "request = json.loads(sys.stdin.buffer.read().decode('utf-8'))\n"
    "sys.path[:0] = request.pop('sys_path')\n"
    "from core.infrastructure.security.jinja_sandbox import _child_main\n"
    "_child_main(request)\n"
)


class TemplateRenderError(Exception):
    """Why a template could not be compiled or rendered.

    ``reason`` is a stable code; ``message`` is safe to show (it names
    variables and fields, never their values); ``line`` is the template line
    when Jinja reports one.
    """

    def __init__(self, reason: str, message: str, line: int | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.line = line

    def __str__(self) -> str:
        return self.message

    def dump(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.reason, "message": self.message}
        if self.line:
            payload["line"] = self.line
        return payload


def _short(text: Any) -> str:
    value = str(text or "").strip()
    return value if len(value) <= _MESSAGE_CHARS else f"{value[:_MESSAGE_CHARS]}…"


def _environment(output_format: str) -> Any:
    """The one Jinja environment every template runs in."""
    from jinja2 import StrictUndefined
    from jinja2.sandbox import ImmutableSandboxedEnvironment, SecurityError

    class _BoundedEnvironment(ImmutableSandboxedEnvironment):
        # Repetition and powers build values far larger than the template text
        # before any output check could see them.
        intercepted_binops = frozenset({"*", "**"})

        def call_binop(self, context: Any, operator: str, left: Any, right: Any) -> Any:
            if operator == "*":
                for sized, count in ((left, right), (right, left)):
                    repeats = isinstance(sized, (str, list, tuple)) and isinstance(count, int)
                    if repeats and len(sized) * max(count, 0) > MAX_OUTPUT_CHARS:
                        raise SecurityError("the repeated value would be too large")
            if operator == "**" and isinstance(right, (int, float)) and abs(right) > 1_000:
                raise SecurityError("the exponent is too large")
            return super().call_binop(context, operator, left, right)

    return _BoundedEnvironment(
        autoescape=output_format == "html",
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=False,
        lstrip_blocks=False,
    )


def _refuse_loaded_templates(environment: Any, source: str, where: str = "a Template step") -> None:
    from jinja2 import nodes

    tree = environment.parse(source, name=_TEMPLATE_NAME)
    for node in tree.find_all((nodes.Include, nodes.Import, nodes.FromImport, nodes.Extends)):
        raise TemplateRenderError(
            "template_unsupported",
            f"include, import and extends are not available in {where}",
            getattr(node, "lineno", None),
        )


def _compile(source: str, output_format: str) -> Any:
    from jinja2 import TemplateSyntaxError

    if len(source) > MAX_TEMPLATE_CHARS:
        raise TemplateRenderError(
            "template_too_large", f"The template is longer than {MAX_TEMPLATE_CHARS} characters"
        )
    environment = _environment(output_format)
    try:
        _refuse_loaded_templates(environment, source)
        return environment.from_string(source)
    except TemplateRenderError:
        raise
    except TemplateSyntaxError as exc:
        raise TemplateRenderError("template_syntax", _short(exc.message), exc.lineno) from None
    except RecursionError:
        raise TemplateRenderError("template_syntax", "The template is nested too deeply") from None


def check_template_syntax(source: str, output_format: str = "text") -> TemplateRenderError | None:
    """The syntax problem of a template, or None when it compiles."""
    try:
        _compile(source, output_format)
    except TemplateRenderError as exc:
        return exc
    return None


def _template_line(exc: BaseException) -> int | None:
    lines = [
        frame.lineno
        for frame in traceback.extract_tb(exc.__traceback__)
        if frame.filename == _TEMPLATE_NAME
    ]
    return lines[-1] if lines else None


def _render_bounded(source: str, context: dict[str, Any], output_format: str) -> str:
    """Render in this process, stopping as soon as the output passes its limit."""
    from jinja2 import UndefinedError
    from jinja2.sandbox import SecurityError

    template = _compile(source, output_format)
    parts: list[str] = []
    size = 0
    try:
        for chunk in template.generate(**context):
            size += len(chunk)
            if size > MAX_OUTPUT_CHARS:
                raise TemplateRenderError(
                    "output_too_large", f"The result is longer than {MAX_OUTPUT_CHARS} characters"
                )
            parts.append(chunk)
    except TemplateRenderError:
        raise
    except UndefinedError as exc:
        raise TemplateRenderError(
            "value_missing", _short(exc.message), _template_line(exc)
        ) from None
    except SecurityError as exc:
        raise TemplateRenderError("template_forbidden", _short(exc), _template_line(exc)) from None
    except MemoryError:
        raise TemplateRenderError("output_too_large", "The template used too much memory") from None
    except RecursionError:
        raise TemplateRenderError("render_failed", "The template recursed too deeply") from None
    except Exception as exc:
        message = f"{type(exc).__name__}: {_short(exc)}"
        raise TemplateRenderError("render_failed", message, _template_line(exc)) from None
    return "".join(parts)


def _limit_child_memory() -> None:
    try:
        import resource
    except ImportError:  # pragma: no cover - not available on this platform
        return
    try:
        resource.setrlimit(resource.RLIMIT_AS, (CHILD_MEMORY_BYTES, CHILD_MEMORY_BYTES))
    except (ValueError, OSError):  # pragma: no cover - the platform refuses the limit
        return


def _child_main(request: dict[str, Any]) -> None:
    """Entry point of the child process: render once, answer once on stdout."""
    if sys.platform.startswith("linux"):
        _limit_child_memory()
    try:
        text = _render_bounded(
            str(request.get("template") or ""),
            dict(request.get("context") or {}),
            str(request.get("format") or "text"),
        )
        answer: dict[str, Any] = {"ok": True, "text": text}
    except TemplateRenderError as exc:
        answer = {"ok": False, **exc.dump()}
    sys.stdout.write(json.dumps(answer, ensure_ascii=False))
    sys.stdout.flush()


def _context_environment() -> Any:
    """The environment agent context templates render in.

    The same sandbox as a Template step, with no globals at all (no range,
    lipsum, cycler, joiner or namespace): a context template only reads the
    values it is given. A name it does not know renders as nothing, as agent
    prompts always have.
    """
    from jinja2 import Undefined

    class _SilentUndefined(Undefined):
        def __str__(self) -> str:
            return ""

        def __iter__(self):
            return iter([])

        def __bool__(self) -> bool:
            return False

    environment = _environment("text")
    environment.undefined = _SilentUndefined
    environment.globals.clear()
    return environment


def _compile_context_template(source: str) -> Any:
    from jinja2 import TemplateSyntaxError

    if len(source) > MAX_TEMPLATE_CHARS:
        raise TemplateRenderError(
            "template_too_large", f"The template is longer than {MAX_TEMPLATE_CHARS} characters"
        )
    environment = _context_environment()
    try:
        _refuse_loaded_templates(environment, source, "an agent context template")
        return environment.from_string(source)
    except TemplateRenderError:
        raise
    except TemplateSyntaxError as exc:
        raise TemplateRenderError("template_syntax", _short(exc.message), exc.lineno) from None
    except RecursionError:
        raise TemplateRenderError("template_syntax", "The template is nested too deeply") from None


def check_context_template(source: str) -> TemplateRenderError | None:
    """Why an agent context template cannot be saved, or None."""
    try:
        _compile_context_template(source)
    except TemplateRenderError as exc:
        return exc
    return None


def render_context_template(source: str, context: dict[str, Any]) -> str:
    """Render an agent context template in this process, inside the sandbox.

    Unlike a Template step it reads live state objects, so it does not go
    through a child process; the sandbox refuses unsafe attributes and calls,
    and the output stops at its limit.
    """
    from jinja2.sandbox import SecurityError

    template = _compile_context_template(source)
    parts: list[str] = []
    size = 0
    try:
        for chunk in template.generate(**context):
            size += len(chunk)
            if size > MAX_OUTPUT_CHARS:
                raise TemplateRenderError(
                    "output_too_large", f"The result is longer than {MAX_OUTPUT_CHARS} characters"
                )
            parts.append(chunk)
    except TemplateRenderError:
        raise
    except SecurityError as exc:
        raise TemplateRenderError("template_forbidden", _short(exc), _template_line(exc)) from None
    except RecursionError:
        raise TemplateRenderError("render_failed", "The template recursed too deeply") from None
    return "".join(parts)


def render_template(source: str, context: dict[str, Any], *, output_format: str) -> str:
    """Render ``source`` with ``context`` in a child process; raise TemplateRenderError."""
    if output_format not in TEMPLATE_FORMATS:
        raise TemplateRenderError("format_invalid", f"Unknown format '{output_format}'")
    if len(source) > MAX_TEMPLATE_CHARS:
        raise TemplateRenderError(
            "template_too_large", f"The template is longer than {MAX_TEMPLATE_CHARS} characters"
        )
    try:
        context_json = json.dumps(context, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        raise TemplateRenderError(
            "input_not_json", "The template inputs must be JSON values"
        ) from None
    if len(context_json.encode("utf-8")) > MAX_CONTEXT_BYTES:
        raise TemplateRenderError(
            "inputs_too_large", f"The template inputs are larger than {MAX_CONTEXT_BYTES} bytes"
        )
    request = json.dumps(
        {
            "sys_path": [_PACKAGE_ROOT, *(entry for entry in sys.path if entry)],
            "template": source,
            "context": json.loads(context_json),
            "format": output_format,
        },
        ensure_ascii=False,
    )
    try:
        completed = subprocess.run(  # nosec B603 - fixed argv, this interpreter, no shell.
            [sys.executable, "-I", "-c", _CHILD_BOOTSTRAP],
            input=request.encode("utf-8"),
            capture_output=True,
            timeout=RENDER_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise TemplateRenderError(
            "render_timeout", f"The template ran longer than {RENDER_TIMEOUT_SECONDS:g} seconds"
        ) from None
    try:
        answer = json.loads(completed.stdout.decode("utf-8"))
    except ValueError:  # includes UnicodeDecodeError
        answer = None
    if not isinstance(answer, dict):
        raise TemplateRenderError(
            "render_failed",
            "The template could not be rendered"
            + (" (out of memory)" if completed.returncode else ""),
        )
    if answer.get("ok") is True and isinstance(answer.get("text"), str):
        return answer["text"]
    line = answer.get("line")
    raise TemplateRenderError(
        str(answer.get("code") or "render_failed"),
        _short(answer.get("message") or "The template could not be rendered"),
        line if isinstance(line, int) else None,
    )


__all__ = [
    "MAX_CONTEXT_BYTES",
    "MAX_OUTPUT_CHARS",
    "MAX_TEMPLATE_CHARS",
    "RENDER_TIMEOUT_SECONDS",
    "TEMPLATE_FORMATS",
    "TemplateRenderError",
    "check_template_syntax",
    "render_template",
]
