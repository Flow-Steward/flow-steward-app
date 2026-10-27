from __future__ import annotations

import ast
from pathlib import Path

from tests.support.source_index import parse_file

CORE_ROOT = Path(__file__).resolve().parents[2] / "core"
CANONICAL_FACTORY = Path("application/orchestration/model_provider.py")
VENDOR_MODULE_PREFIXES = (
    "langchain_anthropic",
    "langchain_google_genai",
    "langchain_openai",
)
VENDOR_CONSTRUCTORS = {
    "AzureChatOpenAI",
    "ChatAnthropic",
    "ChatGoogleGenerativeAI",
    "ChatOpenAI",
}


def test_vendor_chat_model_construction_stays_in_canonical_factory() -> None:
    offenders: list[str] = []
    for path in sorted(CORE_ROOT.rglob("*.py")):
        relative_path = path.relative_to(CORE_ROOT)
        if relative_path == CANONICAL_FACTORY:
            continue
        for node in parse_file(path).nodes:
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
                if any(module.startswith(VENDOR_MODULE_PREFIXES) for module in modules):
                    offenders.append(f"{relative_path}:{node.lineno}:vendor import")
            elif isinstance(node, ast.ImportFrom):
                module = str(node.module or "")
                if module.startswith(VENDOR_MODULE_PREFIXES):
                    offenders.append(f"{relative_path}:{node.lineno}:vendor import")
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in VENDOR_CONSTRUCTORS
            ):
                offenders.append(f"{relative_path}:{node.lineno}:{node.func.id} construction")

    assert offenders == [], offenders
