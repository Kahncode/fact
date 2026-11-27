"""
Project workflows import only `fact.sdk` and `fact.core` from fact.

`.fact/workflows/` is loaded by path, not as a package, so import-linter cannot see it; this test parses it instead.
"""

import ast
from pathlib import Path

import pytest

WORKFLOWS_DIR = Path(__file__).resolve().parents[2] / ".fact" / "workflows"
ALLOWED = ("fact.sdk", "fact.core")


def _fact_imports(source: str) -> list[str]:
    """Every `fact` module a source file imports, wherever the import sits (also inside `with` blocks)."""
    modules: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module == "fact":
                modules += [f"fact.{alias.name}" for alias in node.names]
            else:
                modules.append(node.module)
    return [m for m in modules if m == "fact" or m.startswith("fact.")]


def forbidden_imports(source: str) -> list[str]:
    return [m for m in _fact_imports(source) if not any(m == a or m.startswith(f"{a}.") for a in ALLOWED)]


@pytest.mark.parametrize("path", sorted(WORKFLOWS_DIR.glob("*.py")), ids=lambda p: p.name)
def test_project_workflows_import_only_sdk_and_core(path: Path) -> None:
    assert forbidden_imports(path.read_text(encoding="utf-8")) == []


def test_forbidden_imports_catches_worker_api_and_cli() -> None:
    source = """
from temporalio import workflow
with workflow.unsafe.imports_passed_through():
    from fact.sdk.workflow import FactWorkflow
    from fact.core.enums import WorkflowStatus
    from fact.worker.loader import load
    import fact.api.app
    from fact import cli
"""
    assert forbidden_imports(source) == ["fact.worker.loader", "fact.api.app", "fact.cli"]
